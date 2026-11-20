from __future__ import annotations

import json
import os
import getpass
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import pandas as pd

from bellhaven.crm import CRM, CRMError, DemoCRM, LiveCRM
from bellhaven.actions import (
    FIELD_LABELS, action_reason, customize_action, editable_fields, recommended_action, relationship_summary,
)
from bellhaven.credentials import read_token_file
from bellhaven.domain import STATUSES, FinanceError, care_key, normalize, requires_chow, street_key, zip_key
from bellhaven.demo import PARENT_ID, demo_accounts
from bellhaven.executor import ExecutionError, Executor
from bellhaven.models import Location, Proposal, fingerprint
from bellhaven.pipeline import run_demo, run_pipeline
from bellhaven.reconcile import build_location_plan, chow_link_plan, duplicate_link_plan
from bellhaven.scrape import WebsiteError, scrape_website
from bellhaven.store import Store, StoreError, format_case_id
from bellhaven.verify import reused_website_verification, verify_website


ROOT = Path(__file__).resolve().parent
CRM_CHANGE_EVENTS = frozenset({"Account created", "Account updated"})
REVIEW_EVENTS = frozenset({"Approved", "Rejected"})
PROPOSAL_REFRESH_EVENTS = frozenset({"Evidence refreshed", "Proposal refreshed"})
MAIN_VIEWS = ("Pending cases", "History", "Facilities overview")
st.set_page_config(page_title="Bellhaven Review Workbench", page_icon=":material/fact_check:", layout="wide")


def live_settings() -> dict[str, str] | None:
    if os.environ.get("BELLHAVEN_MODE", "demo") != "live":
        return None
    missing = [name for name in ("BELLHAVEN_TOKEN_FILE", "BELLHAVEN_PARENT_ID") if not os.environ.get(name)]
    if missing:
        raise ValueError("Live mode needs " + " and ".join(missing) + ".")
    return {
        "token_file": os.environ["BELLHAVEN_TOKEN_FILE"], "parent_id": os.environ["BELLHAVEN_PARENT_ID"],
        "db": os.environ.get("BELLHAVEN_LIVE_DB", str(ROOT / "data" / "live.sqlite")),
        "writes": os.environ.get("BELLHAVEN_LIVE_WRITES") == "1",
    }


def writes_locked(crm: CRM) -> bool:
    return crm.is_live and not getattr(crm, "writes_enabled", False)


def target_parent_id() -> str:
    live = live_settings()
    return live["parent_id"] if live else PARENT_ID


def refresh(store: Store, crm: CRM, *, recrawl: bool = False) -> None:
    if crm.is_live:
        latest = store.latest_run()
        if recrawl or latest is None:
            website = scrape_website()
            website_verification = verify_website(website)
        else:
            website = latest["website"]
            website_verification = reused_website_verification(latest["summary"])
        run_pipeline(
            store, crm, website, live_settings()["parent_id"], allow_live=True,
            website_verification=website_verification,
        )
    else:
        run_demo(store)
    st.rerun()


def verification_blocks_approval(latest: dict | None) -> str:
    verification = (latest or {}).get("summary", {}).get("verification")
    if verification is None:
        return "Approval is blocked: this snapshot's reads were never verified. Refresh to verify them."
    if verification["status"] == "fail":
        return "Approval is blocked: read verification failed for this snapshot. Refresh after the cause is fixed."
    return ""


def show_refresh_status(latest: dict) -> None:
    refreshed = "Last refreshed: " + display_time(latest["created_at"])
    verification = latest["summary"].get("verification")
    if verification is None:
        st.caption(refreshed)
        st.warning("These reads have not been verified. Refresh to verify them; approval stays blocked until then.")
        return
    problems = [item for item in verification["checks"] if item["result"] in ("FAIL", "WARN")]
    if verification["status"] != "fail" and not problems:
        st.caption(refreshed + "  |  Read verification passed")
        return
    st.caption(refreshed)
    lines = "\n".join(f"- **{item['result']}** {item['area']}: {item['check']} - {item['detail']}" for item in problems)
    if verification["status"] == "fail":
        st.error(f"**Read verification failed.** Approval is blocked until a refresh passes.\n\n{lines}")
    else:
        st.warning(f"**Read verification passed with warnings.** Review them before approving.\n\n{lines}")


def listed_accounts(store: Store, crm: CRM) -> list[dict]:
    # Live pages display the latest refresh snapshot; every write re-reads the live CRM first.
    if not crm.is_live:
        return crm.list_accounts()
    latest = store.latest_run()
    return latest["accounts"] if latest else []


def case_heading(proposal: Proposal) -> str:
    headings = {
        "relationship_review": "Account Mapping Review", "chow": "Change of Ownership",
        "chow_link": "Change of Ownership - Link to Existing Account",
    }
    return headings.get(proposal.kind) or proposal.title.partition(":")[0].title()


def account_label(account: dict) -> str:
    return f"{account['name']} [{account['account_id']}]"


