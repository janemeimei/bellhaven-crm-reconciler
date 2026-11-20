from __future__ import annotations

import copy
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
from bellhaven.reconcile import build_location_plan, chow_link_plan, duplicate_link_plan, reconcile
from bellhaven.store import Store, StoreError, format_case_id


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


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return copy.deepcopy(self.body)


class FakeCRMServer:
    def __init__(self, accounts, write_body=None):
        self.accounts = {row["account_id"]: dict(row) for row in accounts}
        self.calls = []
        self.write_body = write_body

    def __call__(self, method, url, json=None, params=None, **kwargs):
        path = url.split("/api/v1", 1)[1]
        self.calls.append((method, path, json))
        if method == "GET" and path == "/accounts":
            rows, page, size = list(self.accounts.values()), params["page"], params["page_size"]
            return FakeResponse(
                200, {"data": rows[(page - 1) * size:page * size], "page": page, "page_size": size, "total": len(rows)},
            )
        if method == "POST":
            account = {**json, "account_id": f"NEW-{len(self.accounts)}"}
            self.accounts[account["account_id"]] = account
        else:
            account = self.accounts[path.rsplit("/", 1)[1]]
        if method == "PATCH":
            account.update(json)
        return FakeResponse(200, account if method == "GET" or self.write_body is None else self.write_body)


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
        self.assertEqual(len(active), 12)
        shared = self.case("relationship_review")["proposal"]
        self.assertEqual(set(shared.location_keys), {"demo:campus-north", "demo:campus-south"})

    def test_refresh_does_not_multiply_pending_proposals(self):
        before = {row["proposal"].key for row in self.store.proposals() if row["active"]}
        run_demo(self.store)
        after = {row["proposal"].key for row in self.store.proposals() if row["active"]}
        self.assertEqual(before, after)

    def test_case_numbers_are_unique_and_stable_across_decisions_and_restarts(self):
        original = {row["id"]: row["case_number"] for row in self.store.proposals()}
        self.assertEqual(len(original), len(set(original.values())))
        row = self.case("unlisted_review")
        with patch("bellhaven.store.utcnow", return_value="2030-01-02T03:04:05+00:00"):
            self.store.decide(row["id"], row["plan_hash"], "rejected")
        run_demo(self.store)
        restarted = Store(self.path, "demo-v1")
        self.assertEqual(
            original, {item["id"]: item["case_number"] for item in restarted.proposals()},
        )
        restarted.reopen(row["id"])
        self.assertEqual(restarted.get_proposal(row["id"])["case_number"], original[row["id"]])
        expected_id = format_case_id(original[row["id"]], row["created_at"])
        self.assertEqual(restarted.audit()[0]["case_id"], expected_id)
        self.assertRegex(expected_id, r"^CASE-\d{8}T\d{6}Z-\d{6}$")
        self.assertEqual(restarted.audit()[0]["case_created_at"], row["created_at"])
        self.assertEqual(restarted.audit()[0]["created_at"], "2030-01-02T03:04:05+00:00")
        north = next(item for item in demo_website().locations if item.key == "demo:campus-north")
        added_id = restarted.add_proposal(build_location_plan(north, None, PARENT_ID, manual=True))
        self.assertGreater(restarted.get_proposal(added_id)["case_number"], max(original.values()))

    def test_existing_database_receives_case_numbers_without_losing_decisions(self):
        row = self.case("unlisted_review")
        self.store.decide(row["id"], row["plan_hash"], "rejected", "Keep this decision.")
        with self.store.connection() as connection:
            connection.execute("DROP TABLE case_numbers")
        migrated = Store(self.path, "demo-v1")
        self.assertEqual(migrated.get_proposal(row["id"])["decision"], "rejected")
        self.assertEqual(migrated.audit()[0]["reason"], "Keep this decision.")
        numbers = [item["case_number"] for item in migrated.proposals()]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertTrue(all(number > 0 for number in numbers))

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

    def test_incomplete_crawl_still_reviews_parsed_locations_but_never_claims_absence(self):
        website = replace(demo_website(), errors=("A detail page failed.",))
        proposals, summary = reconcile(website, self.crm.list_accounts(), PARENT_ID)
        self.assertTrue(any(item.kind == "field_correction" for item in proposals))
        self.assertFalse(any(item.kind == "unlisted_review" for item in proposals))
        self.assertFalse(summary["crawl_complete"])
        self.assertEqual(summary["unlisted"], [])
        with self.assertRaises(ValueError):
            reconcile(replace(website, locations=()), self.crm.list_accounts(), PARENT_ID)

    def test_unlisted_account_is_flagged_for_review_without_changing_parent(self):
        payload = self.case("unlisted_review")["proposal"].steps[0]["payload"]
        self.assertEqual(payload["status"], "Needs Review")
        self.assertIn("Absence alone does not establish", payload["note"])
        self.assertNotIn("parent_id", payload)

    def test_name_variant_copy_becomes_an_inactive_duplicate_of_the_billing_account(self):
        row = next(
            item for item in self.store.proposals()
            if item["proposal"].kind == "duplicate" and "demo:linden" in item["proposal"].location_keys
        )
        self.approve(row)
        Executor(self.store, self.crm).apply(row["id"])
        copy = self.crm.get_account("DEMO-LINDEN-COPY")
        self.assertEqual((copy["status"], copy["duplicate_of_account"]), ("Inactive", "DEMO-LINDEN"))
        self.assertEqual(self.crm.get_account("DEMO-LINDEN")["status"], "Active")

    def test_existing_current_account_receives_the_chow_link_without_another_create(self):
        row = self.case("chow_link")
        before = account_snapshot(self.crm.get_account("DEMO-MAPLE-OLD"))
        self.approve(row)
        Executor(self.store, self.crm).apply(row["id"])
        after = account_snapshot(self.crm.get_account("DEMO-MAPLE-OLD"))
        self.assertEqual(after.pop("chow_current_account"), "DEMO-MAPLE")
        before.pop("chow_current_account")
        self.assertEqual(before, after)
        self.assertFalse(any(call[0] == "create" for call in self.crm.writes))
        run_demo(self.store)
        self.assertFalse(any(
            item["active"] and item["decision"] == "pending" and "demo:maple" in item["proposal"].location_keys
            for item in self.store.proposals()
        ))

    def test_stale_care_type_zip_and_inactive_status_are_corrected(self):
        plans = {
            row["proposal"].location_keys[0]: row["proposal"]
            for row in self.store.proposals() if row["proposal"].kind == "field_correction"
        }
        self.assertEqual(plans["demo:spruce"].steps[0]["payload"], {"care_type": "Memory Care"})
        self.assertEqual(plans["demo:poplar"].steps[0]["payload"], {"billing_zip": "43000", "status": "Active"})

    def test_similar_name_elsewhere_does_not_block_creating_a_missing_account(self):
        website = replace(demo_website(), locations=(location("x", "Bellhaven Linden House", "1 New Street"),))
        parent = self.crm.get_account(PARENT_ID)
        elsewhere = account("ELSEWHERE", "Bellhaven Linden House", "9 Old Road")
        elsewhere.update(billing_city="Othertown", billing_zip="45000")
        proposals, _ = reconcile(website, [parent, elsewhere], PARENT_ID)
        self.assertEqual({item.kind for item in proposals}, {"missing_account", "unlisted_review"})
        missing = next(item for item in proposals if item.kind == "missing_account")
        self.assertEqual([row["account_id"] for row in missing.evidence["related_accounts"]], ["ELSEWHERE"])
        same_city = account("SAME-CITY", "Bellhaven Linden House", "9 Old Road")
        proposals, _ = reconcile(website, [parent, same_city], PARENT_ID)
        self.assertEqual([item.kind for item in proposals], ["field_correction"])
        self.assertEqual(proposals[0].steps[0]["payload"], {"billing_street": "1 New Street"})

    def test_address_variants_care_vocabulary_and_po_boxes_do_not_create_noise(self):
        parent = self.crm.get_account(PARENT_ID)
        nursing = "Short-Term Rehabilitation & Nursing"
        multi = replace(location("multi", "Bellhaven Meadows", "1800 N Blanchard St"), care_offerings=("Assisted Living", "Memory Support"))
        website = replace(demo_website(), locations=(
            location("nw", "Bellhaven of Chesterton", "1250 NW Franklin Street", nursing),
            location("pike", "Bellhaven of Kettering", "3313 Wilmington Pike"), multi,
            location("po-box", "Bellhaven of Ashtabula", "3156 W Prospect Rd"),
            location("new", "Bellhaven of Batavia", "2000 Hospital Dr", nursing),
        ))
        accounts = [
            parent, account("CHESTERTON", "Chesterton Senior Commons", "1250 Northwest Franklin St", care="Skilled Nursing"),
            account("KETTERING", "Bellhaven of Kettering", "3313 Wilmington Pk"),
            account("MEADOWS", "Bellhaven Meadows", "1800 N Blanchard St"),
            account("ASHTABULA", "Bellhaven of Ashtabula", "PO Box 517"),
        ]
        proposals, summary = reconcile(website, accounts, PARENT_ID)
        by_kind = {item.kind: item for item in proposals}
        self.assertEqual(sorted(by_kind), ["field_correction", "missing_account"])
        self.assertEqual(by_kind["field_correction"].steps[0]["payload"], {"name": "Bellhaven of Chesterton"})
        self.assertEqual(by_kind["missing_account"].steps[0]["payload"]["care_type"], "Skilled Nursing")
        self.assertEqual(summary["unlisted"], [])
        self.assertEqual(
            {item["name"] for item in summary["locations"] if item["classification"] == "Confirmed match"},
            {"Bellhaven of Kettering", "Bellhaven Meadows", "Bellhaven of Ashtabula"},
        )

    def test_unlisted_account_sold_to_another_operator_follows_the_sop(self):
        parent = self.crm.get_account(PARENT_ID)
        website = replace(demo_website(), locations=(location("willow", "Bellhaven Willow House", "101 Example Street"),))
        sold = account("SANDUSKY", "Bellhaven of Sandusky", "2715 Columbus Ave", revenue=130000, ar=5200)
        accounts = [
            parent, account("MILLSTONE", "Millstone Health Partners", parent=""),
            account("WILLOW", "Bellhaven Willow House", "101 Example Street"), sold,
            account("BUYER", "Millstone Care of Sandusky", "2715 Columbus Avenue", parent="MILLSTONE"),
        ]
        proposals, summary = reconcile(website, accounts, PARENT_ID)
        self.assertEqual([item.kind for item in proposals], ["chow_link"])
        self.assertEqual(
            proposals[0].steps,
            ({"op": "patch", "account_id": "SANDUSKY", "payload": {"chow_current_account": "BUYER"}},),
        )
        self.assertEqual(summary["unlisted"][0]["classification"], "Not on website - change of ownership to new operator")
        sold.update(outstanding_ar=0)
        proposals, _ = reconcile(website, accounts, PARENT_ID)
        self.assertEqual([item.kind for item in proposals], ["unlisted_review"])
        self.assertEqual([row["account_id"] for row in proposals[0].evidence["related_accounts"]], ["BUYER"])
        self.assertIn("Millstone Care of Sandusky [BUYER]", proposals[0].steps[0]["payload"]["note"])

    def test_manual_duplicate_and_chow_links_follow_the_sop_and_survive_refresh(self):
        accounts = {row["account_id"]: row for row in self.crm.list_accounts()}
        with self.assertRaises(ValueError):
            chow_link_plan(accounts["DEMO-BIRCH"], accounts["DEMO-WILLOW"])
        plans = [
            duplicate_link_plan(accounts["DEMO-ORCHARD-B"], accounts["DEMO-ORCHARD-A"]),
            chow_link_plan(accounts["DEMO-MAPLE-OLD"], accounts["DEMO-MAPLE"]),
        ]
        ids = [
            self.store.add_proposal(replace(plan, metadata={**plan.metadata, "action_reason": "Reviewer evidence."}))
            for plan in plans
        ]
        run_demo(self.store)
        for proposal_id in ids:
            self.assertTrue(self.store.get_proposal(proposal_id)["active"])
            self.assertEqual(self.store.get_proposal(proposal_id)["decision"], "pending")
        before = account_snapshot(self.crm.get_account("DEMO-MAPLE-OLD"))
        row = self.store.get_proposal(ids[1])
        self.approve(row)
        Executor(self.store, self.crm).apply(ids[1])
        after = account_snapshot(self.crm.get_account("DEMO-MAPLE-OLD"))
        self.assertEqual(after.pop("chow_current_account"), "DEMO-MAPLE")
        before.pop("chow_current_account")
        self.assertEqual(before, after)

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

    def test_live_writes_need_the_explicit_flag_and_then_patch_only_approved_fields(self):
        server = FakeCRMServer(demo_accounts())
        store = Store(Path(self.directory.name) / "live.sqlite", "live:test")
        with patch("requests.sessions.Session.request", new=server):
            read_only = LiveCRM("synthetic-not-a-credential", confirmed=True)
            run_pipeline(store, read_only, demo_website(), PARENT_ID, allow_live=True)
            row = next(row for row in store.proposals() if "demo:willow" in row["proposal"].location_keys)
            store.decide(row["id"], row["plan_hash"], "approved", "The website shows the new name.")
            with self.assertRaises(ExecutionError):
                Executor(store, read_only).apply(row["id"])
            self.assertEqual([call for call in server.calls if call[0] != "GET"], [])
            Executor(store, LiveCRM("synthetic-not-a-credential", confirmed=True, allow_writes=True)).apply(row["id"])
        self.assertEqual(
            [call for call in server.calls if call[0] != "GET"],
            [("PATCH", "/accounts/DEMO-WILLOW", {"name": "Bellhaven Willow House"})],
        )
        self.assertEqual(store.get_proposal(row["id"])["execution"], "applied")

    def test_accepted_writes_with_unrecognized_bodies_are_confirmed_by_readback(self):
        server = FakeCRMServer(demo_accounts(), write_body={"ok": True})
        store = Store(Path(self.directory.name) / "live.sqlite", "live:test")
        with patch("requests.sessions.Session.request", new=server):
            run_pipeline(store, LiveCRM("synthetic-not-a-credential", confirmed=True), demo_website(), PARENT_ID, allow_live=True)
            rows = [
                next(row for row in store.proposals() if "demo:willow" in row["proposal"].location_keys),
                next(row for row in store.proposals() if row["proposal"].kind == "missing_account"),
            ]
            live = LiveCRM("synthetic-not-a-credential", confirmed=True, allow_writes=True)
            for row in rows:
                store.decide(row["id"], row["plan_hash"], "approved", "The website shows the current details.")
                Executor(store, live).apply(row["id"])
        self.assertEqual(sorted(call[0] for call in server.calls if call[0] != "GET"), ["PATCH", "POST"])
        for row in rows:
            self.assertEqual(store.get_proposal(row["id"])["execution"], "applied")
            result = store.operation(row["id"], row["revision"], 0)["result"]
            self.assertEqual(result["response_shape"], "object keys: ok")
            self.assertIn(result["account_id"], server.accounts)

    def test_rejected_live_writes_keep_the_servers_reason_without_the_token(self):
        response = FakeResponse(
            422, {"detail": [{"loc": ["body", "nam"], "msg": "Unknown field for synthetic-not-a-credential"}]},
        )
        live = LiveCRM("synthetic-not-a-credential", confirmed=True, allow_writes=True)
        with patch("requests.sessions.Session.request", return_value=response):
            with self.assertRaises(CRMError) as caught:
                live.update_account("DEMO-WILLOW", {"name": "Bellhaven Willow House"})
        self.assertNotIsInstance(caught.exception, UncertainWrite)
        self.assertIn("HTTP 422", str(caught.exception))
        self.assertIn("Unknown field", str(caught.exception))
        self.assertNotIn("synthetic-not-a-credential", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
