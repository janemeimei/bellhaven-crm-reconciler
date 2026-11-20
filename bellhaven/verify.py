"""Independent read verification that runs with every reconciliation refresh.

Each check re-derives a fact a different way from the main pipeline: a second crawler and a
regex parser for the website, separate API read paths for the CRM, and invariants plus a looser
address scan for matching. Nothing here writes to the CRM or the website.
"""
from __future__ import annotations

import html
import re
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from bellhaven.crm import CRM, CRMError
from bellhaven.domain import Account, identity_name, normalize, street_key, zip_key
from bellhaven.models import BASE_URL, WebsiteSnapshot, utcnow


PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"
DETAIL_PATH = re.compile(r"/communities/[A-Za-z0-9_-]+")
PAGE_LIMIT = 300


class VerificationError(RuntimeError):
    pass


def check(area: str, name: str, result: str, detail: str = "") -> dict[str, str]:
    return {"area": area, "check": name, "result": result, "detail": detail}


def overall(checks: list[dict[str, str]]) -> str:
    results = {item["result"] for item in checks}
    return "fail" if FAIL in results else "warn" if WARN in results else "pass"


def plain(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def fetch_site_page(url: str) -> str:
    parts = urlsplit(url)
    if parts.netloc != urlsplit(BASE_URL).netloc or parts.path.startswith(("/api", "/crm")):
        raise VerificationError("refusing to read outside the public website")
    try:
        response = requests.get(url, timeout=(5, 25), allow_redirects=False)
    except requests.RequestException as exc:
        raise VerificationError(f"request failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise VerificationError(f"HTTP {response.status_code}")
    if "text/html" not in response.headers.get("Content-Type", "").lower():
        raise VerificationError("not an HTML page")
    return response.text


def linked_paths(page: str, current_url: str) -> set[str]:
    origin = urlsplit(BASE_URL).netloc
    paths = set()
    for href in re.findall(r"""href\s*=\s*["']([^"']+)["']""", page, re.I):
        parts = urlsplit(urljoin(current_url, html.unescape(href)))
        if parts.scheme not in ("http", "https") or parts.netloc != origin:
            continue
        if parts.path.startswith(("/api", "/crm")) or re.search(r"\.[A-Za-z0-9]{2,5}$", parts.path):
            continue
        query = "" if parse_qs(parts.query) == {"page": ["1"]} else parts.query
        paths.add((parts.path.rstrip("/") or "/") + ("?" + query if query else ""))
    return paths


def definition(page: str, label: str) -> str | None:
    match = re.search(rf"<dt[^>]*>\s*{label}\s*</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.S | re.I)
    return match[1] if match else None


def badges(fragment: str) -> list[str]:
    return [
        plain(item) for item in
        re.findall(r"""<[^>]*class\s*=\s*["'][^"']*\bbadge\b[^"']*["'][^>]*>(.*?)</""", fragment, re.S | re.I)
    ]


def parse_detail(page: str) -> dict[str, Any]:
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    address = definition(page, "Address") or ""
    lines = [line for line in (plain(part) for part in re.split(r"<br\s*/?>", address, flags=re.I)) if line]
    locality = re.fullmatch(r"(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", lines[-1]) if lines else None
    phone = definition(page, "Phone")
    return {
        "name": plain(heading[1]) if heading else "", "address": ", ".join(lines[:-1]),
        "city": locality[1] if locality else "", "state": locality[2] if locality else "",
        "zip": locality[3] if locality else "", "care_offerings": badges(definition(page, "Care Offerings") or ""),
        "phone": plain(phone) if phone else "",
    }


def parse_directory(page: str) -> tuple[tuple[int, ...] | None, dict[str, dict[str, Any]]]:
    totals = re.search(r"Page\s+(\d+)\s+of\s+(\d+)\D+?(\d+)\s+communities\s+listed", plain(page), re.I)
    cards = {}
    for block in re.split(r"""<div[^>]*class\s*=\s*["'][^"']*\bcard\b[^"']*["'][^>]*>""", page, flags=re.I)[1:]:
        link = re.search(r"""<a[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", block, re.S | re.I)
        if not link:
            continue
        city = re.search(r"""<div[^>]*class\s*=\s*["'][^"']*\bcity\b[^"']*["'][^>]*>(.*?)</div>""", block, re.S | re.I)
        path = urlsplit(urljoin(BASE_URL + "/communities", html.unescape(link[1]))).path.rstrip("/")
        cards[path] = {"name": plain(link[2]), "city": plain(city[1]) if city else None, "care": badges(block) or None}
    return (tuple(int(value) for value in totals.groups()) if totals else None), cards


def listing(values: list[str], limit: int = 8) -> str:
    return "; ".join(values[:limit]) + (f"; and {len(values) - limit} more" if len(values) > limit else "")


def verify_website(snapshot: WebsiteSnapshot, fetch: Callable[[str], str] = fetch_site_page) -> dict[str, Any]:
    area, checks = "Website", []
    checks.append(check(
        area, "Main crawl finished without errors", WARN if snapshot.errors else PASS,
        listing(list(snapshot.errors)) or f"{len(snapshot.locations)} communities scraped",
    ))
    pending, visited, pages, failures = deque(["/"]), set(), {}, []
    while pending:
        path = pending.popleft()
        if path in visited:
            continue
        if len(visited) >= PAGE_LIMIT:
            failures.append(f"stopped at the {PAGE_LIMIT}-page safety limit")
            break
        visited.add(path)
        try:
            pages[path] = fetch(BASE_URL + path)
        except Exception as exc:  # every unreadable page is reported, never skipped silently
            failures.append(f"{path}: {exc}")
            continue
        pending.extend(sorted(linked_paths(pages[path], BASE_URL + path) - visited))
    checks.append(check(
        area, "Independent crawl read every linked page", FAIL if failures else PASS,
        listing(failures) or f"{len(pages)} pages read",
    ))
    stored = {location.key: location for location in snapshot.locations}
    linked = {path for path in visited if DETAIL_PATH.fullmatch(path)}
    missing, extra = sorted(linked - set(stored)), sorted(set(stored) - linked)
    checks.append(check(
        area, "Independent crawl finds exactly the scraped communities", FAIL if missing or extra else PASS,
        f"independent {len(linked)}, scraped {len(stored)}"
        + (f"; not scraped: {listing(missing)}" if missing else "") + (f"; not linked: {listing(extra)}" if extra else ""),
    ))
    other = sorted(path for path in pages if path not in ("/", "/about") and not path.startswith("/communities"))
    checks.append(check(area, "Other site sections crawled for community links", INFO, listing(other) or "none"))
    directory = [parse_directory(page) for path, page in pages.items() if path.split("?")[0] == "/communities"]
    summaries = {totals for totals, _ in directory if totals}
    cards = {path: card for _, page_cards in directory for path, card in page_cards.items()}
    declared = {(totals[1], totals[2]) for totals in summaries}
    if len(declared) == 1:
        page_total, count = next(iter(declared))
        numbers = {totals[0] for totals in summaries}
        ok = numbers == set(range(1, page_total + 1)) and len(cards) == count
        detail = f"declared {page_total} pages and {count} communities; read pages {sorted(numbers)} with {len(cards)} cards"
    else:
        ok, detail = False, f"directory totals missing or inconsistent: {sorted(declared)}"
    checks.append(check(area, "Directory pages and cards match the site's declared totals", PASS if ok else FAIL, detail))
    checks.append(check(area, "Communities outside the directory", INFO, listing(sorted(linked - set(cards))) or "none"))
    mismatches = []
    for path in sorted(set(pages) & set(stored)):
        location = stored[path]
        expected = {
            "name": location.name, "address": location.address, "city": location.city, "state": location.state,
            "zip": location.zip, "care_offerings": list(location.care_offerings), "phone": location.phone,
        }
        for field, value in parse_detail(pages[path]).items():
            if value != expected[field]:
                mismatches.append(f"{path} {field}: page {value!r}, scraped {expected[field]!r}")
    checks.append(check(
        area, "Every scraped field matches an independent parse of its page", FAIL if mismatches else PASS,
        listing(mismatches) or f"{len(set(pages) & set(stored))} communities x 7 fields",
    ))
    disagreements = []
    for path, card in sorted(cards.items()):
        if path not in pages:
            continue
        page_view = parse_detail(pages[path])
        if card["name"] != page_view["name"]:
            disagreements.append(f"{path} name: card {card['name']!r}, page {page_view['name']!r}")
        if card["city"] is not None and card["city"] != f"{page_view['city']}, {page_view['state']}":
            disagreements.append(f"{path} city: card {card['city']!r}, page {page_view['city']}, {page_view['state']}")
        if card["care"] is not None and card["care"] != page_view["care_offerings"]:
            disagreements.append(f"{path} care: card {card['care']}, page {page_view['care_offerings']}")
    checks.append(check(
        area, "Directory cards agree with community pages", FAIL if disagreements else PASS,
        listing(disagreements) or f"{len(cards)} cards",
    ))
    return {"checked_at": utcnow(), "checks": checks}


def verify_crm(accounts: list[Account], crm: CRM, parent_id: str) -> list[dict[str, str]]:
    area = "CRM"
    if not crm.is_live:
        return [check(area, "Independent CRM re-reads", SKIP, "The synthetic local CRM has no separate server to compare against.")]
    by_id = {account["account_id"]: account for account in accounts}
    local = {account_id for account_id, account in by_id.items() if account["parent_id"] == parent_id}
    checks = []
    try:
        server = [account["account_id"] for account in crm.list_children(parent_id)]
    except CRMError as exc:
        checks.append(check(area, "Server-side parent filter returns the same Bellhaven accounts", FAIL, str(exc)))
    else:
        difference = sorted(set(server) ^ local)
        checks.append(check(
            area, "Server-side parent filter returns the same Bellhaven accounts",
            FAIL if difference or len(server) != len(set(server)) else PASS,
            f"server {len(server)}, full read {len(local)}" + (f"; differ: {listing(difference)}" if difference else ""),
        ))
    problems = []
    for account_id, listed in by_id.items():
        try:
            single = crm.get_account(account_id)
        except CRMError as exc:
            problems.append(f"{account_id}: {exc}")
            continue
        if single != listed:
            fields = sorted(key for key in single.keys() | listed.keys() if single.get(key) != listed.get(key))
            problems.append(f"{account_id}: {', '.join(fields)}")
    checks.append(check(
        area, "Every account read individually equals the full read", FAIL if problems else PASS,
        listing(problems) or f"{len(by_id)} accounts",
    ))
    return checks


def house_number(value: Any) -> str:
    match = re.match(r"\s*(\d+)", str(value or ""))
    return match[1] if match else ""


def street_words(value: Any) -> set[str]:
    return {word for word in street_key(value).split() if len(word) >= 4 and not word.isdigit()}


def verify_matching(
    website: WebsiteSnapshot, accounts: list[Account], summary: dict[str, Any], parent_id: str,
) -> list[dict[str, str]]:
    area, checks = "Matching", []
    rows = summary.get("locations") or []
    classified = sorted(row["key"] for row in rows)
    expected = sorted(location.key for location in website.locations)
    checks.append(check(
        area, "Every website facility is classified exactly once", PASS if classified == expected else FAIL,
        f"{len(classified)} classified, {len(expected)} on the website",
    ))

    def historical(account: Account) -> bool:
        return bool(account.get("chow_current_account") or account.get("duplicate_of_account"))

    if website.complete:
        covered = {account_id for row in rows for account_id in row["account_ids"]}
        covered.update(row["account_id"] for row in summary.get("unlisted", []))
        current = [
            account["account_id"] for account in accounts
            if account["parent_id"] == parent_id and account.get("status") != "Inactive" and not historical(account)
        ]
        uncovered = sorted(account_id for account_id in current if account_id not in covered)
        checks.append(check(
            area, "Every current Bellhaven account is matched or flagged", FAIL if uncovered else PASS,
            f"not accounted for: {listing(uncovered)}" if uncovered else f"{len(current)} accounts",
        ))
    else:
        checks.append(check(
            area, "Every current Bellhaven account is matched or flagged", SKIP,
            "The website crawl was incomplete, so absence was not evaluated.",
        ))
    locations = {location.key: location for location in website.locations}
    misses, twins = [], []
    for row in rows:
        location = locations.get(row["key"])
        if location is None:
            continue
        number = house_number(location.address)
        for account in accounts:
            account_id = account["account_id"]
            if account_id == parent_id or account_id in row["account_ids"] or historical(account):
                continue
            same_place = bool(
                number and house_number(account.get("billing_street")) == number
                and street_words(location.address) & street_words(account.get("billing_street"))
                and (
                    zip_key(location.zip) == zip_key(account.get("billing_zip"))
                    or normalize(location.city) == normalize(account.get("billing_city"))
                )
            )
            label = f"{account['name']} [{account_id}] at {account.get('billing_street')}, {account.get('billing_city')}"
            if same_place:
                misses.append(f"{location.name}: {label}")
            elif identity_name(account.get("name")) and identity_name(account.get("name")) == identity_name(location.name):
                twins.append(f"{location.name} ({location.city} {location.state}) vs {label} {account.get('billing_state')}")
    checks.append(check(
        area, "No account at a facility's address was left out of matching", WARN if misses else PASS,
        listing(misses) or "a looser address scan found none",
    ))
    checks.append(check(area, "Same-name accounts at other addresses (shown as related evidence)", INFO, listing(twins) or "none"))
    return checks


def verify_run(
    website: WebsiteSnapshot, accounts: list[Account], summary: dict[str, Any], crm: CRM, parent_id: str,
    website_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if website_verification is None:
        website_verification = {
            "checked_at": None,
            "checks": [check(
                "Website", "Independent website verification", WARN if crm.is_live else SKIP,
                "Not run for this website snapshot; refresh from the website."
                if crm.is_live else "Synthetic website: there are no pages to re-read.",
            )],
        }
    checks = [
        *website_verification["checks"], *verify_crm(accounts, crm, parent_id),
        *verify_matching(website, accounts, summary, parent_id),
    ]
    return {
        "checked_at": utcnow(), "website_checked_at": website_verification["checked_at"],
        "status": overall(checks), "checks": checks,
    }


def reused_website_verification(summary: dict[str, Any]) -> dict[str, Any] | None:
    previous = summary.get("verification") or {}
    if not previous.get("website_checked_at"):
        return None
    return {
        "checked_at": previous["website_checked_at"],
        "checks": [item for item in previous["checks"] if item["area"] == "Website"],
    }
