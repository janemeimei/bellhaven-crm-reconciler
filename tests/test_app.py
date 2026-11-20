import os
import re
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from bellhaven.actions import action_reason, recommended_action
from bellhaven.pipeline import run_demo
from bellhaven.pipeline import run_pipeline
from bellhaven.crm import CRMError, DemoCRM
from bellhaven.demo import PARENT_ID, demo_accounts, demo_website
from bellhaven.executor import ExecutionError, Executor
from bellhaven.models import fingerprint
from bellhaven.store import Store


class AppTests(unittest.TestCase):
    BACK_LABELS = {"Back to pending cases", "Back to history", "Back to facilities overview"}
    VIEWS = {"pending": "Pending cases", "history": "History", "facilities": "Facilities overview"}

    def overview(self, app, view="pending"):
        for button in app.button:
            if button.label in self.BACK_LABELS:
                button.click().run()
                break
        app.session_state["main-view"] = self.VIEWS[view]
        app.run()
        for button in app.button:
            if button.label in self.BACK_LABELS:
                button.click().run()
                break
        return app

    def open_case(self, app, case_id, view="pending"):
        self.overview(app, view)
        next(button for button in app.button if button.key == f"open-{view}-{case_id}").click().run()
        return app

    def approve_recommended(self, app):
        next(
            item for item in app.checkbox
            if item.label == "I reviewed the evidence and every step of this plan."
        ).check().run()
        next(button for button in app.button if button.key and button.key.startswith("approve-")).click().run()

    @staticmethod
    def highlighted_cells(table):
        frame = table.value
        cells = {(int(row), int(column)) for row, column in re.findall(r"row(\d+)_col(\d+)", table.proto.arrow_data.styler.styles)}
        return sorted((frame.iloc[row]["Field"], frame.columns[column]) for row, column in cells)

    def test_evidence_table_highlights_crm_values_that_differ_from_the_website(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                expected = {
                    "demo:willow": [("Name", "Related CRM accounts")],
                    "demo:cedar": [("Name", "Related CRM accounts"), ("Parent / operator", "Related CRM accounts")],
                }
                for location_key, cells in expected.items():
                    row = next(row for row in store.proposals() if location_key in row["proposal"].location_keys)
                    self.open_case(app, row["id"])
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertEqual(self.highlighted_cells(app.table[0]), cells)

    def test_opening_a_case_scrolls_to_the_top_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                self.assertEqual(len(app.html), 0)
                row = next(row for row in Store(database, "demo-v1").proposals() if row["proposal"].kind == "field_correction")
                self.open_case(app, row["id"])
                self.assertEqual(len(app.html), 1)
                next(item for item in app.text_input if item.label == "Name").set_value("Willow Residence").run()
                self.assertEqual(len(app.html), 0)
                self.assertFalse(app.exception, list(app.exception))

    def test_manual_mapping_in_live_mode_uses_the_configured_bellhaven_parent(self):
        live_parent = "LIVE-BELLHAVEN"
        accounts = [
            {key: live_parent if value == PARENT_ID else value for key, value in account.items()}
            for account in demo_accounts()
        ]
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text("test-token-not-real")
            token_file.chmod(0o600)
            database = Path(directory) / "live.sqlite"
            store = Store(database, "live:" + fingerprint("test-token-not-real"))
            run_pipeline(store, DemoCRM(store, accounts), demo_website(), live_parent)
            relation = next(row for row in store.proposals() if row["proposal"].kind == "relationship_review")
            environment = {
                "BELLHAVEN_MODE": "live", "BELLHAVEN_TOKEN_FILE": str(token_file),
                "BELLHAVEN_PARENT_ID": live_parent, "BELLHAVEN_LIVE_DB": str(database),
            }
            with patch.dict(os.environ, environment), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Preparing a correction must not call the live API."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                self.open_case(app, relation["id"])
                next(area for area in app.text_area if area.label == "Reason for this correction").set_value(
                    "These are separately listed current facilities."
                ).run()
                next(box for box in app.selectbox if box.label == "Account outcome").select("new").run()
                next(button for button in app.button if button.key == "prepare-" + relation["id"]).click().run()
                self.assertFalse(app.exception, list(app.exception))
            manual = next(row for row in store.proposals() if row["proposal"].metadata.get("manual"))
            create = next(step for step in manual["proposal"].steps if step["op"] == "create")
            self.assertEqual(create["payload"]["parent_id"], live_parent)

    def test_local_ui_can_approve_reject_and_refresh_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}):
                with patch(
                    "requests.sessions.Session.request",
                    side_effect=AssertionError("The offline UI must not access the network."),
                ):
                    app = AppTest.from_file(
                        str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                    ).run()
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
                    self.assertEqual([tab.label for tab in app.tabs], ["Pending cases", "History", "Facilities overview"])
                    refreshed = [item.value for item in app.caption if item.value.startswith("Last refreshed:")]
                    self.assertEqual(len(refreshed), 1)
                    self.assertTrue(refreshed[0].endswith("|  Read verification passed"))
                    store = Store(database, "demo-v1")
                    refreshed_at = store.latest_run()["created_at"]
                    next(item for item in app.text_input if item.label == "Reviewer name").set_value("Jane").run()
                    original = next(row for row in store.proposals() if row["proposal"].kind == "field_correction")
                    self.open_case(app, original["id"])
                    self.assertEqual(store.latest_run()["created_at"], refreshed_at)
                    self.assertEqual(app.subheader[0].value, "Field Correction")
                    issue = next(item.value for item in app.markdown if item.value.startswith("**Issue:**"))
                    self.assertIn("Willow Manor", issue)
                    self.assertIn("Bellhaven Willow House", issue)
                    sections = [item.value for item in app.markdown]
                    self.assertLess(sections.index(issue), sections.index("### Evidence"))
                    self.assertLess(sections.index("### Evidence"), sections.index("### Change"))
                    self.assertLess(sections.index("### Change"), sections.index("**Recommended change**"))
                    self.assertEqual([tab.label for tab in app.tabs], ["Pending cases", "History", "Facilities overview"])
                    expected_reason = action_reason(recommended_action(original["proposal"]))
                    self.assertEqual(
                        next(area for area in app.text_area if area.label == "Decision reason").value,
                        expected_reason,
                    )
                    comparison = app.table[0].value.set_index("Field")
                    self.assertEqual(comparison.loc["Name", "Website evidence"], "Bellhaven Willow House")
                    self.assertEqual(comparison.loc["Name", "Related CRM accounts"], "Willow Manor")
                    self.assertEqual(comparison.loc["Street address", "Website evidence"], "101 Example Street")
                    self.assertEqual(comparison.loc["Street address", "Related CRM accounts"], "101 Example St")
                    self.approve_recommended(app)
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
                    change = next(
                        event for event in store.case_activity(original["id"]) if event["event"] == "Account updated"
                    )
                    self.assertEqual(change["actor"], "Jane")
                    self.assertTrue(change["at"])
                    self.assertEqual(
                        change["details"]["changes"]["name"],
                        {"before": "Willow Manor", "after": "Bellhaven Willow House"},
                    )
                    self.overview(app, "history")
                    self.assertEqual(
                        {button.key for button in app.button if button.key and button.key.startswith("open-history-")},
                        {"open-history-" + original["id"]},
                    )
                    self.open_case(app, original["id"], "history")
                    self.assertTrue(any(item.value == "### Case activity" for item in app.markdown))
                    self.assertTrue(next(item for item in app.text_input if item.label == "Name").disabled)
                    self.assertFalse(any(item.value.startswith("**Reason:**") for item in app.markdown))
                    unlisted = next(row for row in store.proposals() if row["proposal"].kind == "unlisted_review")
                    self.open_case(app, unlisted["id"])
                    reject = next(button for button in app.button if button.label == "Reject proposal")
                    self.assertTrue(reject.disabled)
                    next(item for item in app.text_input if item.label == "Rejection reason").set_value(
                        "The facility is temporarily hidden from the website."
                    ).run()
                    next(button for button in app.button if button.label == "Reject proposal").click().run()
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
                    next(button for button in app.button if button.label == "Refresh local reconciliation").click().run()
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
            audit = Store(database, "demo-v1").audit()
            self.assertEqual([item["decision"] for item in audit], ["approved", "rejected"])
            self.assertEqual([item["actor"] for item in audit], ["Jane", "Jane"])
            self.assertEqual(audit[0]["reason"], expected_reason)
            self.assertEqual(audit[1]["reason"], "The facility is temporarily hidden from the website.")

    def test_explicit_relationship_resolution_can_be_prepared_refreshed_and_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}):
                with patch(
                    "requests.sessions.Session.request",
                    side_effect=AssertionError("The offline UI must not access the network."),
                ):
                    app = AppTest.from_file(
                        str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                    ).run()
                    store = Store(database, "demo-v1")
                    relation = next(
                        row for row in store.proposals()
                        if row["proposal"].kind == "relationship_review"
                    )
                    self.open_case(app, relation["id"])
                    accounts_before = len(store.demo_accounts())
                    next(
                        area for area in app.text_area
                        if area.label == "Reason for this correction"
                    ).set_value("These are separately listed current facilities.").run()
                    self.assertTrue(next(button for button in app.button if button.key == "prepare-" + relation["id"]).disabled)
                    next(box for box in app.selectbox if box.label == "Account outcome").select("new").run()
                    next(button for button in app.button if button.key == "prepare-" + relation["id"]).click().run()
                    next(button for button in app.button if button.label == "Refresh local reconciliation").click().run()
                    manual = next(
                        row for row in store.proposals()
                        if row["active"] and row["proposal"].metadata.get("manual")
                    )
                    self.open_case(app, manual["id"])
                    self.approve_recommended(app)
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
                    self.assertEqual(store.get_proposal(manual["id"])["execution"], "applied")
                    self.assertEqual(len(store.demo_accounts()), accounts_before + 1)

    def test_facilities_overview_is_separate_from_cases_and_shows_current_crm_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                self.assertEqual(len(app.main.dataframe), 0)
                self.assertFalse(any("Classification" in item.label for item in app.expander))
                self.overview(app, "facilities")
                self.assertEqual(app.subheader[0].value, "Facilities overview")
                metrics = {item.label: item.value for item in app.metric}
                self.assertEqual(metrics, {"Website facilities": "13", "Pending review": "12", "Reviewed": "0"})
                facilities = app.main.dataframe[0].value
                names = list(facilities["Website facility"])
                harbor = facilities.set_index("Website facility").loc["Bellhaven Harbor House"]
                self.assertEqual(harbor["CRM account"], "Bellhaven Harbor House [DEMO-HARBOR]")
                self.assertEqual(harbor["CRM parent"], "Bellhaven Senior Living")
                self.assertEqual(harbor["Match result"], "Confirmed match")
                self.assertEqual(harbor["Review status"], "No change needed")
                self.assertEqual(harbor["Case"], "")
                unlisted = app.main.dataframe[1].value.iloc[0]
                self.assertEqual(unlisted["CRM account"], "Former Bellhaven Elm House [DEMO-UNLISTED]")
                self.assertEqual(unlisted["Review status"], "Pending review")

                willow = next(row for row in store.proposals() if "demo:willow" in row["proposal"].location_keys)
                app.session_state["overview-facilities"] = {
                    "selection": {"rows": [names.index("Bellhaven Willow House")], "columns": []},
                }
                app.run()
                willow_case = facilities.set_index("Website facility").loc["Bellhaven Willow House", "Case"]
                next(button for button in app.button if button.label == "Open " + willow_case).click().run()
                self.assertEqual(app.session_state["main-view"], "Pending cases")
                self.assertEqual(app.subheader[0].value, "Field Correction")
                self.approve_recommended(app)
                self.assertFalse(app.exception, list(app.exception))
                self.assertEqual(store.get_proposal(willow["id"])["execution"], "applied")
                next(button for button in app.button if button.label == "Back to facilities overview").click().run()
                self.assertEqual(app.session_state["main-view"], "Facilities overview")
                current = app.main.dataframe[0].value.set_index("Website facility").loc["Bellhaven Willow House"]
                self.assertEqual(current["CRM account"], "Bellhaven Willow House [DEMO-WILLOW]")
                self.assertEqual(current["Match result"], "Confirmed match")
                self.assertEqual(current["Review status"], "Approved - applied to CRM")
                self.assertEqual(current["Case"], willow_case)

    def test_live_mode_is_read_only_and_renders_from_the_snapshot_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text("test-token-not-real")
            token_file.chmod(0o600)
            database = Path(directory) / "live.sqlite"
            store = Store(database, "live:" + fingerprint("test-token-not-real"))
            run_pipeline(store, DemoCRM(store, demo_accounts()), demo_website(), PARENT_ID)
            environment = {
                "BELLHAVEN_MODE": "live", "BELLHAVEN_TOKEN_FILE": str(token_file),
                "BELLHAVEN_PARENT_ID": PARENT_ID, "BELLHAVEN_LIVE_DB": str(database),
            }
            with patch.dict(os.environ, environment), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Rendering must not call the website or the live API."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                self.assertFalse(app.exception, list(app.exception))
                self.assertFalse(app.error, list(app.error))
                row = next(row for row in store.proposals() if row["proposal"].kind == "field_correction")
                self.open_case(app, row["id"])
                approve = next(button for button in app.button if button.key == "approve-" + row["id"])
                self.assertEqual(approve.label, "Approve and write to CRM")
                self.assertTrue(approve.disabled)
                self.assertTrue(any("approval is locked" in item.value for item in app.info))
                self.overview(app, "facilities")
                self.assertFalse(app.exception, list(app.exception))
                self.assertEqual(len(app.main.dataframe), 2)
                self.assertEqual(store.audit(), [])

    def test_manual_duplicate_tool_prepares_a_case(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                unlisted = next(row for row in store.proposals() if row["proposal"].kind == "unlisted_review")
                self.open_case(app, unlisted["id"])
                before = store.demo_accounts()
                app.radio[0].set_value("Mark an account as a duplicate").run()
                next(box for box in app.selectbox if box.label == "Duplicate copy").select("DEMO-UNLISTED").run()
                next(box for box in app.selectbox if box.label == "Surviving account").select("DEMO-WILLOW").run()
                next(area for area in app.text_area if area.label == "Reason for this correction").set_value(
                    "Same facility under an older name."
                ).run()
                next(button for button in app.button if button.key == "prepare-" + unlisted["id"]).click().run()
                self.assertFalse(app.exception, list(app.exception))
                self.assertFalse(app.error, list(app.error))
                prepared = next(
                    row for row in store.proposals() if row["proposal"].metadata.get("manual_type") == "duplicate"
                )
                self.assertEqual(prepared["decision"], "pending")
                self.assertEqual(prepared["proposal"].steps[0]["payload"]["duplicate_of_account"], "DEMO-WILLOW")
                self.assertEqual(store.demo_accounts(), before)
                self.assertEqual(store.audit(), [])

    def test_comparison_shows_all_crm_records_without_a_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                for account in store.demo_accounts():
                    if account["account_id"] in {"DEMO-ORCHARD-A", "DEMO-ORCHARD-B"}:
                        account["phone"] = "555-0101" if account["account_id"].endswith("-A") else "555-0102"
                        store.put_demo_account(account)
                run_demo(store)
                duplicate = next(
                    row for row in store.proposals() if row["proposal"].kind == "duplicate"
                )
                self.open_case(app, duplicate["id"])
                comparison = app.table[0].value.set_index("Field")
                columns = {
                    account_id: next(column for column in comparison.columns if f"[{account_id}]" in column)
                    for account_id in ("DEMO-ORCHARD-A", "DEMO-ORCHARD-B")
                }
                self.assertNotIn("Phone", comparison.index)
                self.assertFalse(any(box.label == "CRM record" for box in app.selectbox))
                financials = app.table[1].value.set_index("CRM account ID")
                self.assertEqual(financials.loc["DEMO-ORCHARD-B", "Lifetime revenue"], "0")
                self.assertEqual(financials.loc["DEMO-ORCHARD-A", "Lifetime revenue"], "4000")
                self.assertFalse(app.exception, list(app.exception))

    def test_relationship_review_explains_the_two_to_one_mapping_and_flags_needs_review(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                row = next(row for row in store.proposals() if row["proposal"].kind == "relationship_review")
                before = store.demo_accounts()
                self.open_case(app, row["id"])
                issue = next(item.value for item in app.markdown if item.value.startswith("**Issue:**"))
                for text in ("Bellhaven Campus North", "Bellhaven Campus South", "Former Campus Center", "606 Campus Way"):
                    self.assertIn(text, issue)
                comparison = app.table[0].value.set_index("Field")
                self.assertEqual(comparison.loc["Care offerings", "Website 1: Bellhaven Campus North"], "Assisted Living")
                self.assertEqual(comparison.loc["Care offerings", "Website 2: Bellhaven Campus South"], "Memory Support")
                self.assertEqual(comparison.loc["Care offerings", "Related CRM accounts"], "Assisted Living; Memory Support")
                self.assertFalse(any(box.label == "Website record" for box in app.selectbox))
                self.assertTrue(any("Changes: Account status, Note." in item.value for item in app.markdown))
                self.assertTrue(any(item.value == "Changes from Active to Needs Review." for item in app.caption))
                self.assertTrue(any("does not settle the facility-to-account mapping" in item.value for item in app.info))
                note = next(area.value for area in app.text_area if area.label == "Note")
                self.assertIn("Bellhaven Campus North", note)
                self.assertIn("Bellhaven Campus South", note)
                next(box for box in app.selectbox if box.label == "Account status").select("Active").run()
                self.assertTrue(any("Changes: Note only." in item.value for item in app.markdown))
                self.assertIn("**Custom change**", [item.value for item in app.markdown])
                self.assertEqual(store.demo_accounts(), before)
                self.assertEqual(store.audit(), [])
                self.assertFalse(app.exception, list(app.exception))

    def test_every_case_has_an_issue_summary_and_website_comparison_excludes_financials(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                for row in Store(database, "demo-v1").proposals():
                    with self.subTest(kind=row["proposal"].kind):
                        self.open_case(app, row["id"])
                        issues = [item.value for item in app.markdown if item.value.startswith("**Issue:**")]
                        self.assertEqual(len(issues), 1)
                        self.assertGreater(len(issues[0]), len("**Issue:** "))
                        self.assertFalse(app.main.get("dataframe"))
                        self.assertFalse(any(item.value.startswith("**Reason:**") for item in app.markdown))
                        fields = set(app.table[0].value["Field"])
                        self.assertFalse(fields.intersection({"Lifetime revenue", "Outstanding AR", "Account status"}))
                        if row["proposal"].evidence.get("accounts"):
                            self.assertIn("Lifetime revenue", app.table[1].value.columns)
                            self.assertIn("Outstanding AR", app.table[1].value.columns)
                        if row["proposal"].kind == "chow":
                            link = next(item for item in app.text_input if item.label == "Current account after change of ownership")
                            self.assertTrue(link.disabled)
                            self.assertEqual(link.value, "New current account")
                        if row["proposal"].kind == "chow_link":
                            link = next(item for item in app.text_input if item.label == "Current account after change of ownership")
                            self.assertTrue(link.disabled)
                            self.assertEqual(link.value, "DEMO-MAPLE")
                        if row["proposal"].kind == "duplicate":
                            link = next(item for item in app.text_input if item.label == "Duplicate of account")
                            self.assertTrue(link.disabled)
                            survivor = next(
                                step["payload"]["duplicate_of_account"] for step in row["proposal"].steps
                                if step["op"] == "patch" and step["payload"].get("duplicate_of_account")
                            )
                            self.assertEqual(link.value, survivor)
                            self.assertTrue(next(item for item in app.text_input if item.label == "Account status").disabled)
                        self.assertFalse(app.exception, list(app.exception))
                        self.assertFalse(app.error, list(app.error))

    def test_inline_edits_detect_custom_changes_and_preserve_the_case_id(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                original = next(row for row in store.proposals() if row["proposal"].kind == "field_correction")
                self.open_case(app, original["id"])
                before = store.demo_accounts()
                reason = action_reason(recommended_action(original["proposal"]))
                self.assertEqual(next(box for box in app.selectbox if box.label == "Account status (optional)").value, "Active")
                next(item for item in app.text_input if item.label == "Name").set_value("Willow Residence").run()
                next(box for box in app.selectbox if box.label == "Account status (optional)").select("Needs Review").run()
                self.assertEqual([tab.label for tab in app.tabs], ["Pending cases", "History", "Facilities overview"])
                self.assertIn("**Custom change**", [item.value for item in app.markdown])
                self.assertEqual(next(area for area in app.text_area if area.label == "Decision reason").value, reason)
                self.assertEqual(store.demo_accounts(), before)
                self.assertEqual(store.audit(), [])
                next(area for area in app.text_area if area.label == "Decision reason").set_value("").run()
                approve = next(button for button in app.button if button.label == "Approve and apply locally")
                self.assertTrue(approve.disabled)
                next(
                    area for area in app.text_area if area.label == "Decision reason"
                ).set_value("The reviewer confirmed the preferred current name.").run()
                next(
                    item for item in app.checkbox
                    if item.label == "I reviewed the evidence and every step of this plan."
                ).check().run()
                next(item for item in app.text_input if item.label == "Name").set_value("Willow Current Residence").run()
                self.assertFalse(next(
                    item for item in app.checkbox
                    if item.label == "I reviewed the evidence and every step of this plan."
                ).value)
                self.approve_recommended(app)
                self.assertFalse(app.exception, list(app.exception))
                self.assertFalse(app.error, list(app.error))
                current = store.get_proposal(original["id"])
                self.assertEqual(current["case_number"], original["case_number"])
                self.assertEqual(current["created_at"], original["created_at"])
                self.assertEqual(current["execution"], "applied")
                self.assertEqual(store.audit()[0]["reason"], "The reviewer confirmed the preferred current name.")
                account = next(row for row in store.demo_accounts() if row["account_id"] == "DEMO-WILLOW")
                self.assertEqual(account["name"], "Willow Current Residence")
                self.assertEqual(account["status"], "Needs Review")
                self.assertEqual(current["proposal"].metadata["action_type"], "custom")

    def test_restoring_inline_values_returns_to_the_recommendation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                row = next(row for row in store.proposals() if row["proposal"].kind == "field_correction")
                self.open_case(app, row["id"])
                recommended_name = next(item for item in app.text_input if item.label == "Name").value
                reason = next(area for area in app.text_area if area.label == "Decision reason").value
                next(item for item in app.text_input if item.label == "Name").set_value("Reviewer draft").run()
                self.assertIn("**Custom change**", [item.value for item in app.markdown])
                next(item for item in app.text_input if item.label == "Name").set_value("").run()
                self.assertTrue(any("Name cannot be empty." in item.value for item in app.error))
                self.assertTrue(next(button for button in app.button if button.label == "Approve and apply locally").disabled)
                self.assertEqual(store.audit(), [])
                next(item for item in app.text_input if item.label == "Name").set_value(recommended_name).run()
                self.assertIn("**Recommended change**", [item.value for item in app.markdown])
                self.assertNotIn("**Custom change**", [item.value for item in app.markdown])
                self.assertEqual(next(area for area in app.text_area if area.label == "Decision reason").value, reason)
                self.approve_recommended(app)
                self.assertFalse(app.exception, list(app.exception))
                self.assertFalse(app.error, list(app.error))
                self.assertNotEqual(store.get_proposal(row["id"])["proposal"].metadata.get("action_type"), "custom")
                self.assertEqual(store.audit()[0]["reason"], reason)

    def test_local_refresh_is_not_a_crm_change_or_a_reviewed_history_case(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
                store = Store(database, "demo-v1")
                website = demo_website()
                website = replace(
                    website, locations=tuple(
                        replace(location, phone="555-0199") if location.key == "demo:willow" else location
                        for location in website.locations
                    ),
                )
                run_pipeline(store, DemoCRM(store), website, PARENT_ID)
                self.overview(app)
                self.assertFalse(any(item.value.startswith("Last activity:") for item in app.caption))
                self.assertTrue(any(item.value.startswith("Proposal refreshed (local only):") for item in app.caption))
                self.assertFalse(any(item.value.startswith("Last approved CRM change:") for item in app.caption))
                row = next(row for row in store.proposals() if row["proposal"].kind == "field_correction")
                self.open_case(app, row["id"])
                self.assertTrue(any(item.value == "No verified CRM changes recorded." for item in app.info))
                self.assertTrue(any(item.label == "Local proposal activity - no CRM writes" for item in app.expander))
                self.overview(app, "history")
                self.assertFalse(any(
                    button.key and button.key.startswith("open-history-") for button in app.button
                ))
                self.assertTrue(any(item.value == "No history yet." for item in app.info))
                self.assertFalse(app.exception, list(app.exception))
                self.assertFalse(app.error, list(app.error))

    def test_pending_cards_show_a_status_only_when_an_approved_write_needs_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            store = Store(database, "demo-v1")
            run_demo(store)
            row = next(row for row in store.proposals() if row["proposal"].kind == "field_correction")
            store.decide(row["id"], row["plan_hash"], "approved", "Website name is current.", actor="Jane")
            with patch.object(DemoCRM, "update_account", side_effect=CRMError("CRM unavailable")):
                with self.assertRaises(ExecutionError):
                    Executor(store, DemoCRM(store)).apply(row["id"], actor="Jane")
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}), patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("The offline UI must not access the network."),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20,
                ).run()
            self.assertFalse(app.exception, list(app.exception))
            cards = [item.value for item in app.markdown if item.value.startswith("**CASE-")]
            flagged = [card for card in cards if "Approved - CRM write failed" in card]
            self.assertEqual(len(flagged), 1)
            self.assertGreater(len(cards), 1)
            self.assertFalse(any("|" in card for card in cards if card not in flagged))


if __name__ == "__main__":
    unittest.main()
