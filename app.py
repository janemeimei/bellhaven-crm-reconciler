from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st

from bellhaven.crm import CRMError, DemoCRM
from bellhaven.demo import PARENT_ID, demo_accounts
from bellhaven.executor import ExecutionError, Executor
from bellhaven.models import Location
from bellhaven.pipeline import run_demo
from bellhaven.reconcile import build_location_plan
from bellhaven.store import Store, StoreError


ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="Bellhaven Review Workbench", page_icon=":material/fact_check:", layout="wide")


def refresh(store: Store) -> None:
    run_demo(store)
    st.rerun()


def show_case(store: Store, crm: DemoCRM, row: dict) -> None:
    proposal = row["proposal"]
    st.subheader(proposal.title)
    st.caption(
        f"Decision: {row['decision']}  |  Execution: {row['execution']}  |  "
        f"Revision: {row['revision']}"
    )
    st.write(proposal.rationale)
    for warning in proposal.warnings:
        st.warning(warning)
    if proposal.kind == "chow":
        st.warning("BILLING PROTECTION: do not change the old account's parent, name, status, or note.")
    if row["message"]:
        st.info(row["message"])
    website_column, crm_column = st.columns(2)
    with website_column:
        st.markdown("**Website evidence**")
        for location in proposal.evidence.get("locations", []):
            st.markdown(f"**{location['name']}**")
            st.write(f"{location['address']}, {location['city']}, {location['state']} {location['zip']}")
            st.write("Care: " + "; ".join(location["care_offerings"]))
            st.code(location["source_url"], language=None)
            st.caption(location["evidence"])
        if not proposal.location_keys:
            st.info("No location was found in the complete synthetic website snapshot. This does not prove a sale.")
    with crm_column:
        st.markdown("**Related CRM accounts**")
        related = {
            account["account_id"]: account
            for account in proposal.evidence.get("accounts", []) + proposal.evidence.get("related_accounts", [])
        }
        if related:
            for account in related.values():
                st.markdown(f"**{account['name']}**")
                st.caption(f"{account['account_id']} | Parent: {account['parent_id']}")
                st.write(
                    f"{account.get('billing_street', '')}, "
                    f"{account.get('billing_city', '')}, {account.get('billing_state', '')} "
                    f"{account.get('billing_zip', '')}"
                )
                for label, field in (
                    ("Lifetime revenue", "lifetime_revenue"), ("Outstanding AR", "outstanding_ar"),
                ):
                    value = account.get(field)
                    display = "Unknown (not zero)" if value is None or value == "" else str(value)
                    st.write(f"**{label}:** {display}")
                st.caption(f"Status: {account.get('status', '')} | Care: {account.get('care_type', '')}")
                for field in ("chow_current_account", "duplicate_of_account"):
                    if account.get(field):
                        st.caption(f"{field}: {account[field]}")
            with st.expander("Full account evidence"):
                st.json(list(related.values()))
        else:
            st.info("No plausible existing account was found.")
    st.markdown("**Exact proposed plan**")
    if proposal.steps:
        changes = []
        for number, step in enumerate(proposal.steps, 1):
            if step["op"] == "bind":
                changes.append({"step": number, "account": step["account_id"], "field": "local identity link", "before": "", "after": step["location_key"]})
            else:
                target = step.get("account_id", "NEW:" + step.get("ref", ""))
                for field, value in step["payload"].items():
                    before = proposal.before.get(target, {}).get(field, "(new account)")
                    changes.append(
                        {
                            "step": number, "account": target, "field": field,
                            "before": str(before), "after": json.dumps(value) if isinstance(value, dict) else str(value),
                        }
                    )
        st.dataframe(changes, hide_index=True, width="stretch")
        with st.expander("Inspect the immutable plan"):
            st.json({"plan_hash": proposal.plan_hash, "steps": proposal.steps})
    else:
        st.info("This case is not executable. Select an identity below or reject the unsupported conclusion.")
    if proposal.kind == "relationship_review" and row["decision"] == "pending":
        with st.expander("Prepare an explicit identity resolution"):
            st.caption("Preparation does not approve or execute anything. Other accounts remain unchanged.")
            locations = [Location.from_dict(value) for value in proposal.evidence["locations"]]
            selected_location = st.selectbox(
                "Website location", locations, format_func=lambda value: value.name,
                key="manual-location-" + row["id"],
            )
            available = [
                account for account in crm.list_accounts()
                if account["account_id"] != PARENT_ID and account.get("billing_street")
                and not account.get("chow_current_account") and not account.get("duplicate_of_account")
            ]
            options = ["new"] + [account["account_id"] for account in available]
            by_id = {account["account_id"]: account for account in available}
            selected_id = st.selectbox(
                "Current account for this facility", options,
                format_func=lambda value: "Create a new current account" if value == "new" else f"{by_id[value]['name']} [{value}]",
                key="manual-account-" + row["id"],
            )
            if st.button("Prepare resolution for review", key="prepare-" + row["id"]):
                plan = build_location_plan(
                    selected_location, by_id.get(selected_id), PARENT_ID, manual=True,
                )
                if plan is None or not plan.actionable:
                    st.error("The financial/identity evidence does not yet support an executable plan.")
                else:
                    prepared_id = store.add_proposal(plan)
                    if store.get_proposal(prepared_id)["decision"] == "pending":
                        st.success("Resolution prepared. Select it in the review queue; it still needs approval.")
                        st.rerun()
                    else:
                        st.info("This resolution was already decided. Include decided cases to explicitly reopen it.")
    if row["decision"] == "pending" and row["active"]:
        reason = st.text_input("Decision reason (optional)", key="reason-" + row["id"])
        acknowledged = st.checkbox(
            "I reviewed the evidence and every step of this plan.",
            key="ack-" + row["id"] + "-" + row["plan_hash"],
        )
        approve_column, reject_column = st.columns(2)
        with approve_column:
            if st.button(
                "Approve and apply locally", type="primary", key="approve-" + row["id"],
                disabled=not acknowledged or not proposal.actionable,
            ):
                store.decide(row["id"], row["plan_hash"], "approved", reason)
                Executor(store, crm).apply(row["id"])
                refresh(store)
        with reject_column:
            if st.button("Reject proposal", key="reject-" + row["id"]):
                store.decide(row["id"], row["plan_hash"], "rejected", reason)
                st.rerun()
    elif row["decision"] == "approved" and row["execution"] != "applied":
        if st.button("Resume / reconcile approved plan", key="resume-" + row["id"]):
            Executor(store, crm).apply(row["id"])
            refresh(store)
    if row["decision"] != "pending" and row["active"] and row["execution"] != "applied":
        if st.button("Explicitly reopen with latest evidence", key="reopen-" + row["id"]):
            store.reopen(row["id"])
            st.rerun()