def problem_summary(proposal: Proposal) -> str:
    if proposal.metadata.get("issue_summary"):
        return proposal.metadata["issue_summary"]
    locations = proposal.evidence.get("locations", [])
    accounts = proposal.evidence.get("accounts", [])
    by_id = {account["account_id"]: account for account in accounts}
    location_name = locations[0]["name"] if locations else ""
    account = by_id.get(proposal.metadata.get("survivor_id"), accounts[0] if accounts else {})
    parent = account.get("parent_name") or account.get("parent_id") or "an unspecified parent"
    if proposal.kind == "missing_account":
        if proposal.metadata.get("manual"):
            return f'The separately listed website facility "{location_name}" has no confirmed current CRM account of its own.'
        related = proposal.evidence.get("related_accounts", [])
        if related:
            return (
                f'The website lists "{location_name}", but no CRM account matches its address; '
                f'similar-named accounts elsewhere: {"; ".join(account_label(row) for row in related)}.'
            )
        return f'The website lists "{location_name}", but no matching current CRM account was found.'
    if proposal.kind == "chow":
        return f'The facility is listed by Bellhaven, but its CRM account remains under "{parent}" with revenue history and positive outstanding AR.'
    if proposal.kind == "duplicate":
        if proposal.metadata.get("manual_type") == "duplicate":
            loser = by_id[proposal.metadata["loser_id"]]
            survivor = by_id[proposal.metadata["survivor_id"]]
            return f"The reviewer identified {account_label(loser)} as a duplicate of {account_label(survivor)}."
        return (
            f'{len(accounts)} CRM accounts represent the single website facility "{location_name}": '
            + "; ".join(account_label(row) for row in accounts) + "."
        )
    if proposal.kind == "chow_link":
        old = "; ".join(
            account_label(by_id[account_id]) for account_id in proposal.metadata.get("chow_old_ids", [])
            if account_id in by_id
        )
        current_id = proposal.metadata.get("current_id") or proposal.metadata.get("survivor_id")
        current = account_label(by_id[current_id]) if current_id in by_id else current_id
        if proposal.metadata.get("manual_type") == "chow_link":
            return f"The reviewer identified {old} as the historical billing account for current account {current}."
        if proposal.metadata.get("sold"):
            operator = by_id.get(current_id, {}).get("parent_name") or "another parent"
            return (
                f'The website no longer lists {old}; {current} under "{operator}" has the same address, '
                "and the old Bellhaven account has revenue history and outstanding AR."
            )
        return (
            f'Current account {current} exists for "{location_name}", but the former operator\'s '
            f"billing account {old} is not linked to it."
        )
    if proposal.kind == "relationship_review":
        return relationship_summary(proposal)
    if proposal.kind == "financial_review":
        reason = proposal.rationale.rstrip(".").replace("_", " ")
        return f"Parent correction is blocked: {reason}."
    if proposal.kind == "unlisted_review":
        return f'CRM account "{account["name"]}" is linked to Bellhaven but has no matching location in the complete website snapshot.'
    if proposal.kind == "identity_confirmation":
        return f'The website record has no confirmed identity link to CRM account {proposal.account_ids[0]}.'
    differences = []
    labels = {
        "name": "name", "billing_street": "street address", "billing_city": "city",
        "billing_state": "state", "billing_zip": "ZIP", "care_type": "care offerings",
    }
    for step in proposal.steps:
        if step["op"] != "patch":
            continue
        before = proposal.before[step["account_id"]]
        for field, desired in step["payload"].items():
            if field == "parent_id":
                differences.append(
                    f'parent is "{parent}" in CRM while the facility is listed by Bellhaven'
                    if before.get("parent_id")
                    else "the CRM account has no parent company although Bellhaven lists the facility"
                )
            elif field == "status" and desired == "Active":
                differences.append(f'status is "{before.get("status")}" in CRM although the website lists the facility')
            elif field in labels:
                differences.append(f'{labels[field]} is "{before.get(field, "")}" in CRM versus "{desired}" on the website')
    if not differences:
        raise ValueError("This case has no identifiable issue to summarize.")
    return "CRM and website differ: " + "; ".join(differences) + "."


def comparison_rows(location: dict | None, account: dict | None) -> list[dict[str, str]]:
    website = location or {}
    crm_account = account or {}

    def website_value(field: str) -> str:
        if location is None:
            return "Not listed"
        value = website.get(field)
        if isinstance(value, (list, tuple)):
            return "; ".join(value) or "Not provided"
        return str(value) if value is not None and value != "" else "Not provided"

    def crm_value(field: str) -> str:
        if account is None:
            return "No existing account"
        value = crm_account.get(field)
        if value is None or value == "":
            return "Not provided"
        return str(value)

    rows = [
        {"Field": label, "Website evidence": website_value(website_field), "Related CRM accounts": crm_value(crm_field)}
        for label, website_field, crm_field in (
            ("Name", "name", "name"),
            ("Street address", "address", "billing_street"),
            ("City", "city", "billing_city"),
            ("State", "state", "billing_state"),
            ("ZIP", "zip", "billing_zip"),
            ("Care offerings", "care_offerings", "care_type"),
        )
    ]
    parent = crm_value("parent_id")
    if account is not None and crm_account.get("parent_name"):
        parent = f"{crm_account['parent_name']} [{parent}]"
    rows.append(
        {"Field": "Parent / operator", "Website evidence": "Bellhaven Senior Living" if location else "Not listed", "Related CRM accounts": parent}
    )
    return rows


def website_column(index: int, location: dict | None, count: int) -> str:
    return f"Website {index}: {location['name']}" if count > 1 else "Website evidence"


def crm_column(index: int, account: dict | None, count: int) -> str:
    return f"CRM {index}: {account['name']} [{account['account_id']}]" if count > 1 else "Related CRM accounts"


def comparison_matrix(locations: list[dict], accounts: list[dict]) -> list[dict[str, str]]:
    rows = [{"Field": row["Field"]} for row in comparison_rows(None, None)]
    for index, location in enumerate(locations or [None], 1):
        label = website_column(index, location, len(locations))
        for row, values in zip(rows, comparison_rows(location, None)):
            row[label] = values["Website evidence"]
    for index, account in enumerate(accounts or [None], 1):
        label = crm_column(index, account, len(accounts))
        for row, values in zip(rows, comparison_rows(None, account)):
            row[label] = values["Related CRM accounts"]
    return rows


COMPARED_FIELDS = {
    "Name": ("name", "name", normalize),
    "Street address": ("address", "billing_street", street_key),
    "City": ("city", "billing_city", normalize),
    "State": ("state", "billing_state", normalize),
    "ZIP": ("zip", "billing_zip", zip_key),
}
DIFFERENCE_STYLE = "background-color: rgba(255, 170, 0, 0.28); font-weight: 600"


def differs_from_website(field: str, account: dict, locations: list[dict], parent_id: str) -> bool:
    # Uses the matcher's own comparisons, so a highlight means the same thing as a proposed difference.
    if field in COMPARED_FIELDS:
        website_field, crm_field, key = COMPARED_FIELDS[field]
        return all(key(account.get(crm_field)) != key(location.get(website_field)) for location in locations)
    if field == "Care offerings":
        crm_care = care_key(account.get("care_type"))
        return all(not crm_care & care_key(" ".join(location.get("care_offerings", []))) for location in locations)
    if field == "Parent / operator":
        return account.get("parent_id") != parent_id
    return False


