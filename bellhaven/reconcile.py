from __future__ import annotations

from dataclasses import replace
from difflib import SequenceMatcher
from typing import Any

from bellhaven.domain import (
    Account, FinanceError, account_snapshot, care_key, crm_care_type, identity_name, is_po_box, money,
    normalize, requires_chow, street_key, zip_key,
)
from bellhaven.models import Location, Proposal, WebsiteSnapshot, fingerprint


CLASSIFICATIONS = {
    "field_correction": "Match - update details",
    "reparent": "Match - wrong parent (re-parent)",
    "chow": "Match - wrong parent (change of ownership)",
    "chow_link": "Match - link historical billing account",
    "duplicate": "Match - duplicate accounts",
    "missing_account": "No CRM account",
    "relationship_review": "Needs review - ambiguous mapping",
    "financial_review": "Needs review - financial data",
}
UNLISTED_NOTE = (
    "Not found in a complete Bellhaven website crawl. Absence alone does not establish "
    "a sale, closure, or new parent; ownership needs review."
)


def append_note(account: Account, text: str) -> str:
    current = str(account.get("note") or "").strip()
    return current if text in current else "\n".join(part for part in (current, text) if part)


def addresses_match(left: tuple[Any, Any, Any, Any], right: tuple[Any, Any, Any, Any]) -> bool:
    street = street_key(left[0])
    if not street or street != street_key(right[0]):
        return False
    postal = zip_key(left[3])
    return (bool(postal) and postal == zip_key(right[3])) or (
        normalize(left[1]) == normalize(right[1]) and normalize(left[2]) == normalize(right[2])
    )


def billing_address(account: Account) -> tuple[Any, Any, Any, Any]:
    return (
        account.get("billing_street"), account.get("billing_city"),
        account.get("billing_state"), account.get("billing_zip"),
    )


def same_address(location: Location, account: Account) -> bool:
    return addresses_match(
        (location.address, location.city, location.state, location.zip), billing_address(account),
    )


def candidate_reason(location: Location, account: Account) -> tuple[bool, list[str]] | None:
    postal = zip_key(location.zip)
    same_zip = bool(postal) and postal == zip_key(account.get("billing_zip"))
    same_city = normalize(location.city) == normalize(account.get("billing_city"))
    same_state = normalize(location.state) == normalize(account.get("billing_state"))
    same_name = normalize(location.name) == normalize(account.get("name"))
    location_name = identity_name(location.name)
    account_name = identity_name(account.get("name"))
    similarity = (
        SequenceMatcher(None, location_name, account_name).ratio()
        if location_name and account_name else 0
    )
    similar_name = same_name or similarity >= 0.85
    if same_address(location, account):
        reasons = ["Street address matches", "ZIP matches" if same_zip else "ZIP differs; city and state match"]
        website_care = care_key(" ".join(location.care_offerings))
        account_care = care_key(account.get("care_type"))
        conflict = bool(website_care and account_care and not website_care.intersection(account_care))
        if conflict:
            reasons.append(
                "Care offerings differ; the CRM value may be outdated" if similar_name
                else "Care offerings conflict; a co-located facility is possible"
            )
        if not same_name:
            reasons.append("Names differ; rebranding is possible, not proven")
        return similar_name or not conflict, reasons
    if same_name and same_city and same_state:
        return True, ["Same name in the same city and state", "Street address differs; the CRM address may be outdated"]
    if same_name and not any(account.get(field) for field in ("billing_street", "billing_city", "billing_zip")):
        return True, ["Same name; the CRM account has no address"]
    if same_name or (same_zip and similarity >= 0.72) or (
        same_city and same_state and similarity >= 0.88
    ):
        return False, ["Name/locality candidate without an exact street/ZIP identity match"]
    return None


def location_payload(location: Location, parent_id: str) -> dict[str, Any]:
    marker = "[bh:" + fingerprint({"location": location.key, "parent": parent_id})[:16] + "]"
    return {
        "name": location.name, "parent_id": parent_id,
        "billing_street": location.address, "billing_city": location.city,
        "billing_state": location.state, "billing_zip": location.zip,
        "care_type": crm_care_type(location.care_offerings), "phone": location.phone,
        "status": "Active", "note": f"Current website evidence: {location.source_url} {marker}",
    }


