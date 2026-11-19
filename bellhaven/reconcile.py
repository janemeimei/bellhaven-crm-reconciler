from __future__ import annotations

from dataclasses import replace
from difflib import SequenceMatcher
from typing import Any

from bellhaven.domain import (
    Account, FinanceError, account_snapshot, care_key, identity_name, money, normalize, requires_chow,
    street_key, zip_key,
)
from bellhaven.models import Location, Proposal, WebsiteSnapshot, fingerprint


def append_note(account: Account, text: str) -> str:
    current = str(account.get("note") or "").strip()
    return current if text in current else "\n".join(part for part in (current, text) if part)


def candidate_reason(location: Location, account: Account) -> tuple[bool, list[str]] | None:
    street = street_key(location.address)
    same_street = bool(street) and street == street_key(account.get("billing_street"))
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
    if same_street and (same_zip or (same_city and same_state)):
        reasons = ["Street address matches", "ZIP matches" if same_zip else "ZIP differs"]
        website_care = care_key(" ".join(location.care_offerings))
        account_care = care_key(account.get("care_type"))
        conflict = bool(website_care and account_care and not website_care.intersection(account_care))
        if conflict:
            reasons.append("Care offerings conflict; a shared campus is possible")
        if not same_name:
            reasons.append("Names differ; rebranding is possible, not proven")
        return same_zip and not conflict, reasons
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
        "care_type": "; ".join(location.care_offerings), "phone": location.phone,
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
            for key, desired, normalizer in comparisons:
                if normalizer(account.get(key)) != normalizer(desired):
                    patch[key] = desired
            if changing_parent:
                patch["parent_id"] = parent_id
                kind = "reparent"
                if money(account, "lifetime_revenue") == 0 and money(account, "outstanding_ar") > 0:
                    warnings.append("Zero revenue with positive AR: verify the values; the SOP still permits direct re-parenting.")
            if manual and care_key(account.get("care_type")) != care_key(" ".join(location.care_offerings)):
                patch["care_type"] = "; ".join(location.care_offerings)
            if patch:
                steps.append({"op": "patch", "account_id": account_id, "payload": patch})
            elif manual:
                steps.append({"op": "bind", "account_id": account_id, "location_key": location.key})
                kind = "identity_confirmation"
            else:
                return None
            rationale = (
                "Proposed current identity/parent corrections are shown explicitly below. "
                "Address/name similarity supports a candidate, not an automatic business-event conclusion."
            )
            if account.get("status") != "Active":
                warnings.append("The account is not Active. This plan does not silently reactivate it.")
    if manual:
        rationale = "Reviewer-selected identity resolution. " + rationale
    intent = {
        "location": location.key, "kind": kind, "steps": steps,
        "account": account["account_id"] if account else "new",
    }
    return Proposal(
        key="location:" + fingerprint(intent), kind=kind, title=f"{kind.replace('_', ' ').title()}: {location.name}",
        rationale=rationale, location_keys=(location.key,),
        account_ids=tuple(before), evidence=evidence, steps=tuple(steps), before=before,
        warnings=tuple(warnings), metadata=metadata,
    )


def relationship_case(
    locations: list[Location], accounts: list[Account], reasons: dict[str, Any],
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
        evidence={"locations": [location.to_dict() for location in locations], "accounts": accounts, "reasons": reasons},
        steps=(), before={account["account_id"]: account_snapshot(account) for account in accounts},
        actionable=False, group_key=fingerprint(location_keys),
        warnings=("No merge, deletion, or one-to-many CHOW pointer will be invented.",),
    )