def comparison_table(locations: list[dict], accounts: list[dict], parent_id: str):
    frame = pd.DataFrame(comparison_matrix(locations, accounts))
    styles = pd.DataFrame("", index=frame.index, columns=frame.columns)
    if locations:
        for index, account in enumerate(accounts, 1):
            column = crm_column(index, account, len(accounts))
            for row_index, field in frame["Field"].items():
                if differs_from_website(field, account, locations, parent_id):
                    styles.loc[row_index, column] = DIFFERENCE_STYLE
    return frame.style.apply(lambda _: styles, axis=None)


def crm_detail_rows(accounts: list[dict]) -> list[dict[str, str]]:
    rows = []
    for account in accounts:
        row = {"CRM account ID": account["account_id"], "Account status": account.get("status", "Not provided")}
        for label, field in (("Lifetime revenue", "lifetime_revenue"), ("Outstanding AR", "outstanding_ar")):
            value = account.get(field)
            row[label] = "Unknown (not zero)" if value is None or value == "" else str(value)
        rows.append(row)
    return rows


def show_account_changes(
    proposal: Proposal, widget_prefix: str, parents: dict[str, dict], *, editable: bool,
) -> tuple[dict[tuple[int, str], str], str | None]:
    fields = set(editable_fields(proposal)) if editable else set()
    field_order = {field: position for position, field in enumerate(FIELD_LABELS)}
    edits: dict[tuple[int, str], str] = {}
    validation_error = None
    for index, step in enumerate(proposal.steps):
        account_id = step.get("account_id")
        account = proposal.before.get(account_id, {})
        with st.container(border=True):
            st.markdown(
                f"**{account.get('name', account_id)} [{account_id}]**"
                if account_id else "**New current account**"
            )
            operation = {"create": "Create account", "patch": "Update account", "bind": "Confirm identity"}[step["op"]]
            step_caption = st.empty()
            effect = st.empty()
            if step["op"] == "bind":
                step_caption.caption(f"Step {index + 1} | {operation}")
                effect.markdown("Saves a local website-to-account identity link. No CRM account fields change.")
                st.text_input(
                    "Website identity", value=step["location_key"], disabled=True,
                    key=f"{widget_prefix}-{index}-identity",
                )
                continue
            effective_values = dict(step["payload"])
            step_fields = sorted(
                set(step["payload"]) | {field for step_index, field in fields if step_index == index},
                key=lambda field: (field_order.get(field, len(field_order)), field),
            )
            columns = st.columns(2)
            for position, field in enumerate(step_fields):
                original_value = step["payload"].get(field, account.get(field))
                widget_key = f"{widget_prefix}-{index}-{field}"
                label = FIELD_LABELS.get(field, field)
                if field == "status" and field not in step["payload"]:
                    label += " (optional)"
                field_container = st.container() if field == "note" else columns[position % 2]
                with field_container:
                    if (index, field) not in fields:
                        value = (
                            "New current account"
                            if isinstance(original_value, dict) and "ref" in original_value
                            else str(original_value)
                        )
                        if field == "note":
                            st.text_area(label, value=value, key=widget_key, disabled=True, height=150)
                        else:
                            st.text_input(label, value=value, key=widget_key, disabled=True)
                        continue
                    help_text = "Recommended value: " + original_value
                    if field == "parent_id":
                        choices = list(parents)
                        if original_value not in parents:
                            validation_error = "The suggested parent is not an available root account."
                            value = original_value
                        else:
                            value = st.selectbox(
                                label, choices, index=choices.index(original_value),
                                format_func=lambda key: f"{parents[key]['name']} [{key}]",
                                key=widget_key, help=help_text,
                            )
                    elif field == "status":
                        choices = sorted(STATUSES)
                        if original_value not in choices:
                            validation_error = "The current CRM status is invalid; confirm it before approval."
                            value = original_value
                        else:
                            value = st.selectbox(
                                label, choices, index=choices.index(original_value),
                                key=widget_key, help=help_text,
                            )
                    elif field == "note":
                        value = st.text_area(label, value=original_value, key=widget_key, help=help_text, height=150)
                    else:
                        value = st.text_input(label, value=original_value, key=widget_key, help=help_text)
                    if field == "status" and step["op"] == "patch":
                        st.caption(
                            f"Unchanged: {value}."
                            if value == account.get(field) else f"Changes from {account.get(field)} to {value}."
                        )
                effective_values[field] = value.strip()
                if value.strip() != original_value.strip():
                    edits[index, field] = value
            if step["op"] == "create":
                parent_id = effective_values["parent_id"]
                parent_name = parents.get(parent_id, {}).get("name", parent_id)
                effect.markdown(
                    f"Creates a separate current account under **{parent_name}**. "
                    "This step leaves existing accounts unchanged."
                )
            else:
                changed_fields = {
                    field for field, value in effective_values.items() if account.get(field) != value
                }
                if changed_fields == {"note"}:
                    operation = "Add review note only"
                    effect.markdown(
                        "**Changes: Note only.** Name, parent, status, and balances stay unchanged."
                    )
                elif changed_fields == {"chow_current_account"}:
                    target = effective_values["chow_current_account"]
                    target_text = "the new current account" if isinstance(target, dict) else f"current account **{target}**"
                    effect.markdown(
                        f"Links this historical billing account to {target_text}. "
                        "Its name, parent, status, note, and balances stay unchanged."
                    )
                elif "duplicate_of_account" in changed_fields:
                    labels = ", ".join(FIELD_LABELS.get(field, field) for field in step_fields if field in changed_fields)
                    target = effective_values["duplicate_of_account"]
                    target_text = "the new current account" if isinstance(target, dict) else f"surviving account **{target}**"
                    effect.markdown(
                        f"**Changes: {labels}.** Marks this copy **Inactive** and links it to {target_text}. "
                        "No merge or deletion."
                    )
                elif changed_fields:
                    labels = ", ".join(FIELD_LABELS.get(field, field) for field in step_fields if field in changed_fields)
                    effect.markdown(f"**Changes: {labels}.** Every other account field stays unchanged.")
                else:
                    effect.markdown("No CRM account fields change in this step.")
            step_caption.caption(f"Step {index + 1} | {operation}")
    if not proposal.steps:
        st.info("No safe automatic change is available; use Prepare a different correction below.")
    return edits, validation_error