def main() -> None:
    st.title("Bellhaven Review Workbench")
    st.caption("OFFLINE DEMO  |  Synthetic data only  |  No live website or CRM connections")
    store = Store(os.environ.get("BELLHAVEN_DEMO_DB", str(ROOT / "data" / "demo.sqlite")), "demo-v1")
    crm = DemoCRM(store, demo_accounts())
    if store.latest_run() is None:
        run_demo(store)
    st.sidebar.header("Local workspace")
    st.sidebar.success("Offline mode")
    st.sidebar.info("Live testing is locked until the owner confirms. No credential is loaded by this app.")
    if st.sidebar.button("Refresh local reconciliation", width="stretch"):
        refresh(store)
    st.sidebar.caption("Decisions survive refreshes and application restarts.")
    st.sidebar.download_button(
        "Download decision audit", json.dumps(store.audit(), indent=2),
        file_name="synthetic-decision-audit.json", mime="application/json",
    )
    latest = store.latest_run()
    rows = store.proposals()
    pending = [row for row in rows if row["active"] and row["decision"] == "pending"]
    columns = st.columns(5)
    for column, label, value in zip(
        columns,
        ("Website locations", "Local CRM accounts", "Pending cases", "Applied", "Rejected"),
        (
            latest["summary"]["website_locations"], len(crm.list_accounts()), len(pending),
            sum(row["execution"] == "applied" for row in rows),
            sum(row["decision"] == "rejected" for row in rows),
        ),
    ):
        column.metric(label, value)
    review_tab, accounts_tab, history_tab, guide_tab = st.tabs(
        ["Review queue", "Local CRM", "Decision history", "How it works"]
    )
    with review_tab:
        show_history = st.checkbox("Include decided and no-longer-observed cases")
        visible = rows if show_history else [
            row for row in rows if row["active"] and (
                row["decision"] == "pending"
                or (row["decision"] == "approved" and row["execution"] != "applied")
            )
        ]
        if not visible:
            st.success("No pending proposals. Refreshing will not resurrect decided items.")
        else:
            by_id = {row["id"]: row for row in visible}
            selected = st.selectbox(
                "Case", list(by_id),
                format_func=lambda key: f"{by_id[key]['proposal'].title} [{by_id[key]['decision']}]",
            )
            show_case(store, crm, by_id[selected])
    with accounts_tab:
        st.info("This is a persistent synthetic CRM, not your assessment sandbox.")
        st.dataframe(crm.list_accounts(), hide_index=True, width="stretch")
    with history_tab:
        st.dataframe(store.audit(), hide_index=True, width="stretch")
        st.caption("Reopening is explicit; past decisions and their approved plan snapshots are retained.")
    with guide_tab:
        st.markdown(
            """
            **Identity first, then ownership.** Names and addresses are evidence, not permanent IDs.
            Multiple records may be duplicates, distinct services, or legitimate billing history.

            **CHOW uses AND.** Revenue history and positive AR together require a new current account.
            The old account only receives its `chow_current_account` pointer.
            Either value being zero permits direct re-parenting; missing values do not count as zero.

            **Nothing writes without approval.** Approve applies only the displayed plan.
            Reject is also saved. Interrupted writes are reconciled before another attempt.

            **Local-first.** All names, locations, and balances here are synthetic.
            Live connection and the real assessment review are separate, owner-approved steps.
            """
        )


try:
    main()
except (StoreError, CRMError, ExecutionError, ValueError) as exc:
    st.error(str(exc))
    st.info("The operation was not marked successful. Check the case state before continuing.")
