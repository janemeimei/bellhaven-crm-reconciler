from __future__ import annotations

from dataclasses import replace

from bellhaven.crm import CRM, DemoCRM
from bellhaven.demo import PARENT_ID, demo_accounts, demo_website
from bellhaven.domain import account_snapshot
from bellhaven.models import Proposal, WebsiteSnapshot, fingerprint
from bellhaven.reconcile import build_location_plan, chow_link_plan, duplicate_link_plan, reconcile
from bellhaven.store import Store
from bellhaven.verify import verify_run


def respects_custom_outcome(
    proposal: Proposal, outcome: dict, locations: dict, accounts: dict,
) -> bool:
    if set(proposal.location_keys) != set(outcome["sources"]):
        return False
    if any(
        key not in locations or fingerprint(locations[key].to_dict()) != signature
        for key, signature in outcome["sources"].items()
    ):
        return False
    candidates = set(proposal.account_ids) | {
        account["account_id"] for account in
        proposal.evidence.get("accounts", []) + proposal.evidence.get("related_accounts", [])
    }
    return candidates.issubset(outcome["accounts"]) and all(
        key in accounts and account_snapshot(accounts[key]) == expected
        for key, expected in outcome["accounts"].items()
    )


def rebuild_manual_plan(
    previous: Proposal, locations: dict, by_id: dict, parent_id: str,
) -> Proposal | None:
    manual_type = previous.metadata.get("manual_type", "location")
    if manual_type in {"duplicate", "chow_link"}:
        ids = (
            (previous.metadata["loser_id"], previous.metadata["survivor_id"]) if manual_type == "duplicate"
            else (previous.metadata["old_id"], previous.metadata["current_id"])
        )
        if not all(account_id in by_id for account_id in ids):
            raise ValueError("A manually selected account disappeared; resolve that case before refreshing.")
        builder = duplicate_link_plan if manual_type == "duplicate" else chow_link_plan
        try:
            return builder(by_id[ids[0]], by_id[ids[1]])
        except ValueError:
            return None
    if len(previous.location_keys) != 1 or previous.location_keys[0] not in locations:
        return None
    selected_id = previous.metadata.get("selected_account_id", "")
    if selected_id and selected_id not in by_id:
        raise ValueError("A manually selected account disappeared; resolve that case before refreshing.")
    return build_location_plan(
        locations[previous.location_keys[0]], by_id.get(selected_id), parent_id, manual=True,
    )


def run_pipeline(
    store: Store, crm: CRM, website: WebsiteSnapshot, parent_id: str, *, allow_live: bool = False,
    website_verification: dict | None = None,
) -> dict:
    if crm.is_live and not allow_live:
        raise ValueError("Live pipeline testing is locked in this local-first build.")
    accounts = crm.list_accounts()
    proposals, summary = reconcile(website, accounts, parent_id, store.identity_links())
    by_id = {account["account_id"]: account for account in accounts}
    locations = {location.key: location for location in website.locations}
    outcomes = store.custom_outcomes()
    suppressed = {
        proposal.key for proposal in proposals
        if any(respects_custom_outcome(proposal, outcome, locations, by_id) for outcome in outcomes)
    }
    proposals = [proposal for proposal in proposals if proposal.key not in suppressed]
    for item in summary["locations"]:
        if item["proposal_key"] in suppressed:
            item.update(classification="Confirmed match (reviewer custom outcome)", proposal_key=None)
    for item in summary["unlisted"]:
        if item["proposal_key"] in suppressed:
            item.update(classification="Not on website (reviewer custom outcome)", proposal_key=None)
    known_keys = {proposal.key for proposal in proposals}
    for row in store.proposals():
        previous = row["proposal"]
        if row["decision"] != "pending" or not previous.metadata.get("manual"):
            continue
        plan = rebuild_manual_plan(previous, locations, by_id, parent_id)
        if plan is not None and previous.metadata.get("action_reason"):
            plan = replace(
                plan, metadata={**plan.metadata, "action_reason": previous.metadata["action_reason"]},
            )
        if plan is not None and plan.key not in known_keys:
            proposals.append(plan)
            known_keys.add(plan.key)
    summary["proposals"] = len(proposals)
    summary["verification"] = verify_run(website, accounts, summary, crm, parent_id, website_verification)
    store.save_run(website, accounts, proposals, summary)
    return summary


def run_demo(store: Store) -> dict:
    client = DemoCRM(store, demo_accounts())
    return run_pipeline(store, client, demo_website(), PARENT_ID)