def show_actions(store: Store, crm: DemoCRM, row: dict, actor: str) -> None:
    proposal = row["proposal"]
    st.markdown("### Change")
    st.caption("Reviewer: " + (actor.strip() or "Not specified"))
    if row["decision"] != "pending":
        st.markdown("**Custom change**" if proposal.metadata.get("action_type") == "custom" else "**Recommended change**")
        show_account_changes(
            proposal, f"reviewed-account-{row['id']}-{row['plan_hash']}", {}, editable=False,
        )
        if row["decision"] == "approved" and row["execution"] != "applied":
            if st.button(
                "Resume / reconcile approved plan", key="resume-" + row["id"],
                disabled=not actor.strip() or writes_locked(crm),
            ):
                with st.spinner(
                    "Checking the CRM, finishing the approved plan, and refreshing. This takes about 20 seconds."
                    if crm.is_live else "Applying locally..."
                ):
                    Executor(store, crm).apply(row["id"], actor=actor)
                    refresh(store, crm)
        if row["active"] and row["execution"] != "applied":
            if st.button("Explicitly reopen with latest evidence", key="reopen-" + row["id"], disabled=not actor.strip()):
                store.reopen(row["id"], actor=actor)
                st.rerun()
        return
    if not row["active"]:
        st.info("This case is no longer observed in the current snapshot.")
        return
    recommended = recommended_action(proposal)
    reason = action_reason(recommended)
    change_heading = st.empty()
    fields = editable_fields(recommended)
    parents = {
        account["account_id"]: account for account in listed_accounts(store, crm) if not account["parent_id"]
    } if any(field == "parent_id" for _, field in fields) else {}
    edits, validation_error = show_account_changes(
        recommended, f"change-value-{row['id']}-{row['plan_hash']}", parents, editable=True,
    )
    decision_reason = st.text_area(
        "Decision reason", value=reason, key=f"reason-{row['id']}-{row['plan_hash']}",
        help="Prefilled from the recommendation reason. Review or edit it before approval.",
    )
    selected_plan = recommended
    if validation_error is None:
        try:
            if edits:
                selected_plan = customize_action(
                    recommended, edits, decision_reason, problem_summary(proposal), set(parents),
                )
            Executor.validate_plan(selected_plan)
        except (ValueError, ExecutionError) as exc:
            validation_error = str(exc)
    change_heading.markdown(
        "**Custom change**"
        if edits or recommended.metadata.get("action_type") == "custom"
        else "**Recommended change**"
    )
    if validation_error:
        st.error(validation_error)
    if proposal.kind == "relationship_review" and validation_error is None:
        flagged = {step.get("account_id") for step in selected_plan.steps}
        for account_id, account in proposal.before.items():
            if account_id not in flagged:
                why = (
                    "historical account link" if account.get("chow_current_account") or account.get("duplicate_of_account")
                    else "under another operator"
                )
                st.caption(f"Left unchanged: {account_label(account)} ({why}).")
        st.info(
            "Approving records this review on the account(s); it does not settle the facility-to-account mapping. "
            "Use Prepare a different correction below to propose a specific mapping."
        )
    if writes_locked(crm):
        st.info(
            "Live read-only: approval is locked until live writes are enabled. "
            "Rejecting records a local decision only; it never writes to the CRM."
        )
    verification_block = verification_blocks_approval(store.latest_run())
    if verification_block:
        st.error(verification_block)
    can_approve = bool(
        actor.strip() and decision_reason.strip() and selected_plan.actionable and not validation_error
        and not writes_locked(crm) and not verification_block
    )
    approve_column, reject_column = st.columns(2, vertical_alignment="bottom")
    with approve_column:
        acknowledged = st.checkbox(
            "I reviewed the evidence and every step of this plan.",
            key="ack-" + row["id"] + "-" + fingerprint(
                [selected_plan.plan_hash, decision_reason, actor, list(edits.items())]
            ),
            disabled=not can_approve,
        )
        if st.button(
            "Approve and write to CRM" if crm.is_live else "Approve and apply locally",
            type="primary", key="approve-" + row["id"],
            disabled=not can_approve or not acknowledged,
        ):
            with st.spinner(
                "Writing to the CRM, reading the result back, and refreshing. This takes about 20 seconds."
                if crm.is_live else "Applying locally..."
            ):
                Executor.validate_plan(selected_plan)
                if selected_plan.plan_hash == row["plan_hash"]:
                    store.decide(row["id"], row["plan_hash"], "approved", decision_reason.strip(), actor=actor)
                else:
                    store.approve_revision(
                        row["id"], row["plan_hash"], selected_plan, decision_reason, actor=actor,
                    )
                Executor(store, crm).apply(row["id"], actor=actor)
                refresh(store, crm)
    with reject_column:
        rejection_reason = st.text_input(
            "Rejection reason", key="reject-reason-" + row["id"],
            placeholder="Required to reject: why is this proposal wrong?",
        )
        if st.button(
            "Reject proposal", key="reject-" + row["id"],
            disabled=not actor.strip() or not rejection_reason.strip(),
        ):
            store.decide(row["id"], row["plan_hash"], "rejected", rejection_reason.strip(), actor=actor)
            st.rerun()


CORRECTION_TYPES = (
    "Map a website facility to an account",
    "Mark an account as a duplicate",
    "Link a historical billing account (change of ownership)",
)