def build_location_plan(
    location: Location, account: Account | None, parent_id: str,
    *, manual: bool = False,
) -> Proposal | None:
    evidence: dict[str, Any] = {"locations": [location.to_dict()], "accounts": [account] if account else []}
    warnings = []
    before = {account["account_id"]: account_snapshot(account)} if account else {}
    steps: list[dict[str, Any]] = []
    bindings: dict[str, Any] = {}
    metadata: dict[str, Any] = {
        "parent_id": parent_id, "bindings": bindings, "manual": manual,
        "selected_account_id": account["account_id"] if account else "",
    }
    kind = "missing_account" if account is None else "field_correction"
    if account is None:
        payload = location_payload(location, parent_id)
        steps.append({"op": "create", "ref": "current", "payload": payload})
        bindings[location.key] = {"ref": "current"}
        rationale = (
            "The reviewer selected a separate current account; existing records will be preserved."
            if manual else "No plausible account was found after comparing the complete CRM snapshot."
        )
    else:
        account_id = account["account_id"]
        bindings[location.key] = account_id
        changing_parent = account["parent_id"] != parent_id
        try:
            chow = requires_chow(account) if changing_parent else False
        except FinanceError as exc:
            return Proposal(
                key="finance-review:" + fingerprint([location.key, account_id, parent_id]),
                kind="financial_review", title=f"Financial review: {location.name}",
                rationale=str(exc), location_keys=(location.key,), account_ids=(account_id,),
                evidence=evidence, steps=(), before=before, warnings=(str(exc),), actionable=False,
                metadata=metadata,
            )
        if chow:
            kind = "chow"
            payload = location_payload(location, parent_id)
            steps = [
                {"op": "create", "ref": "current", "payload": payload},
                {
                    "op": "patch", "account_id": account_id,
                    "payload": {"chow_current_account": {"ref": "current"}},
                },
            ]
            bindings[location.key] = {"ref": "current"}
            metadata["chow_old_ids"] = [account_id]
            rationale = (
                "Revenue history AND positive outstanding AR require a new current account. "
                "Only chow_current_account may change on the old account."
            )
        else:
            patch: dict[str, Any] = {}
            if normalize(account["name"]) != normalize(location.name):
                patch["name"] = location.name
            comparisons = (
                ("billing_street", location.address, street_key),
                ("billing_city", location.city, normalize),
                ("billing_state", location.state, normalize),
                ("billing_zip", location.zip, zip_key),
            )
            # A PO Box is a legitimate mailing/billing address, not a stale facility address.
            if is_po_box(account.get("billing_street")):
                comparisons = ()
            for key, desired, normalizer in comparisons:
                if normalizer(account.get(key)) != normalizer(desired):
                    patch[key] = desired
            website_care = care_key(" ".join(location.care_offerings))
            if website_care and not website_care.intersection(care_key(account.get("care_type"))):
                patch["care_type"] = crm_care_type(location.care_offerings)
            if changing_parent:
                patch["parent_id"] = parent_id
                kind = "reparent"
                if money(account, "lifetime_revenue") == 0 and money(account, "outstanding_ar") > 0:
                    warnings.append("Zero revenue with positive AR: verify the values; the SOP still permits direct re-parenting.")
            if account.get("status") == "Inactive":
                patch["status"] = "Active"
            if patch:
                steps.append({"op": "patch", "account_id": account_id, "payload": patch})
            elif manual:
                steps.append({"op": "bind", "account_id": account_id, "location_key": location.key})
                kind = "identity_confirmation"
            else:
                return None
            rationale = ""
            if account.get("status") == "Needs Review":
                warnings.append("CRM status is Needs Review; this plan keeps that status.")
    if manual:
        rationale = "Reviewer-selected identity resolution. " + rationale
    intent = {
        "location": location.key, "kind": kind, "steps": steps,
        "account": account["account_id"] if account else "new",
    }
    return Proposal(
        key="location:" + fingerprint(intent), kind=kind, title=f"{'Change of Ownership' if kind == 'chow' else kind.replace('_', ' ').title()}: {location.name}",
        rationale=rationale, location_keys=(location.key,),
        account_ids=tuple(before), evidence=evidence, steps=tuple(steps), before=before,
        warnings=tuple(warnings), metadata=metadata,
    )


