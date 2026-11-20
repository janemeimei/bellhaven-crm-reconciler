import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bellhaven.crm import DemoCRM, UncertainWrite
from bellhaven.demo import PARENT_ID, demo_accounts, demo_website
from bellhaven.executor import Executor
from bellhaven.pipeline import run_demo, run_pipeline
from bellhaven.store import Store, StoreError


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "demo.sqlite"
        self.store = Store(self.path, "demo-v1")
        self.crm = DemoCRM(self.store, demo_accounts())
        run_demo(self.store)

    def case(self, kind):
        return next(row for row in self.store.proposals() if row["proposal"].kind == kind)

    def test_change_records_who_when_and_field_values_without_duplicate_events(self):
        row = self.case("field_correction")
        self.store.decide(row["id"], row["plan_hash"], "approved", "Current website name.", actor="Jane")
        Executor(self.store, self.crm).apply(row["id"], actor="Jane")
        activity = self.store.case_activity(row["id"])
        change = next(event for event in activity if event["event"] == "Account updated")
        self.assertEqual(change["actor"], "Jane")
        self.assertTrue(change["at"])
        self.assertTrue(change["details"]["crm_updated_at"])
        self.assertEqual(
            change["details"]["changes"]["name"],
            {"before": "Willow Manor", "after": "Bellhaven Willow House"},
        )
        self.assertEqual(change["details"]["reason"], "Current website name.")
        Executor(self.store, self.crm).apply(row["id"], actor="Another reviewer")
        self.assertEqual(self.store.case_activity(row["id"]), activity)

    def test_recovery_keeps_original_writer_separate_from_verifier(self):
        class LostResponseCRM(DemoCRM):
            def create_account(self, payload):
                result = super().create_account(payload)
                raise UncertainWrite("Synthetic lost create response.")

        row = self.case("chow")
        self.store.decide(row["id"], row["plan_hash"], "approved", actor="Jane")
        with self.assertRaises(UncertainWrite):
            Executor(self.store, LostResponseCRM(self.store)).apply(row["id"], actor="Jane")
        Executor(self.store, self.crm).apply(row["id"], actor="Alex")
        activity = self.store.case_activity(row["id"])
        created = next(event for event in activity if event["event"] == "Account created")
        linked = next(event for event in activity if event["event"] == "Account updated")
        self.assertEqual(created["actor"], "Jane")
        self.assertEqual(created["details"]["verified_by"], "Alex")
        self.assertEqual(linked["actor"], "Alex")

    def test_legacy_actor_is_unknown_not_backfilled_with_current_reviewer(self):
        row = self.case("unlisted_review")
        self.store.decide(row["id"], row["plan_hash"], "rejected", "Legacy decision.", actor="Jane")
        with self.store.connection() as connection:
            connection.execute("ALTER TABLE decisions DROP COLUMN actor")
        migrated = Store(self.path, "demo-v1")
        self.assertEqual(migrated.audit()[0]["actor"], "Not recorded")
        self.assertEqual(migrated.case_activity(row["id"])[0]["actor"], "Not recorded")

    def test_blank_reviewer_cannot_approve_or_execute(self):
        row = self.case("field_correction")
        with self.assertRaises(StoreError):
            self.store.decide(row["id"], row["plan_hash"], "approved", actor=" ")
        self.store.decide(row["id"], row["plan_hash"], "approved", actor="Jane")
        with self.assertRaises(StoreError):
            Executor(self.store, self.crm).apply(row["id"], actor="")
        self.assertEqual(self.crm.get_account("DEMO-WILLOW")["name"], "Willow Manor")

    def test_proposal_refresh_is_local_and_never_writes_account_records(self):
        row = self.case("field_correction")
        before = self.crm.list_accounts()
        website = demo_website()
        website = replace(
            website, locations=tuple(
                replace(location, phone="555-0199") if location.key == "demo:willow" else location
                for location in website.locations
            ),
        )
        run_pipeline(self.store, self.crm, website, PARENT_ID)
        event = next(
            event for event in self.store.case_activity(row["id"])
            if event["event"] == "Proposal refreshed"
        )
        self.assertEqual(event["details"]["scope"], "local_proposal")
        self.assertFalse(event["details"]["crm_write"])
        self.assertEqual(self.crm.list_accounts(), before)
        self.assertEqual(self.store.get_proposal(row["id"])["decision"], "pending")
        self.assertIsNone(self.store.operation(row["id"], row["revision"], 0))
        self.assertEqual(self.store.audit(), [])

    def test_unrelated_approved_change_does_not_refresh_other_cases(self):
        unrelated = self.case("relationship_review")
        before = self.store.get_proposal(unrelated["id"])["proposal"].evidence
        row = self.case("field_correction")
        self.store.decide(row["id"], row["plan_hash"], "approved", actor="Jane")
        Executor(self.store, self.crm).apply(row["id"], actor="Jane")
        run_demo(self.store)
        self.assertEqual(self.store.get_proposal(unrelated["id"])["proposal"].evidence, before)
        self.assertEqual(self.store.case_activity(unrelated["id"]), [])


if __name__ == "__main__":
    unittest.main()