def show_manual_tools(store: Store, crm: DemoCRM, row: dict, actor: str) -> None:
    proposal = row["proposal"]
    facilities = {
        account["account_id"]: account for account in listed_accounts(store, crm) if account.get("parent_id")
    }
    case_ids = [
        account_id for account_id in [
            *proposal.before,
            *(account["account_id"] for account in
              proposal.evidence.get("accounts", []) + proposal.evidence.get("related_accounts", [])),
        ]
        if account_id in facilities
    ]
    current_ids = [
        account_id for account_id in dict.fromkeys(case_ids + sorted(facilities))
        if not facilities[account_id].get("chow_current_account")
        and not facilities[account_id].get("duplicate_of_account")
    ]

    def label(account_id: str) -> str:
        return account_label(facilities[account_id])

    with st.expander("Prepare a different correction", expanded=proposal.kind == "relationship_review"):
        st.write(
            "Prepares a separate proposal for approval. Nothing changes until that proposal is approved. "
            "Accounts from this case are listed first."
        )
        correction = st.radio("Correction type", CORRECTION_TYPES, key="manual-type-" + row["id"])
        plan = None
        error = None
        if correction == CORRECTION_TYPES[0]:
            locations = [Location.from_dict(value) for value in proposal.evidence.get("locations", [])]
            if not locations:
                st.info("This case has no website facility to map.")
            else:
                selected_location = st.selectbox(
                    "Website facility", locations,
                    format_func=lambda value: f"{value.name} | {'; '.join(value.care_offerings)}",
                    key="manual-location-" + row["id"],
                )
                options = ["new"] + [
                    account_id for account_id in current_ids if facilities[account_id].get("billing_street")
                ]
                selected_id = st.selectbox(
                    "Account outcome", options, index=None,
                    placeholder="Select an existing account or create a separate one",
                    format_func=lambda value: "Create a separate current account" if value == "new" else label(value),
                    key="manual-account-" + row["id"],
                )
                if selected_id is not None:
                    plan = build_location_plan(
                        selected_location, facilities.get(selected_id), target_parent_id(), manual=True,
                    )
                    if plan is None or not plan.actionable:
                        error = "The financial/identity evidence does not yet support an executable plan."
                    elif selected_id == "new":
                        st.caption(
                            f"Next proposal: create a separate account for {selected_location.name} under Bellhaven. "
                            "Existing accounts stay unchanged."
                        )
                    else:
                        st.caption(
                            f"Next proposal: use {label(selected_id)} for {selected_location.name}. "
                            "Review the required field changes or the billing-protected change of ownership before approving."
                        )
        elif correction == CORRECTION_TYPES[1]:
            loser_id = st.selectbox(
                "Duplicate copy", current_ids, index=None, format_func=label, key="manual-loser-" + row["id"],
            )
            survivor_id = st.selectbox(
                "Surviving account", [account_id for account_id in current_ids if account_id != loser_id],
                index=None, format_func=label, key="manual-survivor-" + row["id"],
            )
            if loser_id and survivor_id:
                try:
                    plan = duplicate_link_plan(facilities[loser_id], facilities[survivor_id])
                    st.caption(f"Next proposal: mark {label(loser_id)} Inactive as a duplicate of {label(survivor_id)}.")
                except ValueError as exc:
                    error = str(exc)
        else:
            eligible = []
            for account_id in current_ids:
                try:
                    if requires_chow(facilities[account_id]):
                        eligible.append(account_id)
                except FinanceError:
                    continue
            st.caption("Only accounts with revenue history and outstanding AR above zero are eligible, per the SOP.")
            old_id = st.selectbox(
                "Historical billing account", eligible, index=None, format_func=label, key="manual-old-" + row["id"],
            )
            current_options = [
                account_id for account_id in current_ids
                if old_id is None or facilities[account_id]["parent_id"] != facilities[old_id]["parent_id"]
            ]
            current_id = st.selectbox(
                "Current account", current_options, index=None, format_func=label, key="manual-current-" + row["id"],
            )
            if old_id and current_id:
                try:
                    plan = chow_link_plan(facilities[old_id], facilities[current_id])
                    st.caption(
                        f"Next proposal: set only chow_current_account on {label(old_id)} to {current_id}."
                    )
                except ValueError as exc:
                    error = str(exc)
        if error:
            st.error(error)
        reason = st.text_area("Reason for this correction", key="manual-reason-" + row["id"])
        if st.button(
            "Prepare correction for review", key="prepare-" + row["id"],
            disabled=plan is None or error is not None or not reason.strip() or not actor.strip(),
        ):
            plan = replace(plan, metadata={**plan.metadata, "action_reason": reason.strip()})
            prepared_id = store.add_proposal(plan, actor=actor)
            if store.get_proposal(prepared_id)["decision"] == "pending":
                open_case("detail-pending", prepared_id)
                st.rerun()
            else:
                st.info("This correction was already decided. Open it in History to review its decision.")


def show_case(store: Store, crm: DemoCRM, row: dict, actor: str) -> None:
    proposal = row["proposal"]
    st.subheader(case_heading(proposal))
    st.caption(
        f"{format_case_id(row['case_number'], row['created_at'])}  |  Decision: {row['decision']}  |  Execution: {row['execution']}  |  "
        f"Revision: {row['revision']}"
    )
    st.markdown("**Issue:** " + problem_summary(proposal))
    for warning in proposal.warnings:
        st.warning(warning)
    if proposal.kind == "chow":
        st.warning("BILLING PROTECTION: do not change the old account's parent, name, status, or note.")
    if row["message"]:
        st.info(row["message"])
    st.markdown("### Evidence")
    website_records = proposal.evidence.get("locations", [])
    related = {
        account["account_id"]: account
        for account in proposal.evidence.get("accounts", []) + proposal.evidence.get("related_accounts", [])
    }
    st.caption(f"Website: {len(website_records)} facility record(s) | CRM: {len(related)} account(s)")
    st.table(comparison_table(website_records, list(related.values()), target_parent_id()), hide_index=True)
    if website_records and related:
        st.caption("Highlighted: the CRM value differs from the website.")
    for location in website_records:
        st.caption(f"Website source - {location['name']}: {location['source_url']}")
    st.markdown("**CRM account details**")
    if related:
        st.table(crm_detail_rows(list(related.values())), hide_index=True)
        for account in related.values():
            for field in ("chow_current_account", "duplicate_of_account"):
                if account.get(field):
                    st.caption(f"{account['account_id']} | {field}: {account[field]}")
    else:
        st.info("No existing CRM account; financial values are not available.")
    with st.expander("Source evidence and complete records"):
        for location in website_records:
            st.write(location["evidence"])
        st.json({"website_records": website_records, "crm_accounts": list(related.values())})
    show_actions(store, crm, row, actor)
    if row["decision"] == "pending" and row["active"]:
        show_manual_tools(store, crm, row, actor)
    show_case_activity(store, row)