def duplicate_plan(
    location: Location, accounts: list[Account], parent_id: str,
) -> Proposal | None:
    if len({normalize(account["name"]) for account in accounts}) != 1:
        return None
    if any(account["parent_id"] != parent_id for account in accounts):
        return None
    if len({frozenset(care_key(account.get("care_type"))) for account in accounts}) != 1:
        return None
    try:
        ranked = sorted(
            accounts,
            key=lambda account: (
                -money(account, "outstanding_ar"), -money(account, "lifetime_revenue"),
                account["account_id"],
            ),
        )
        winner, *losers = ranked
        if any(money(row, field) != 0 for row in losers for field in ("lifetime_revenue", "outstanding_ar")):
            return None
    except FinanceError:
        return None
    base = build_location_plan(location, winner, parent_id, manual=True)
    if base is None or not base.actionable:
        return None
    steps = list(base.steps)
    before = dict(base.before)
    for loser in losers:
        loser_id = loser["account_id"]
        before[loser_id] = account_snapshot(loser)
        note = f"Duplicate candidate of {winner['account_id']}; approved against {location.source_url}."
        steps.append(
            {
                "op": "patch", "account_id": loser_id,
                "payload": {
                    "duplicate_of_account": winner["account_id"], "status": "Inactive",
                    "note": append_note(loser, note),
                },
            }
        )
    return replace(
        base, key="duplicate:" + fingerprint([location.key, winner["account_id"], sorted(before)]),
        kind="duplicate", title=f"Duplicate candidates: {location.name}",
        rationale=(
            "Names, addresses, parent, and care type agree. The proposed losing copies have "
            "zero revenue and zero AR. Confirm these are duplicates, not legitimate historical accounts."
        ),
        account_ids=tuple(before), evidence={"locations": [location.to_dict()], "accounts": accounts},
        steps=tuple(steps), before=before, metadata={**base.metadata, "manual": False},
    )


def reconcile(
    website: WebsiteSnapshot, accounts: list[Account], parent_id: str,
    identity_links: dict[str, str] | None = None,
) -> tuple[list[Proposal], dict[str, Any]]:
    if not website.complete:
        raise ValueError("An incomplete website crawl cannot generate CRM proposals.")
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
        seen_accounts.update(candidates)
        if any(reason[0] for reason in candidates.values()):
            candidates = {
                account_id: reason for account_id, reason in candidates.items() if reason[0]
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
        if selected in current:
            current = {selected: (True, ["Previously confirmed local identity link"])}
        edges[location.key] = current
    proposals = []
    unchanged = []
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
            proposal = duplicate_plan(group[0], candidates, parent_id)
            if proposal is None:
                proposal = relationship_case(group, candidates, edges)
        else:
            proposal = relationship_case(group, candidates, edges)
        if proposal is not None:
            evidence = dict(proposal.evidence)
            evidence["related_accounts"] = [
                row for location in group for row in context[location.key]
                if row["account_id"] not in proposal.account_ids
            ]
            proposals.append(replace(proposal, evidence=evidence))
    for account in accounts:
        account_id = account["account_id"]
        if (
            account["parent_id"] != parent_id or account_id in seen_accounts
            or account.get("chow_current_account") or account.get("duplicate_of_account")
            or account.get("status") == "Inactive"
        ):
            continue
        note = (
            "Not found in a complete Bellhaven website crawl. Absence alone does not establish "
            "a sale, closure, or new parent; ownership needs review."
        )
        patch = {"status": "Needs Review", "note": append_note(account, note)}
        if all(account.get(field) == value for field, value in patch.items()):
            continue
        proposals.append(
            Proposal(
                key="unlisted:" + fingerprint([account_id, parent_id]),
                kind="unlisted_review", title=f"Unlisted account: {account['name']}",
                rationale=note, location_keys=(), account_ids=(account_id,),
                evidence={"locations": [], "accounts": [account], "crawl_complete": True},
                steps=({"op": "patch", "account_id": account_id, "payload": patch},),
                before={account_id: account_snapshot(account)},
                warnings=("This plan changes neither the parent nor the financial fields.",),
                metadata={"parent_id": parent_id},
            )
        )
    return proposals, {
        "website_locations": len(website.locations), "crm_accounts": len(accounts),
        "proposals": len(proposals), "unchanged": unchanged,
        "relationship_cases": sum(not proposal.actionable for proposal in proposals),
        "crawl_warnings": list(website.warnings),
    }