def relationship_case(
    locations: list[Location], accounts: list[Account], reasons: dict[str, Any], parent_id: str,
) -> Proposal:
    location_keys = tuple(sorted(location.key for location in locations))
    return Proposal(
        key="relationship:" + fingerprint(location_keys), kind="relationship_review",
        title=f"Relationship review: {', '.join(location.name for location in locations)}",
        rationale=(
            "Several identities are plausible. Rebranding, co-located services, splitting, "
            "consolidation, and historical billing accounts must be distinguished before writing."
        ),
        location_keys=location_keys, account_ids=tuple(account["account_id"] for account in accounts),
        evidence={
            "locations": [location.to_dict() for location in locations], "accounts": accounts,
            "reasons": {key: reasons[key] for key in location_keys},
        },
        steps=(), before={account["account_id"]: account_snapshot(account) for account in accounts},
        actionable=False, group_key=fingerprint(location_keys),
        warnings=("No merge, deletion, or one-to-many change-of-ownership pointer will be invented.",),
        metadata={"parent_id": parent_id},
    )


def resolve_accounts(location: Location, accounts: list[Account], parent_id: str) -> Proposal | None:
    try:
        protected = {
            account["account_id"]: account["parent_id"] != parent_id and requires_chow(account)
            for account in accounts
        }
        billing = {
            account["account_id"]: (money(account, "outstanding_ar"), money(account, "lifetime_revenue"))
            for account in accounts
        }
    except FinanceError:
        return None
    survivor = min(
        accounts,
        key=lambda account: (
            protected[account["account_id"]],
            -billing[account["account_id"]][0], -billing[account["account_id"]][1],
            account["parent_id"] != parent_id, account.get("status") != "Active",
            normalize(account["name"]) != normalize(location.name), account["account_id"],
        ),
    )
    survivor_id = survivor["account_id"]
    base = build_location_plan(location, survivor, parent_id)
    if base is not None and not base.actionable:
        return None
    creates_current = base is not None and base.kind == "chow"
    target: Any = {"ref": "current"} if creates_current else survivor_id
    steps = list(base.steps) if base else []
    before = dict(base.before) if base else {survivor_id: account_snapshot(survivor)}
    metadata = dict(base.metadata) if base else {
        "parent_id": parent_id, "bindings": {location.key: survivor_id}, "selected_account_id": survivor_id,
    }
    warnings = list(base.warnings) if base else []
    chow_old_ids = list(metadata.get("chow_old_ids", []))
    duplicates = []
    for account in sorted(accounts, key=lambda row: row["account_id"]):
        account_id = account["account_id"]
        if account_id == survivor_id:
            continue
        before[account_id] = account_snapshot(account)
        if protected[account_id]:
            steps.append({"op": "patch", "account_id": account_id, "payload": {"chow_current_account": target}})
            chow_old_ids.append(account_id)
            continue
        label = "the new current account" if creates_current else survivor_id
        note = f"Duplicate candidate of {label}; approved against {location.source_url}."
        steps.append(
            {
                "op": "patch", "account_id": account_id,
                "payload": {
                    "duplicate_of_account": target, "status": "Inactive", "note": append_note(account, note),
                },
            }
        )
        duplicates.append(account_id)
        if any(billing[account_id]):
            warnings.append(f"Duplicate copy {account_id} has billing history; confirm before retiring it.")
    current = (
        "create a new Bellhaven account as the current account" if creates_current
        else f"keep {survivor_id} as the current account"
    )
    sentences = [f"{len(accounts)} CRM accounts match this website facility; {current}."]
    if duplicates:
        sentences.append(f"Mark {', '.join(duplicates)} Inactive with duplicate_of_account; nothing is merged or deleted.")
    if chow_old_ids:
        sentences.append(
            f"{', '.join(chow_old_ids)} {'has' if len(chow_old_ids) == 1 else 'have'} revenue history and "
            "outstanding AR under another parent, so per the SOP only chow_current_account changes."
        )
    rationale = " ".join(sentences)
    kind = "duplicate" if duplicates else ("chow" if creates_current else "chow_link")
    title = {
        "duplicate": "Duplicate candidates", "chow": "Change of ownership",
        "chow_link": "Change of ownership - link to existing account",
    }[kind]
    prefix = "duplicate:" if kind == "duplicate" else "chow-link:"
    return Proposal(
        key=prefix + fingerprint([location.key, survivor_id, sorted(before)]), kind=kind,
        title=f"{title}: {location.name}", rationale=rationale,
        location_keys=(location.key,), account_ids=tuple(before),
        evidence={"locations": [location.to_dict()], "accounts": accounts},
        steps=tuple(steps), before=before, warnings=tuple(warnings),
        metadata={
            **metadata, "chow_old_ids": chow_old_ids, "survivor_id": survivor_id,
            "manual": False, "action_reason": rationale,
        },
    )