def display_time(value: str) -> str:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Audit timestamps must include a timezone.")
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def show_case_activity(store: Store, row: dict) -> None:
    st.markdown("### Case activity")
    activity = store.case_activity(row["id"])
    changes = [event for event in activity if event["event"] in CRM_CHANGE_EVENTS]
    decisions = [event for event in activity if event["event"] in REVIEW_EVENTS]
    local = [
        event for event in activity
        if event["event"] not in CRM_CHANGE_EVENTS and event["event"] not in REVIEW_EVENTS
    ]

    def activity_table(events: list[dict], scope: str) -> None:
        st.dataframe(
            [
                {
                    "Recorded at (UTC)": display_time(event["at"]), "Who": event["actor"],
                    "Action": "Proposal refreshed (local only)" if event["event"] in PROPOSAL_REFRESH_EVENTS else event["event"],
                    "Scope": scope, "Revision": event["revision"],
                    "Reason": event["details"].get("reason", ""),
                }
                for event in events
            ],
            hide_index=True, width="stretch",
        )

    st.markdown("**Approved CRM changes**")
    if changes:
        activity_table(changes, "Approved CRM write")
    else:
        st.info("No verified CRM changes recorded.")
    if row["execution"] == "uncertain":
        st.warning("A write result is uncertain and may have reached the CRM. Reconcile it before retrying.")
    st.markdown("**Review decisions**")
    if decisions:
        activity_table(decisions, "Review decision")
    else:
        st.caption("No approval or rejection has been recorded.")
    if local:
        with st.expander("Local proposal activity - no CRM writes"):
            activity_table(local, "Local only - no CRM write")
    if changes:
        with st.expander("Change details"):
            for event in changes:
                details = event["details"]
                st.markdown(f"**{event['event']}: {details['account_id']}**")
                st.caption(
                    f"Changed by {event['actor']} | Recorded {display_time(event['at'])} | "
                    f"Verified by {details['verified_by']}"
                )
                if details.get("crm_updated_at"):
                    st.caption("CRM update timestamp: " + details["crm_updated_at"])
                st.table(
                    [
                        {
                            "Field": FIELD_LABELS.get(field, field),
                            "Before": "(new account)" if change["before"] is None else str(change["before"]),
                            "After": str(change["after"]),
                        }
                        for field, change in details["changes"].items()
                    ],
                    hide_index=True,
                )


def show_crawl_status(summary: dict) -> None:
    if summary.get("crawl_complete") is False:
        st.warning(
            "The website crawl was incomplete. Cases were generated for the pages that were parsed; "
            "'not on website' checks were skipped."
        )
        with st.expander("Crawl errors"):
            for error in summary.get("crawl_errors", []):
                st.write(error)


def is_open_case(row: dict) -> bool:
    return bool(row["active"] and row["decision"] == "pending") or (
        row["decision"] == "approved" and row["execution"] != "applied"
    )


def review_status(row: dict | None) -> str:
    if row is None:
        return "No change needed"
    if row["decision"] == "pending":
        return "Pending review"
    if row["decision"] == "rejected":
        return "Rejected - CRM unchanged"
    if row["execution"] == "applied":
        return "Approved - applied to CRM"
    return "Approved - CRM write " + row["execution"]


def open_case(detail_key: str, case_id: str) -> None:
    st.session_state[detail_key] = case_id
    # Streamlit keeps the scroll position across reruns, so the newly opened case asks once to start at the top.
    st.session_state["scroll-to-top"] = datetime.now(timezone.utc).isoformat()


SCROLL_TO_TOP = """<style>
[data-testid="stLayoutWrapper"]:has(> .st-key-scroll-to-top), .st-key-scroll-to-top { display: none; }
</style>
<script>
/* request REQUEST */
for (const element of [document.scrollingElement, document.querySelector('[data-testid="stMain"]')]) {
  if (element) element.scrollTop = 0;
}
</script>"""


def scroll_to_top_once() -> None:
    request = st.session_state.pop("scroll-to-top", None)
    if request:
        with st.container(key="scroll-to-top"):
            st.html(SCROLL_TO_TOP.replace("REQUEST", request), unsafe_allow_javascript=True)


def open_from_overview(case_id: str, view: str) -> None:
    st.session_state["main-view"] = MAIN_VIEWS[0] if view == "pending" else MAIN_VIEWS[1]
    open_case("detail-" + view, case_id)
    st.session_state["return-view"] = "detail-" + view


def close_case(detail_key: str) -> None:
    st.session_state.pop(detail_key, None)
    if st.session_state.get("return-view") == detail_key:
        st.session_state.pop("return-view")
        st.session_state["main-view"] = MAIN_VIEWS[2]


def current_account_id(account_id: str, accounts: dict[str, dict]) -> str:
    visited = set()
    while account_id in accounts and account_id not in visited:
        visited.add(account_id)
        successor = (
            accounts[account_id].get("chow_current_account") or accounts[account_id].get("duplicate_of_account")
        )
        if not successor:
            break
        account_id = successor
    return account_id


def joined_address(*parts: str | None) -> str:
    *lines, state, postal = parts
    region = " ".join(part for part in (state, postal) if part)
    return ", ".join(part for part in (*lines, region) if part)


def crm_columns(account_ids: list[str], accounts: dict[str, dict]) -> dict[str, str]:
    found = [accounts[account_id] for account_id in account_ids if account_id in accounts]

    def parent(account: dict) -> str:
        parent_id = account.get("parent_id") or ""
        if parent_id in accounts:
            return accounts[parent_id]["name"]
        return account.get("parent_name") or parent_id or "No parent"

    return {
        "CRM account": "; ".join(
            [account_label(account) for account in found]
            + [f"[{account_id}] not found in CRM" for account_id in account_ids if account_id not in accounts]
        ) or "None",
        "CRM parent": "; ".join(parent(account) for account in found),
        "CRM status": "; ".join(str(account.get("status") or "") for account in found),
    }


def show_selected_case(selection, cases: list[dict | None], scope: str) -> None:
    selected = selection.selection.rows
    if not selected or selected[0] >= len(cases):
        return
    case = cases[selected[0]]
    if case is None:
        st.caption("No case is linked to this row: no CRM change is needed.")
        return
    st.button(
        "Open " + format_case_id(case["case_number"], case["created_at"]),
        key="overview-open-" + scope, on_click=open_from_overview,
        args=(case["id"], "pending" if is_open_case(case) else "history"),
    )


