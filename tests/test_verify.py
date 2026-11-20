import copy
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from streamlit.testing.v1 import AppTest

from bellhaven.crm import LiveCRM
from bellhaven.demo import PARENT_ID, account
from bellhaven.models import BASE_URL
from bellhaven.pipeline import run_demo
from bellhaven.reconcile import reconcile
from bellhaven.scrape import scrape_website
from bellhaven.store import Store
from bellhaven.verify import FAIL, PASS, WARN, check, verify_crm, verify_matching, verify_website


def page(body):
    return (
        '<html><body><nav><a href="/">Home</a><a href="/communities">Our Communities</a>'
        f'<a href="/about">About Us</a></nav><div class="wrap">{body}</div></body></html>'
    )


def badges(care):
    return "".join(f'<span class="badge">{item}</span>' for item in care)


def detail(name, street, locality, care, phone):
    return page(
        f'<h1>{name}</h1><dl class="detail"><dt>Address</dt><dd>{street}<br>{locality}</dd>'
        f"<dt>Care Offerings</dt><dd>{badges(care)}</dd><dt>Phone</dt><dd>{phone}</dd></dl>"
    )


def card(slug, name, city, care):
    return (
        f'<div class="card"><h3><a href="/communities/{slug}">{name}</a></h3>'
        f'<div class="city">{city}</div><div>{badges(care)}</div></div>'
    )


def site():
    return {
        "/": page('<h1>Bellhaven</h1><a href="/communities/meadows">Bellhaven Meadows</a>'),
        "/about": page("<p>About Bellhaven.</p>"),
        "/communities": page(
            '<h1>Our Communities</h1><p>Page 1 of 1 &middot; 2 communities listed</p><div class="cardgrid">'
            + card("alpha", "Bellhaven of Alpha", "Alpha, OH", ["Assisted Living"])
            + card("beta", "Bellhaven of Beta", "Beta, OH", ["Assisted Living", "Memory Support"])
            + "</div>"
        ),
        "/communities/alpha": detail("Bellhaven of Alpha", "1 Main St", "Alpha, OH 43001", ["Assisted Living"], "(111) 111-1111"),
        "/communities/beta": detail(
            "Bellhaven of Beta", "2 Oak Ave", "Beta, OH 43002", ["Assisted Living", "Memory Support"], "(222) 222-2222",
        ),
        "/communities/meadows": detail(
            "Bellhaven Meadows", "3 Elm Rd", "Gamma, OH 43003", ["Short-Term Rehabilitation &amp; Nursing"], "(333) 333-3333",
        ),
    }


def fetcher(pages):
    def fetch(url):
        parts = urlsplit(url)
        return pages[(parts.path or "/") + ("?" + parts.query if parts.query else "")]
    return fetch


def results(verification_checks):
    return {item["check"]: item["result"] for item in verification_checks}