def duplicate_link_plan(loser: Account, survivor: Account) -> Proposal:
    loser_id, survivor_id = loser["account_id"], survivor["account_id"]
    if loser_id == survivor_id:
        raise ValueError("Choose two different accounts.")
    if loser.get("duplicate_of_account") or survivor.get("duplicate_of_account"):
        raise ValueError("An account already marked as a duplicate cannot take part in another duplicate link.")
    if loser.get("chow_current_account"):
        raise ValueError("A historical change-of-ownership account must stay unchanged; it cannot be marked as a duplicate.")
    warnings = []
    try:
        if requires_chow(loser):
            warnings.append(
                "This copy has revenue history and outstanding AR. If it is a former owner's billing "
                "account, use a change-of-ownership link instead."
            )
    except FinanceError as exc:
        warnings.append(str(exc))
    note = append_note(loser, f"Duplicate of {survivor_id}; confirmed by reviewer.")
    return Proposal(
        key="manual-duplicate:" + fingerprint([loser_id, survivor_id]), kind="duplicate",
        title=f"Duplicate candidates: {loser['name']}",
        rationale="Reviewer-selected duplicate. The copy becomes Inactive and points to the surviving account.",
        location_keys=(), account_ids=(survivor_id, loser_id),
        evidence={"locations": [], "accounts": [survivor, loser]},
        steps=(
            {
                "op": "patch", "account_id": loser_id,
                "payload": {"duplicate_of_account": survivor_id, "status": "Inactive", "note": note},
            },
        ),
        before={survivor_id: account_snapshot(survivor), loser_id: account_snapshot(loser)},
        warnings=tuple(warnings),
        metadata={"manual": True, "manual_type": "duplicate", "loser_id": loser_id, "survivor_id": survivor_id},
    )


def chow_link_plan(old: Account, current: Account) -> Proposal:
    old_id, current_id = old["account_id"], current["account_id"]
    if old_id == current_id:
        raise ValueError("Choose two different accounts.")
    if old.get("chow_current_account") or old.get("duplicate_of_account"):
        raise ValueError("The historical account already points to another account.")
    if current.get("chow_current_account") or current.get("duplicate_of_account"):
        raise ValueError("The current account must not point to another account.")
    if old["parent_id"] == current["parent_id"]:
        raise ValueError("A change-of-ownership link connects accounts under different parent companies.")
    if not requires_chow(old):
        raise ValueError(
            "The SOP uses a change of ownership only when revenue history and outstanding AR are both above zero; "
            "re-parent the account or mark a duplicate instead."
        )
    return Proposal(
        key="manual-chow-link:" + fingerprint([old_id, current_id]), kind="chow_link",
        title=f"Change of ownership - link to existing account: {old['name']}",
        rationale=(
            "Reviewer-selected change-of-ownership link. "
            "The billing account stays exactly as it is except chow_current_account."
        ),
        location_keys=(), account_ids=(old_id, current_id),
        evidence={"locations": [], "accounts": [old, current]},
        steps=({"op": "patch", "account_id": old_id, "payload": {"chow_current_account": current_id}},),
        before={old_id: account_snapshot(old), current_id: account_snapshot(current)},
        metadata={
            "manual": True, "manual_type": "chow_link", "old_id": old_id,
            "current_id": current_id, "chow_old_ids": [old_id],
        },
    )