def show_facilities_overview(store: Store, crm: DemoCRM, latest: dict, rows: list[dict]) -> None:
    summary = latest["summary"]
    st.subheader("Facilities overview")
    st.caption("Match results come from the last refresh; CRM values and review status are current.")
    show_crawl_status(summary)
    if summary.get("locations") is None:
        st.info("Refresh the local reconciliation to build the facilities overview.")
        return
    accounts = {account["account_id"]: account for account in listed_accounts(store, crm)}
    links = store.identity_links()
    website = {location.key: location for location in latest["website"].locations}
    by_key = {row["proposal"].key: row for row in rows}
    reviewed_at: dict[str, str] = {}
    for event in store.case_activity():
        if event["event"] in REVIEW_EVENTS:
            reviewed_at[event["proposal_id"]] = max(event["at"], reviewed_at.get(event["proposal_id"], ""))

    def linked_case(proposal_key: str | None, related: list[dict]) -> dict | None:
        candidates = ([by_key[proposal_key]] if proposal_key in by_key else []) + related
        open_cases = [row for row in candidates if is_open_case(row)]
        if open_cases:
            return open_cases[0]
        return max(
            (row for row in candidates if row["id"] in reviewed_at),
            key=lambda row: reviewed_at[row["id"]], default=None,
        )

    def case_columns(case: dict | None) -> dict[str, str]:
        return {
            "Case": format_case_id(case["case_number"], case["created_at"]) if case else "",
            "Review status": review_status(case),
        }

    facilities, facility_cases = [], []
    for item in summary["locations"]:
        location = website.get(item["key"])
        link = links.get(item["key"])
        account_ids = [current_account_id(link, accounts)] if link in accounts else item["account_ids"]
        case = linked_case(
            item["proposal_key"], [row for row in rows if item["key"] in row["proposal"].location_keys],
        )
        facility_cases.append(case)
        facilities.append(
            {
                "Website facility": item["name"],
                "Website address": joined_address(
                    location.address, location.city, location.state, location.zip,
                ) if location else "",
                "Care offerings": "; ".join(location.care_offerings) if location else "",
                **crm_columns(account_ids, accounts),
                "Match result": item["classification"], **case_columns(case),
            }
        )
    unlisted, unlisted_cases = [], []
    for item in summary.get("unlisted", []):
        account = accounts.get(item["account_id"], {})
        case = linked_case(
            item["proposal_key"],
            [
                row for row in rows
                if not row["proposal"].location_keys and item["account_id"] in row["proposal"].account_ids
            ],
        )
        unlisted_cases.append(case)
        columns = crm_columns([item["account_id"]], accounts)
        unlisted.append(
            {
                "CRM account": columns["CRM account"],
                "CRM address": joined_address(
                    account.get("billing_street"), account.get("billing_city"),
                    account.get("billing_state"), account.get("billing_zip"),
                ),
                "CRM parent": columns["CRM parent"], "CRM status": columns["CRM status"],
                "Match result": item["classification"], **case_columns(case),
            }
        )
    statuses = Counter(row["Review status"] for row in facilities)
    metrics = st.columns(3)
    metrics[0].metric("Website facilities", len(facilities))
    metrics[1].metric("Pending review", statuses["Pending review"])
    metrics[2].metric(
        "Reviewed", len(facilities) - statuses["No change needed"] - statuses["Pending review"],
    )
    st.markdown(f"**Website facilities ({len(facilities)})**")
    st.caption("Each website facility, its current CRM account, and its case. Select a row to open the case.")
    selection = st.dataframe(
        facilities, hide_index=True, width="stretch", height="content", on_select="rerun",
        selection_mode="single-row", key="overview-facilities",
        column_config={"Website facility": st.column_config.TextColumn(pinned=True)},
    )
    show_selected_case(selection, facility_cases, "facility")
    st.markdown(f"**Bellhaven CRM accounts not on the website ({len(unlisted)})**")
    st.caption("Active or Needs Review accounts under the Bellhaven parent that no website facility matched.")
    if unlisted:
        selection = st.dataframe(
            unlisted, hide_index=True, width="stretch", height="content", on_select="rerun",
            selection_mode="single-row", key="overview-unlisted",
            column_config={"CRM account": st.column_config.TextColumn(pinned=True)},
        )
        show_selected_case(selection, unlisted_cases, "unlisted")
    elif summary.get("crawl_complete") is False:
        st.info("Not checked because the website crawl was incomplete.")
    else:
        st.info("Every Bellhaven CRM account matched a website facility.")


