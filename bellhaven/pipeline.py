from __future__ import annotations

from bellhaven.crm import CRM, DemoCRM
from bellhaven.demo import PARENT_ID, demo_accounts, demo_website
from bellhaven.models import WebsiteSnapshot
from bellhaven.reconcile import build_location_plan, reconcile
from bellhaven.store import Store


def run_pipeline(
    store: Store, crm: CRM, website: WebsiteSnapshot, parent_id: str, *, allow_live: bool = False,
) -> dict:
    if crm.is_live and not allow_live:
        raise ValueError("Live pipeline testing is locked in this local-first build.")
    accounts = crm.list_accounts()
    proposals, summary = reconcile(website, accounts, parent_id, store.identity_links())
    by_id = {account["account_id"]: account for account in accounts}
    locations = {location.key: location for location in website.locations}
    known_keys = {proposal.key for proposal in proposals}
    for row in store.proposals():
        previous = row["proposal"]
        if (
            row["decision"] != "pending" or not previous.metadata.get("manual")
            or previous.kind == "duplicate"
        ):
            continue
        if len(previous.location_keys) != 1 or previous.location_keys[0] not in locations:
            continue
        selected_id = previous.metadata.get("selected_account_id", "")
        if selected_id and selected_id not in by_id:
            raise ValueError("A manually selected account disappeared; resolve that case before refreshing.")
        plan = build_location_plan(
            locations[previous.location_keys[0]], by_id.get(selected_id), parent_id, manual=True,
        )
        if plan is not None and plan.key not in known_keys:
            proposals.append(plan)
            known_keys.add(plan.key)
    summary["proposals"] = len(proposals)
    store.save_run(website, accounts, proposals, summary)
    return summary


def run_demo(store: Store) -> dict:
    client = DemoCRM(store, demo_accounts())
    return run_pipeline(store, client, demo_website(), PARENT_ID)