def sold_facility_plan(old: Account, current: Account) -> Proposal:
    plan = chow_link_plan(old, current)
    rationale = (
        f"The complete website crawl no longer lists this Bellhaven account, and {current['name']} "
        f"[{current['account_id']}] under {current.get('parent_name') or current['parent_id']} has the "
        "same address. The old account has revenue history and outstanding AR, so per the SOP it stays "
        "exactly as it is; only chow_current_account points to the current account."
    )
    metadata = {key: value for key, value in plan.metadata.items() if key != "manual_type"}
    return replace(
        plan, key="sold-chow-link:" + fingerprint([old["account_id"], current["account_id"]]),
        rationale=rationale, evidence={**plan.evidence, "crawl_complete": True},
        metadata={**metadata, "manual": False, "sold": True, "action_reason": rationale},
    )


def reconcile(
    website: WebsiteSnapshot, accounts: list[Account], parent_id: str,
    identity_links: dict[str, str] | None = None,
) -> tuple[list[Proposal], dict[str, Any]]:
    if not website.locations:
        raise ValueError("No website locations were collected; nothing can be reconciled.")
    by_id = {account["account_id"]: account for account in accounts}
    if len(by_id) != len(accounts):
        raise ValueError("The CRM snapshot contains duplicate account IDs.")
    if parent_id not in by_id:
        raise ValueError("Select an existing, verified Bellhaven parent account.")
    links = identity_links or {}
    locations = {location.key: location for location in website.locations}

    def current_successor(account_id: str) -> str | None:
        visited_ids = set()
        while account_id in by_id and account_id not in visited_ids:
            visited_ids.add(account_id)
            row = by_id[account_id]
            pointers = [
                value for value in (row.get("chow_current_account"), row.get("duplicate_of_account"))
                if value
            ]
            if not pointers:
                return account_id
            if len(pointers) != 1:
                return None
            account_id = pointers[0]
        return None

    edges: dict[str, dict[str, tuple[bool, list[str]]]] = {}
    seen_accounts: set[str] = set()
    context: dict[str, list[Account]] = {}
    for location in website.locations:
        candidates = {
            account["account_id"]: reason
            for account in accounts
            if account["account_id"] != parent_id
            and (reason := candidate_reason(location, account)) is not None
        }
        context[location.key] = [by_id[account_id] for account_id in candidates]
        if any(reason[0] for reason in candidates.values()):
            candidates = {
                account_id: reason for account_id, reason in candidates.items() if reason[0]
            }
        else:
            candidates = {
                account_id: reason for account_id, reason in candidates.items()
                if same_address(location, by_id[account_id])
            }
        current = {}
        for account_id, reason in candidates.items():
            row = by_id[account_id]
            successor = row.get("chow_current_account") or row.get("duplicate_of_account")
            if successor:
                terminal = current_successor(account_id)
                if terminal in candidates and terminal != account_id:
                    continue
                reason = (False, reason[1] + ["Historical pointer is missing, cyclic, or does not identify a matching current account"])
            current[account_id] = reason
        selected = links.get(location.key)
        terminal = current_successor(selected) if selected in by_id else None
        if terminal is not None:
            current = {terminal: (True, ["Previously confirmed local identity link"])}
        edges[location.key] = current
        seen_accounts.update(current)
    proposals = []
    unchanged = []
    location_rows = []
    visited: set[str] = set()
    for key in locations:
        if key in visited:
            continue
        group_locations = {key}
        group_accounts = set(edges[key])
        while True:
            expanded = {
                other for other, candidates in edges.items()
                if other in group_locations or group_accounts.intersection(candidates)
            }
            expanded_accounts = {account_id for other in expanded for account_id in edges[other]}
            if expanded == group_locations and expanded_accounts == group_accounts:
                break
            group_locations, group_accounts = expanded, expanded_accounts
        visited.update(group_locations)
        group = [locations[item] for item in sorted(group_locations)]
        candidates = [by_id[item] for item in sorted(group_accounts)]
        proposal = None
        if len(group) == 1 and not candidates:
            proposal = build_location_plan(group[0], None, parent_id)
        elif len(group) == 1 and len(candidates) == 1 and edges[key][candidates[0]["account_id"]][0]:
            proposal = build_location_plan(group[0], candidates[0], parent_id)
            if proposal is None:
                unchanged.append({"location": group[0].name, "account_id": candidates[0]["account_id"]})
        elif len(group) == 1 and all(reason[0] for reason in edges[key].values()):
            proposal = (
                resolve_accounts(group[0], candidates, parent_id)
                or relationship_case(group, candidates, edges, parent_id)
            )
        else:
            proposal = relationship_case(group, candidates, edges, parent_id)
        if proposal is not None:
            evidence = dict(proposal.evidence)
            evidence["related_accounts"] = [
                row for location in group for row in context[location.key]
                if row["account_id"] not in proposal.account_ids
            ]
            proposals.append(replace(proposal, evidence=evidence))
        location_rows.extend(
            {
                "key": location.key, "name": location.name,
                "classification": CLASSIFICATIONS[proposal.kind] if proposal else "Confirmed match",
                "account_ids": [account["account_id"] for account in candidates],
                "proposal_key": proposal.key if proposal else None,
            }
            for location in group
        )
    unlisted = []
    for account in accounts if website.complete else []:
        account_id = account["account_id"]
        if (
            account["parent_id"] != parent_id or account_id in seen_accounts
            or account.get("chow_current_account") or account.get("duplicate_of_account")
            or account.get("status") == "Inactive"
        ):
            continue
        successors = [
            other for other in accounts
            if other["account_id"] != account_id and other["account_id"] not in seen_accounts
            and other.get("parent_id") not in ("", parent_id)
            and not other.get("chow_current_account") and not other.get("duplicate_of_account")
            and other.get("status") != "Inactive"
            and addresses_match(billing_address(account), billing_address(other))
        ]
        try:
            sold = len(successors) == 1 and requires_chow(account)
        except FinanceError:
            sold = False
        proposal = None
        classification = "Not on website"
        if sold:
            proposal = sold_facility_plan(account, successors[0])
            proposals.append(proposal)
            classification = "Not on website - change of ownership to new operator"
        else:
            note = UNLISTED_NOTE
            if successors:
                note += " Same address in CRM: " + "; ".join(
                    f"{other['name']} [{other['account_id']}] under {other.get('parent_name') or other['parent_id']}"
                    for other in successors
                ) + "."
            patch = {"note": append_note(account, note)}
            if account.get("status") == "Active":
                patch["status"] = "Needs Review"
            patch = {field: value for field, value in patch.items() if account.get(field) != value}
            if patch:
                proposal = Proposal(
                    key="unlisted:" + fingerprint([account_id, parent_id]),
                    kind="unlisted_review", title=f"Unlisted account: {account['name']}",
                    rationale=note, location_keys=(), account_ids=(account_id,),
                    evidence={
                        "locations": [], "accounts": [account], "related_accounts": successors,
                        "crawl_complete": True,
                    },
                    steps=({"op": "patch", "account_id": account_id, "payload": patch},),
                    before={account_id: account_snapshot(account)},
                    warnings=("The parent and financial fields remain unchanged.",),
                    metadata={"parent_id": parent_id},
                )
                proposals.append(proposal)
        unlisted.append(
            {
                "account_id": account_id, "name": account["name"], "classification": classification,
                "proposal_key": proposal.key if proposal else None,
            }
        )
    return proposals, {
        "website_locations": len(website.locations), "crm_accounts": len(accounts),
        "proposals": len(proposals), "unchanged": unchanged,
        "relationship_cases": sum(not proposal.actionable for proposal in proposals),
        "crawl_complete": website.complete, "crawl_errors": list(website.errors),
        "locations": location_rows, "unlisted": unlisted,
        "crawl_warnings": list(website.warnings),
    }