class FakeCRMServer:
    def __init__(self, accounts):
        self.accounts = {row["account_id"]: dict(row) for row in accounts}

    def __call__(self, method, url, json=None, params=None, **kwargs):
        if method != "GET":
            raise AssertionError("Verification must never write.")
        path = url.split("/api/v1", 1)[1]
        if path == "/accounts":
            rows = [
                row for row in self.accounts.values()
                if "parent_id" not in params or row["parent_id"] == params["parent_id"]
            ]
            page_number, size = params["page"], params["page_size"]
            body = {
                "data": rows[(page_number - 1) * size:page_number * size],
                "page": page_number, "page_size": size, "total": len(rows),
            }
        else:
            body = self.accounts[path.rsplit("/", 1)[1]]
        return type("Response", (), {"status_code": 200, "json": lambda self: copy.deepcopy(body)})()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.pages = site()
        self.website = scrape_website(fetcher(self.pages))
        self.assertTrue(self.website.complete, self.website.errors)
        self.accounts = [
            account(PARENT_ID, "Bellhaven Senior Living", parent=""),
            account("ALPHA", "Bellhaven of Alpha", "1 Main Street"),
            account("BETA", "Bellhaven of Beta", "2 Oak Avenue"),
        ]
        for row, (city, postal) in zip(self.accounts[1:], (("Alpha", "43001"), ("Beta", "43002"))):
            row.update(billing_city=city, billing_zip=postal)

    def test_clean_reads_pass_every_check(self):
        website = verify_website(self.website, fetcher(self.pages))["checks"]
        self.assertFalse([item for item in website if item["result"] in (FAIL, WARN)], website)
        with patch("requests.sessions.Session.request", new=FakeCRMServer(self.accounts)):
            crm = verify_crm(self.accounts, LiveCRM("synthetic-not-a-credential", confirmed=True), PARENT_ID)
        self.assertEqual({item["result"] for item in crm}, {PASS})

    def test_tampered_website_reads_are_detected(self):
        tampered = replace(self.website, locations=tuple(
            replace(location, zip="43999") if location.name == "Bellhaven of Alpha" else location
            for location in self.website.locations if location.name != "Bellhaven Meadows"
        ))
        outcome = results(verify_website(tampered, fetcher(self.pages))["checks"])
        self.assertEqual(outcome["Every scraped field matches an independent parse of its page"], FAIL)
        self.assertEqual(outcome["Independent crawl finds exactly the scraped communities"], FAIL)
        pages = dict(self.pages)
        pages["/communities"] = pages["/communities"].replace("Beta, OH</div>", "Delta, OH</div>")
        pages["/about"] = page('<a href="/news">News</a>')
        pages["/news"] = page('<a href="/communities/hidden">Our newest community</a>')
        pages["/communities/hidden"] = detail("Bellhaven Hidden", "9 Pine St", "Zeta, OH 43009", ["Assisted Living"], "")
        outcome = results(verify_website(scrape_website(fetcher(pages)), fetcher(pages))["checks"])
        self.assertEqual(outcome["Directory cards agree with community pages"], FAIL)
        self.assertEqual(outcome["Independent crawl finds exactly the scraped communities"], FAIL)

    def test_tampered_crm_reads_are_detected(self):
        listed = copy.deepcopy(self.accounts)
        listed[1]["name"] = "Misread Name"
        del listed[2]
        with patch("requests.sessions.Session.request", new=FakeCRMServer(self.accounts)):
            outcome = results(verify_crm(listed, LiveCRM("synthetic-not-a-credential", confirmed=True), PARENT_ID))
        self.assertEqual(outcome["Server-side parent filter returns the same Bellhaven accounts"], FAIL)
        self.assertEqual(outcome["Every account read individually equals the full read"], FAIL)

    def test_matching_invariants_and_loose_misses(self):
        proposals, summary = reconcile(self.website, self.accounts, PARENT_ID)
        self.assertEqual(
            {item["result"] for item in verify_matching(self.website, self.accounts, summary, PARENT_ID)} - {"INFO"},
            {PASS},
        )
        broken = copy.deepcopy(summary)
        broken["locations"] = broken["locations"][1:]
        broken["unlisted"].append({"account_id": "ELSEWHERE", "name": "", "classification": "", "proposal_key": None})
        stray = account("STRAY", "Bellhaven of Alpha Annex", "1 Main Street", parent="OTHER")
        stray.update(billing_city="Alpha", billing_zip="43001")
        outcome = results(verify_matching(self.website, self.accounts + [stray], broken, PARENT_ID))
        self.assertEqual(outcome["Every website facility is classified exactly once"], FAIL)
        self.assertEqual(outcome["Every current Bellhaven account is matched or flagged"], FAIL)
        outcome = results(verify_matching(self.website, self.accounts + [stray], summary, PARENT_ID))
        self.assertEqual(outcome["No account at a facility's address was left out of matching"], WARN)

    def test_failed_verification_alerts_and_blocks_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite"
            failure = [check("Matching", "Every website facility is classified exactly once", FAIL, "1 missing")]
            with patch("bellhaven.verify.verify_matching", return_value=failure):
                run_demo(Store(database, "demo-v1"))
            store = Store(database, "demo-v1")
            row = next(row for row in store.proposals() if row["proposal"].kind == "field_correction")
            with patch.dict(os.environ, {"BELLHAVEN_DEMO_DB": str(database)}):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
                self.assertTrue(any("Read verification failed" in item.value for item in app.error))
                app.session_state["detail-pending"] = row["id"]
                app.run()
                approve = next(button for button in app.button if button.key == "approve-" + row["id"])
                self.assertTrue(approve.disabled)
                self.assertTrue(any(item.value.startswith("Approval is blocked") for item in app.error))


if __name__ == "__main__":
    unittest.main()
