import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from bellhaven.store import Store


class AppTests(unittest.TestCase):
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
                    app.checkbox[1].check().run()
                    next(button for button in app.button if button.label == "Approve and apply locally").click().run()
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
                    next(button for button in app.button if button.label == "Reject proposal").click().run()
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
                    next(button for button in app.button if button.label == "Refresh local reconciliation").click().run()
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
            audit = Store(database, "demo-v1").audit()
            self.assertEqual([item["decision"] for item in audit], ["approved", "rejected"])

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
                    app.selectbox[0].select(relation["id"]).run()
                    next(button for button in app.button if button.label == "Prepare resolution for review").click().run()
                    next(button for button in app.button if button.label == "Refresh local reconciliation").click().run()
                    manual = next(
                        row for row in store.proposals()
                        if row["active"] and row["proposal"].metadata.get("manual")
                    )
                    app.selectbox[0].select(manual["id"]).run()
                    app.checkbox[1].check().run()
                    next(button for button in app.button if button.label == "Approve and apply locally").click().run()
                    self.assertFalse(app.exception, list(app.exception))
                    self.assertFalse(app.error, list(app.error))
                    self.assertEqual(store.get_proposal(manual["id"])["execution"], "applied")
                    self.assertEqual(len(store.demo_accounts()), 13)


if __name__ == "__main__":
    unittest.main()
