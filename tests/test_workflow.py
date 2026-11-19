from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bellhaven.crm import CRMError, DemoCRM, LiveCRM, UncertainWrite
from bellhaven.demo import PARENT_ID, account, demo_accounts, demo_website, location
from bellhaven.domain import FinanceError, account_snapshot, requires_chow
from bellhaven.executor import ExecutionError, Executor, StaleProposal
from bellhaven.pipeline import run_demo, run_pipeline
from bellhaven.reconcile import build_location_plan, reconcile
from bellhaven.store import Store, StoreError


class SpyCRM(DemoCRM):
    def __init__(self, store):
        super().__init__(store, demo_accounts())
        self.writes = []

    def create_account(self, payload):
        self.writes.append(("create", payload))
        return super().create_account(payload)

    def update_account(self, account_id, payload):
        self.writes.append(("patch", account_id, payload))
        return super().update_account(account_id, payload)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "demo.sqlite"
        self.store = Store(self.path, "demo-v1")
        self.crm = SpyCRM(self.store)
        run_demo(self.store)

    def case(self, kind):
        return next(
            row for row in self.store.proposals()
            if row["active"] and row["proposal"].kind == kind
        )

    def approve(self, row):
        self.store.decide(row["id"], row["plan_hash"], "approved")

    def test_sop_uses_and_not_or(self):
        for revenue, ar, expected in ((0, 0, False), (10, 0, False), (0, 10, False), (10, 10, True)):
            with self.subTest(revenue=revenue, ar=ar):
                row = account("A", "A", revenue=revenue, ar=ar)
                self.assertEqual(requires_chow(row), expected)
        for field in ("lifetime_revenue", "outstanding_ar"):
            row = account("A", "A")
            row[field] = None
            with self.assertRaises(FinanceError):
                requires_chow(row)

    def test_rebranding_and_parent_changes_are_separate(self):
        renamed = self.case("field_correction")["proposal"]
        self.assertEqual(renamed.steps[0]["payload"], {"name": "Bellhaven Willow House"})
        direct = self.case("reparent")["proposal"]
        self.assertEqual(direct.steps[0]["payload"], {"parent_id": PARENT_ID})
        self.assertTrue(direct.warnings)

    def test_common_brand_does_not_join_unrelated_facilities(self):
        active = [row for row in self.store.proposals() if row["active"]]
        self.assertEqual(len(active), 8)
        shared = self.case("relationship_review")["proposal"]
        self.assertEqual(set(shared.location_keys), {"demo:campus-north", "demo:campus-south"})

    def test_refresh_does_not_multiply_pending_proposals(self):
        before = {row["proposal"].key for row in self.store.proposals() if row["active"]}
        run_demo(self.store)
        after = {row["proposal"].key for row in self.store.proposals() if row["active"]}
        self.assertEqual(before, after)

    def test_historical_chow_is_not_a_duplicate(self):
        proposals, summary = reconcile(demo_website(), self.crm.list_accounts(), PARENT_ID)
        self.assertFalse(any("demo:harbor" in item.location_keys for item in proposals))
        self.assertIn("DEMO-HARBOR", [item["account_id"] for item in summary["unchanged"]])

    def test_cyclic_history_is_not_mistaken_for_a_missing_account(self):
        website = replace(
            demo_website(), locations=(location("cycle", "Cycle House", "100 Cycle St"),),
        )
        first = account("CYCLE-A", "Cycle House", "100 Cycle Street", current="CYCLE-B")
        second = account("CYCLE-B", "Cycle House", "100 Cycle St", current="CYCLE-A")
        parent = self.crm.get_account(PARENT_ID)
        proposals, _ = reconcile(website, [parent, first, second], PARENT_ID)
        self.assertEqual(proposals[0].kind, "relationship_review")
        self.assertFalse(any(item.kind == "missing_account" for item in proposals))

    def test_missing_finances_block_parent_change(self):
        proposal = self.case("financial_review")["proposal"]
        self.assertFalse(proposal.actionable)
        self.assertFalse(proposal.steps)

    def test_incomplete_crawl_cannot_generate_absence_or_other_writes(self):
        website = replace(demo_website(), errors=("A detail page failed.",))
        with self.assertRaises(ValueError):
            reconcile(website, self.crm.list_accounts(), PARENT_ID)

    def test_unlisted_does_not_mean_sold(self):
        proposal = self.case("unlisted_review")["proposal"]
        self.assertEqual(set(proposal.steps[0]["payload"]), {"status", "note"})
        self.assertEqual(proposal.steps[0]["payload"]["status"], "Needs Review")

    def test_no_write_without_exact_approval(self):
        row = self.case("chow")
        with self.assertRaises(ExecutionError):
            Executor(self.store, self.crm).apply(row["id"])
        with self.assertRaises(StoreError):
            self.store.decide(row["id"], "wrong-version", "approved")
        self.assertFalse(self.crm.writes)

    def test_rejection_persists_across_refresh_and_restart(self):
        row = self.case("unlisted_review")
        self.store.decide(row["id"], row["plan_hash"], "rejected", "Absence is not a sale.")
        run_demo(self.store)
        reopened = Store(self.path, "demo-v1")
        self.assertEqual(reopened.get_proposal(row["id"])["decision"], "rejected")
        with self.assertRaises(ExecutionError):
            Executor(reopened, self.crm).apply(row["id"])
        self.assertFalse(self.crm.writes)
        reopened.reopen(row["id"])
        self.assertEqual(reopened.get_proposal(row["id"])["decision"], "pending")
        self.assertEqual(len(reopened.audit()), 1)

    def test_chow_preserves_every_other_old_business_field(self):
        row = self.case("chow")
        before = account_snapshot(self.crm.get_account("DEMO-CEDAR"))
        self.approve(row)
        Executor(self.store, self.crm).apply(row["id"])
        after = account_snapshot(self.crm.get_account("DEMO-CEDAR"))
        successor = after.pop("chow_current_account")
        before.pop("chow_current_account")
        self.assertEqual(before, after)
        current = self.crm.get_account(successor)
        self.assertEqual(current["parent_id"], PARENT_ID)
        self.assertEqual(current["lifetime_revenue"], 0)
        self.assertEqual(current["outstanding_ar"], 0)
        writes = len(self.crm.writes)
        Executor(self.store, self.crm).apply(row["id"])
        self.assertEqual(len(self.crm.writes), writes)
        run_demo(self.store)
        self.assertFalse(self.store.get_proposal(row["id"])["active"])

    def test_duplicate_loser_links_to_survivor_without_merge(self):
        row = self.case("duplicate")
        self.approve(row)
        Executor(self.store, self.crm).apply(row["id"])
        loser = self.crm.get_account("DEMO-ORCHARD-B")
        self.assertEqual(loser["status"], "Inactive")
        self.assertEqual(loser["duplicate_of_account"], "DEMO-ORCHARD-A")
        self.assertEqual(self.crm.get_account("DEMO-ORCHARD-A")["lifetime_revenue"], 4000)

    def test_financial_drift_invalidates_approval(self):
        row = self.case("chow")
        self.approve(row)
        changed = self.crm.get_account("DEMO-CEDAR")
        changed["outstanding_ar"] = 0
        self.store.put_demo_account(changed)
        with self.assertRaises(StaleProposal):
            Executor(self.store, self.crm).apply(row["id"])
        self.assertFalse(self.crm.writes)
        self.assertEqual(self.store.get_proposal(row["id"])["execution"], "stale")

    def test_lost_create_response_is_reconciled_without_another_post(self):
        class LostResponseCRM(SpyCRM):
            first = True

            def create_account(self, payload):
                result = super().create_account(payload)
                if self.first:
                    self.first = False
                    raise UncertainWrite("Synthetic lost create response.")
                return result

        crm = LostResponseCRM(self.store)
        row = self.case("chow")
        self.approve(row)
        with self.assertRaises(UncertainWrite):
            Executor(self.store, crm).apply(row["id"])
        Executor(self.store, crm).apply(row["id"])
        self.assertEqual(sum(call[0] == "create" for call in crm.writes), 1)
        self.assertEqual(self.store.get_proposal(row["id"])["execution"], "applied")

    def test_unconfirmed_create_is_never_blindly_retried(self):
        class NeverConfirmedCRM(SpyCRM):
            def create_account(self, payload):
                self.writes.append(("create", payload))
                raise UncertainWrite("Synthetic timeout before a result is known.")

        crm = NeverConfirmedCRM(self.store)
        row = self.case("chow")
        self.approve(row)
        for _ in range(2):
            with self.assertRaises(UncertainWrite):
                Executor(self.store, crm).apply(row["id"])
        self.assertEqual(len(crm.writes), 1)

    def test_readback_failure_retains_the_created_account_id(self):
        class ReadbackFailureCRM(SpyCRM):
            fail_id = None

            def create_account(self, payload):
                result = super().create_account(payload)
                self.fail_id = result["account_id"]
                return result

            def get_account(self, account_id):
                if account_id == self.fail_id:
                    self.fail_id = None
                    raise CRMError("Synthetic readback unavailable.")
                return super().get_account(account_id)

        crm = ReadbackFailureCRM(self.store)
        row = self.case("chow")
        self.approve(row)
        with self.assertRaises(UncertainWrite):
            Executor(self.store, crm).apply(row["id"])
        Executor(self.store, crm).apply(row["id"])
        self.assertEqual(sum(call[0] == "create" for call in crm.writes), 1)

    def test_manual_split_resolution_does_not_copy_finances(self):
        north = next(item for item in demo_website().locations if item.key == "demo:campus-north")
        plan = build_location_plan(north, None, PARENT_ID, manual=True)
        proposal_id = self.store.add_proposal(plan)
        self.store.decide(proposal_id, plan.plan_hash, "approved")
        old = account_snapshot(self.crm.get_account("DEMO-CAMPUS"))
        Executor(self.store, self.crm).apply(proposal_id)
        self.assertEqual(account_snapshot(self.crm.get_account("DEMO-CAMPUS")), old)
        self.assertIn(north.key, self.store.identity_links())

    def test_prepared_manual_resolution_survives_refresh(self):
        north = next(item for item in demo_website().locations if item.key == "demo:campus-north")
        plan = build_location_plan(north, None, PARENT_ID, manual=True)
        proposal_id = self.store.add_proposal(plan)
        run_demo(self.store)
        self.assertTrue(self.store.get_proposal(proposal_id)["active"])
        self.assertEqual(self.store.get_proposal(proposal_id)["decision"], "pending")

    def test_new_matching_account_invalidates_a_create(self):
        row = self.case("missing_account")
        self.approve(row)
        payload = row["proposal"].steps[0]["payload"]
        DemoCRM.create_account(self.crm, payload)
        self.crm.writes.clear()
        with self.assertRaises(StaleProposal):
            Executor(self.store, self.crm).apply(row["id"])
        self.assertFalse(self.crm.writes)

    def test_live_paths_are_locked_without_owner_confirmation(self):
        with patch("requests.sessions.Session.request") as request:
            with self.assertRaises(CRMError):
                LiveCRM("synthetic-not-a-credential").list_accounts()
            self.assertFalse(request.called)
        live = LiveCRM("synthetic-not-a-credential")
        with self.assertRaises(ValueError):
            run_pipeline(self.store, live, demo_website(), PARENT_ID)
        with self.assertRaises(ExecutionError):
            Executor(self.store, live).apply("not-a-proposal")


if __name__ == "__main__":
    unittest.main()
