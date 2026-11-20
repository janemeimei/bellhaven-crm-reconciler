import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bellhaven.actions import action_reason, customize_action, editable_fields, recommended_action
from bellhaven.crm import DemoCRM
from bellhaven.demo import PARENT_ID, demo_accounts, demo_website
from bellhaven.domain import account_snapshot
from bellhaven.executor import Executor
from bellhaven.pipeline import run_demo, run_pipeline
from bellhaven.store import Store, StoreError


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "demo.sqlite", "demo-v1")
        self.crm = DemoCRM(self.store, demo_accounts())
        run_demo(self.store)
        self.parents = {PARENT_ID, "DEMO-FORMER"}

    def case(self, kind):
        return next(row for row in self.store.proposals() if row["active"] and row["proposal"].kind == kind)

    def test_ambiguous_recommendation_flags_bellhaven_accounts_needs_review(self):
        plan = recommended_action(self.case("relationship_review")["proposal"])
        self.assertTrue(plan.actionable)
        self.assertTrue(all(set(step["payload"]) == {"note", "status"} for step in plan.steps))
        self.assertTrue(all(step["payload"]["status"] == "Needs Review" for step in plan.steps))

    def test_mapping_review_leaves_other_operators_accounts_unchanged(self):
        proposal = self.case("relationship_review")["proposal"]
        outsider = dict(self.crm.get_account("DEMO-CEDAR"))
        widened = replace(proposal, before={**proposal.before, "DEMO-CEDAR": account_snapshot(outsider)})
        plan = recommended_action(widened)
        self.assertNotIn("DEMO-CEDAR", {step["account_id"] for step in plan.steps})

    def test_mapping_review_note_and_reason_identify_the_actual_conflicting_records(self):
        proposal = self.case("relationship_review")["proposal"]
        plan = recommended_action(proposal)
        note = plan.steps[0]["payload"]["note"]
        for text in ("Bellhaven Campus North", "Bellhaven Campus South", "Former Campus Center", "606 Campus Way"):
            self.assertIn(text, note)
            self.assertIn(text, action_reason(plan))
        account_id = plan.steps[0]["account_id"]
        self.assertTrue(note.startswith(proposal.before[account_id]["note"]))
        self.assertEqual(set(plan.steps[0]["payload"]), {"note", "status"})

    def test_unresolved_cases_flag_needs_review_without_changing_parent_or_balances(self):
        protected = ("parent_id", "lifetime_revenue", "outstanding_ar")
        for kind in ("relationship_review", "financial_review", "unlisted_review"):
            with self.subTest(kind=kind):
                row = self.case(kind)
                plan = recommended_action(row["proposal"])
                before = {
                    step["account_id"]: self.crm.get_account(step["account_id"]) for step in plan.steps
                }
                self.store.approve_revision(
                    row["id"], row["plan_hash"], plan, "Flag the discrepancy for review.",
                )
                Executor(self.store, self.crm).apply(row["id"])
                for account_id, account in before.items():
                    after = self.crm.get_account(account_id)
                    self.assertEqual(after["status"], "Needs Review")
                    self.assertEqual(
                        {field: after[field] for field in protected},
                        {field: account[field] for field in protected},
                    )

    def test_reviewer_can_keep_the_crm_status_with_an_inline_edit(self):
        row = self.case("unlisted_review")
        recommended = recommended_action(row["proposal"])
        self.assertEqual(recommended.steps[0]["payload"]["status"], "Needs Review")
        plan = customize_action(
            recommended, {(0, "status"): "Active"}, "The facility is still current; keep it Active.",
            "The account is not listed.", self.parents,
        )
        self.assertEqual(set(plan.steps[0]["payload"]), {"note"})
        self.store.approve_revision(
            row["id"], row["plan_hash"], plan, "The facility is still current; keep it Active.",
        )
        Executor(self.store, self.crm).apply(row["id"])
        self.assertEqual(self.crm.get_account("DEMO-UNLISTED")["status"], "Active")

    def test_listed_inactive_account_is_recommended_active(self):
        account = self.crm.get_account("DEMO-WILLOW")
        account["status"] = "Inactive"
        self.store.put_demo_account(account)
        run_demo(self.store)
        row = next(
            item for item in self.store.proposals()
            if item["active"] and item["proposal"].location_keys == ("demo:willow",)
        )
        plan = recommended_action(row["proposal"])
        self.assertEqual(plan.steps[0]["payload"]["status"], "Active")
        self.assertIn("Inactive CRM status is outdated", action_reason(plan))
        self.store.decide(row["id"], row["plan_hash"], "approved", action_reason(plan))
        Executor(self.store, self.crm).apply(row["id"])
        self.assertEqual(self.crm.get_account("DEMO-WILLOW")["status"], "Active")

    def test_custom_approval_requires_reason_and_keeps_case_identity(self):
        row = self.case("field_correction")
        plan = customize_action(row["proposal"], {(0, "name"): "Willow Residence"}, "", "Original name mismatch.", self.parents)
        with self.assertRaises(StoreError):
            self.store.approve_revision(row["id"], row["plan_hash"], plan, "")
        self.assertEqual(self.crm.get_account("DEMO-WILLOW")["name"], "Willow Manor")
        reason = "The reviewer confirmed the current name."
        plan = customize_action(row["proposal"], {(0, "name"): "Willow Residence"}, reason, "Original name mismatch.", self.parents)
        self.store.approve_revision(row["id"], row["plan_hash"], plan, reason)
        Executor(self.store, self.crm).apply(row["id"])
        current = self.store.get_proposal(row["id"])
        self.assertEqual(current["case_number"], row["case_number"])
        self.assertEqual(current["created_at"], row["created_at"])
        self.assertEqual(current["revision"], row["revision"] + 1)
        self.assertEqual(self.store.audit()[0]["reason"], reason)

    def test_custom_creation_is_not_reversed_on_an_unchanged_rerun(self):
        row = self.case("missing_account")
        plan = customize_action(
            row["proposal"], {(0, "billing_zip"): "43001"}, "Confirmed postal correction.",
            "The listed facility has no account.", self.parents,
        )
        self.store.approve_revision(row["id"], row["plan_hash"], plan, "Confirmed postal correction.")
        Executor(self.store, self.crm).apply(row["id"])
        run_demo(self.store)
        run_demo(self.store)
        key = row["proposal"].location_keys[0]
        self.assertFalse(any(
            key in item["proposal"].location_keys and item["active"] and item["decision"] == "pending"
            for item in self.store.proposals()
        ))
        website = demo_website()
        changed = replace(
            website, locations=tuple(
                replace(location, name="Bellhaven Meadow Lodge") if location.key == key else location
                for location in website.locations
            ),
        )
        run_pipeline(self.store, self.crm, changed, PARENT_ID)
        self.assertTrue(any(
            key in item["proposal"].location_keys and item["active"] and item["decision"] == "pending"
            for item in self.store.proposals()
        ))

    def test_custom_chow_preserves_old_account_and_locks_protected_fields(self):
        row = self.case("chow")
        old = account_snapshot(self.crm.get_account("DEMO-CEDAR"))
        with self.assertRaises(ValueError):
            customize_action(row["proposal"], {(1, "chow_current_account"): "anything"}, "Reason", "Issue", self.parents)
        self.assertFalse(any(field == "lifetime_revenue" for _, field in editable_fields(row["proposal"])))
        plan = customize_action(
            row["proposal"], {(0, "name"): "Cedar Current Residence", (0, "billing_city"): "Current City"},
            "Confirmed current name and city.",
            "The parent differs and billing history is protected.", self.parents,
        )
        self.store.approve_revision(row["id"], row["plan_hash"], plan, "Confirmed current name.")
        Executor(self.store, self.crm).apply(row["id"])
        current = account_snapshot(self.crm.get_account("DEMO-CEDAR"))
        successor = current.pop("chow_current_account")
        old.pop("chow_current_account")
        self.assertEqual(current, old)
        self.assertEqual(self.crm.get_account(successor)["name"], "Cedar Current Residence")
        self.assertEqual(self.crm.get_account(successor)["billing_city"], "Current City")

    def test_multiple_inline_edits_keep_original_step_targets_when_a_patch_is_removed(self):
        first = recommended_action(self.case("relationship_review")["proposal"])
        second = recommended_action(self.case("unlisted_review")["proposal"])
        recommended = replace(
            first, before={**first.before, **second.before},
            steps=(first.steps[0], second.steps[0]),
            account_ids=first.account_ids + second.account_ids,
        )
        first_id = recommended.steps[0]["account_id"]
        second_id = recommended.steps[1]["account_id"]
        plan = customize_action(
            recommended,
            {
                (0, "note"): recommended.before[first_id]["note"],
                (0, "status"): recommended.before[first_id]["status"],
                (1, "note"): "Second account needs review.",
            },
            "Only the second account needs a note.", "The identities need review.", self.parents,
        )
        self.assertEqual(len(plan.steps), len(recommended.steps) - 1)
        self.assertEqual(plan.steps[0]["account_id"], second_id)
        self.assertEqual(plan.steps[0]["payload"]["note"], "Second account needs review.")
        self.assertEqual(recommended.steps[0]["account_id"], first_id)


if __name__ == "__main__":
    unittest.main()
