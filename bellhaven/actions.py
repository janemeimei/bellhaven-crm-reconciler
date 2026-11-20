from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from typing import Any

from bellhaven.domain import STATUSES, WRITABLE_FIELDS, validate_payload
from bellhaven.models import Proposal
from bellhaven.reconcile import append_note


FIELD_LABELS = {
    "name": "Name", "parent_id": "Parent", "billing_street": "Street address",
    "billing_city": "City", "billing_state": "State", "billing_zip": "ZIP",
    "care_type": "Care offerings", "status": "Account status", "phone": "Phone", "note": "Note",
    "chow_current_account": "Current account after change of ownership", "duplicate_of_account": "Duplicate of account",
}


def relationship_summary(proposal: Proposal) -> str:
    locations = proposal.evidence["locations"]
    accounts = proposal.evidence["accounts"]
    website_names = "; ".join(
        f"{location['name']} ({'; '.join(location['care_offerings'])})" for location in locations
    )
    account_names = "; ".join(
        f"{account['name']} [{account['account_id']}] ({account.get('care_type') or 'care not provided'})"
        for account in accounts
    )
    addresses = {
        (location["address"], location["city"], location["state"], location["zip"])
        for location in locations
    }
    address = f" at {locations[0]['address']}" if len(addresses) == 1 else ""
    return (
        f"The website lists {len(locations)} {'facility' if len(locations) == 1 else 'facilities'}"
        f"{address}: {website_names}, but CRM has {len(accounts)} candidate "
        f"{'account' if len(accounts) == 1 else 'accounts'}: {account_names}."
    )


def action_reason(proposal: Proposal) -> str:
    if proposal.metadata.get("action_reason"):
        return proposal.metadata["action_reason"]
    if proposal.kind == "relationship_review":
        return (
            relationship_summary(proposal)
            + " Confirm which CRM account represents each website facility. Until then, flag the "
            "Bellhaven account(s) Needs Review with a note; parents and balances stay unchanged."
        )
    reasons = {
        "field_correction": "The website lists this matched facility with different current details; update the CRM account to match.",
        "reparent": "The website lists this facility under Bellhaven, and the financial fields permit re-parenting the existing account.",
        "chow": "Revenue history and positive outstanding AR require preserving the old account and creating a separate current account.",
        "chow_link": "A current account already exists. The former operator's account has revenue history and outstanding AR, so per the SOP it stays unchanged except chow_current_account.",
        "missing_account": "The listed facility has no matched current account in the collected CRM snapshot.",
        "duplicate": "Several CRM accounts represent one facility. Keep one current account and mark the other copies Inactive duplicates linked to it; nothing is merged or deleted.",
        "unlisted_review": "The complete website crawl no longer lists this Bellhaven account. Absence does not show whether it was sold or closed, so flag it Needs Review with a note; its parent and balances stay unchanged.",
        "financial_review": "Financial data is incomplete, so the change-of-ownership SOP cannot be applied safely. Flag the account Needs Review with a note; its parent stays unchanged until the finances are confirmed.",
        "identity_confirmation": "The reviewer selected this account as the current identity for the website record.",
    }
    if proposal.kind not in reasons:
        raise ValueError("This case has no supported recommendation reason.")
    reason = reasons[proposal.kind]
    if any(step["op"] == "patch" and step["payload"].get("status") == "Active" for step in proposal.steps):
        reason += " The website currently lists this facility, so its Inactive CRM status is outdated."
    return reason


def recommended_action(proposal: Proposal) -> Proposal:
    if proposal.actionable and proposal.steps:
        return proposal
    parent_id = proposal.metadata.get("parent_id")
    steps = []
    review_text = (
        relationship_summary(proposal) + " Confirm which CRM account represents each website facility."
        if proposal.kind == "relationship_review" else proposal.rationale
    )
    for account_id, account in proposal.before.items():
        if account.get("chow_current_account") or account.get("duplicate_of_account"):
            continue
        if proposal.kind == "relationship_review" and parent_id and account.get("parent_id") != parent_id:
            continue
        patch = {"note": append_note(account, "Review required: " + review_text)}
        if account.get("status") == "Active":
            patch["status"] = "Needs Review"
        patch = {field: value for field, value in patch.items() if account.get(field) != value}
        if patch:
            steps.append({"op": "patch", "account_id": account_id, "payload": patch})
    return replace(
        proposal, steps=tuple(steps), actionable=bool(steps),
        metadata={**proposal.metadata, "action_reason": action_reason(proposal)},
    )


def editable_fields(proposal: Proposal) -> list[tuple[int, str]]:
    result = []
    for index, step in enumerate(proposal.steps):
        if step["op"] not in {"create", "patch"}:
            continue
        for field, value in step["payload"].items():
            if field not in WRITABLE_FIELDS or field in {"chow_current_account", "duplicate_of_account"}:
                continue
            if field == "status" and step["payload"].get("duplicate_of_account"):
                continue
            if isinstance(value, str):
                result.append((index, field))
        if step["op"] == "patch":
            account = proposal.before[step["account_id"]]
            if (
                "status" not in step["payload"] and account.get("status") in STATUSES
                and step["account_id"] not in proposal.metadata.get("chow_old_ids", [])
                and not account.get("chow_current_account") and not account.get("duplicate_of_account")
            ):
                result.append((index, "status"))
    return result


def customize_action(
    proposal: Proposal, edits: dict[tuple[int, str], str], reason: str,
    issue_summary: str, parent_ids: set[str],
) -> Proposal:
    allowed = set(editable_fields(proposal))
    steps = deepcopy(list(proposal.steps))
    metadata: dict[str, Any] = {
        **proposal.metadata, "action_type": "custom", "action_reason": reason.strip(),
        "issue_summary": issue_summary, "manual": False,
    }
    for (index, field), value in edits.items():
        if (index, field) not in allowed:
            raise ValueError("This field is protected or is not available for customization.")
        value = value.strip()
        if field in {"name", "billing_street", "billing_city", "care_type"} and not value:
            raise ValueError(f"{FIELD_LABELS[field]} cannot be empty.")
        if field == "parent_id" and value not in parent_ids:
            raise ValueError("Choose an existing parent account.")
        if field == "status" and value not in STATUSES:
            raise ValueError("Choose Active, Inactive, or Needs Review.")
        if field == "billing_state" and not re.fullmatch(r"[A-Z]{2}", value):
            raise ValueError("State must use a two-letter uppercase abbreviation.")
        if field == "billing_zip" and not re.fullmatch(r"\d{5}(?:-\d{4})?", value):
            raise ValueError("ZIP must contain five digits, optionally followed by a four-digit extension.")
        step = steps[index]
        if field == "note" and step["op"] == "create":
            marker = re.search(r"\[bh:[a-f0-9]{16}\]", step["payload"].get("note", ""))
            if marker is None:
                raise ValueError("The new-account plan is missing its recovery marker.")
            if marker[0] not in value:
                value = (value + " " + marker[0]).strip()
        step["payload"][field] = value
        if step["op"] == "patch":
            before = proposal.before[step["account_id"]]
            if before.get(field) == value:
                del step["payload"][field]
        if field == "parent_id" and step["op"] == "create":
            metadata["parent_id"] = value
    steps = [step for step in steps if step["op"] == "bind" or step["payload"]]
    if not any(step["op"] in {"create", "patch"} for step in steps):
        raise ValueError("The custom action contains no CRM changes.")
    for step in steps:
        if step["op"] in {"create", "patch"}:
            validate_payload(step["payload"])
    return replace(proposal, steps=tuple(steps), metadata=metadata)