def show_case_list(
    store: Store, crm: DemoCRM, rows: list[dict], view: str, actor: str, latest: dict,
) -> None:
    detail_key = "detail-" + view
    by_id = {row["id"]: row for row in rows}
    selected = st.session_state.get(detail_key)
    if selected is not None:
        st.button(
            "Back to facilities overview" if st.session_state.get("return-view") == detail_key
            else "Back to pending cases" if view == "pending" else "Back to history",
            key="back-" + view, on_click=close_case, args=(detail_key,),
        )
        scroll_to_top_once()
        if selected not in by_id:
            st.error("This case is no longer available.")
            return
        show_case(store, crm, by_id[selected], actor)
        return
    st.subheader("Pending cases" if view == "pending" else "History")
    if view == "pending":
        show_crawl_status(latest["summary"])
    activity_by_case: dict[str, list[dict]] = {}
    for event in store.case_activity():
        activity_by_case.setdefault(event["proposal_id"], []).append(event)
    reviews_by_case = {
        case_id: [event for event in activity if event["event"] in REVIEW_EVENTS]
        for case_id, activity in activity_by_case.items()
    }
    if view == "pending":
        visible = [row for row in rows if is_open_case(row)]
    else:
        visible = [
            row for row in rows
            if reviews_by_case.get(row["id"])
        ]
    if view == "history":
        visible.sort(
            key=lambda row: reviews_by_case[row["id"]][-1]["at"],
            reverse=True,
        )
    search = st.text_input(
        "Search cases", placeholder="Case ID, issue, facility, or reviewer", key="search-" + view,
    ).strip().casefold()
    if search:
        visible = [
            row for row in visible
            if search in " ".join(
                [
                    format_case_id(row["case_number"], row["created_at"]),
                    row["created_at"], display_time(row["created_at"]),
                    row["proposal"].title, problem_summary(row["proposal"]),
                    *(event["actor"] for event in activity_by_case.get(row["id"], [])),
                ]
            ).casefold()
        ]
    st.caption(f"{len(visible)} case(s)")
    if not visible:
        st.info("No pending cases." if view == "pending" else "No history yet.")
    for row in visible:
        with st.container(border=True):
            summary_column, open_column = st.columns([5, 1])
            with summary_column:
                status = ""
                if row["decision"] != "pending":
                    label = review_status(row)
                    needs_attention = row["decision"] == "approved" and row["execution"] != "applied"
                    status = "  |  " + (f":orange[**{label}**]" if needs_attention else label)
                st.markdown(
                    f"**{format_case_id(row['case_number'], row['created_at'])}**  \n"
                    f"{case_heading(row['proposal'])}{status}"
                )
                st.write(problem_summary(row["proposal"]))
                activity = activity_by_case.get(row["id"], [])
                changes = [event for event in activity if event["event"] in CRM_CHANGE_EVENTS]
                if changes:
                    last = changes[-1]
                    st.caption(f"Last approved CRM change: {last['actor']} | {display_time(last['at'])}")
                if row["execution"] == "uncertain":
                    st.caption("CRM write outcome: Uncertain - needs reconciliation.")
                decisions = reviews_by_case.get(row["id"], [])
                if decisions:
                    last = decisions[-1]
                    st.caption(f"Last reviewed: {last['actor']} | {display_time(last['at'])} | {last['event']}")
                refreshes = [event for event in activity if event["event"] in PROPOSAL_REFRESH_EVENTS]
                if view == "pending" and refreshes:
                    st.caption("Proposal refreshed (local only): " + display_time(refreshes[-1]["at"]))
                if not activity:
                    st.caption("Created: " + display_time(row["created_at"]))
            with open_column:
                if st.button("Open case", key=f"open-{view}-{row['id']}", width="stretch"):
                    open_case(detail_key, row["id"])
                    if st.session_state.get("return-view") == detail_key:
                        st.session_state.pop("return-view")
                    st.rerun()


def main() -> None:
    live = live_settings()
    st.title("Bellhaven Review Workbench")
    if live:
        token = read_token_file(live["token_file"])
        store = Store(live["db"], "live:" + fingerprint(token))
        crm: CRM = LiveCRM(token, confirmed=True, allow_writes=live["writes"])
    else:
        st.caption("OFFLINE DEMO  |  Synthetic data only  |  No live website or CRM connections")
        store = Store(os.environ.get("BELLHAVEN_DEMO_DB", str(ROOT / "data" / "demo.sqlite")), "demo-v1")
        crm = DemoCRM(store, demo_accounts())
        if store.latest_run() is None:
            run_demo(store)
    st.sidebar.header("Live sandbox" if live else "Local workspace")
    if live and not live["writes"]:
        st.sidebar.caption("Read-only: CRM writes are not enabled.")
    elif not live:
        st.sidebar.success("Offline mode")
        st.sidebar.info("Live testing is locked until the owner confirms. No credential is loaded by this app.")
    actor = st.sidebar.text_input(
        "Reviewer name", value=getpass.getuser(), key="reviewer-name",
        help="A local audit label, not an authenticated sign-in.",
    )
    if not actor.strip():
        st.sidebar.warning("Enter a reviewer name before approving or changing a case.")
    if st.sidebar.button("Refresh from website and CRM" if live else "Refresh local reconciliation", width="stretch"):
        with st.spinner("Refreshing and verifying reads..."):
            refresh(store, crm, recrawl=True)
    st.sidebar.caption("Decisions survive refreshes and application restarts.")
    st.sidebar.download_button(
        "Download decision audit", json.dumps(store.audit(), indent=2),
        file_name="live-decision-audit.json" if live else "synthetic-decision-audit.json",
        mime="application/json",
    )
    latest = store.latest_run()
    if latest is None:
        st.info("No snapshot yet. Use the refresh button in the sidebar.")
        return
    show_refresh_status(latest)
    rows = store.proposals()
    pending_tab, history_tab, overview_tab = st.tabs(
        list(MAIN_VIEWS), key="main-view", on_change="rerun",
    )
    if pending_tab.open:
        with pending_tab:
            show_case_list(store, crm, rows, "pending", actor, latest)
    if history_tab.open:
        with history_tab:
            show_case_list(store, crm, rows, "history", actor, latest)
    if overview_tab.open:
        with overview_tab:
            show_facilities_overview(store, crm, latest, rows)
    with st.sidebar.expander("Read verification"):
        verification = latest["summary"].get("verification")
        if verification is None:
            st.write("Not verified yet.")
        else:
            if verification.get("website_checked_at"):
                st.caption("Website pages verified: " + display_time(verification["website_checked_at"]))
            st.dataframe(
                [
                    {"Result": item["result"], "Area": item["area"], "Check": item["check"], "Detail": item["detail"]}
                    for item in verification["checks"]
                ],
                hide_index=True, width="stretch",
            )
    with st.sidebar.expander("CRM snapshot (last refresh)" if live else "Local CRM"):
        st.dataframe(listed_accounts(store, crm), hide_index=True, width="stretch")
    mode_note = (
        """
        **Live sandbox.** Website and CRM data come from the assessment sandbox.
        Approved plans write through the CRM API only when live writes are explicitly enabled.
        """
        if live else
        """
        **Local-first.** All names, locations, and balances here are synthetic.
        Live connection and the real assessment review are separate, owner-approved steps.
        """
    )
    with st.sidebar.expander("How it works"):
        st.markdown(
            """
            **Identity first, then ownership.** Names and addresses are evidence, not permanent IDs.
            Multiple records may be duplicates, distinct services, or legitimate billing history.

            **Change of ownership uses AND.** Revenue history and positive AR together require a new current account.
            The old account only receives its `chow_current_account` pointer.
            Either value being zero permits direct re-parenting; missing values do not count as zero.

            **Nothing writes without approval.** Approve applies only the displayed plan.
            Reject is also saved. Interrupted writes are reconciled before another attempt.
            """
            + mode_note
        )


try:
    main()
except (StoreError, CRMError, ExecutionError, ValueError, WebsiteError) as exc:
    st.error(str(exc))
    st.info("The operation was not marked successful. Check the case state before continuing.")
