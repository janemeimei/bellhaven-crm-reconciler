from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from bellhaven.models import BASE_URL, Location, WebsiteSnapshot, utcnow


class WebsiteError(RuntimeError):
    pass


def public_url(href: str, current_url: str = BASE_URL) -> str | None:
    parts = urlsplit(urljoin(current_url, href))
    origin = urlsplit(BASE_URL)
    if parts.scheme != origin.scheme or parts.netloc != origin.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    if path not in ("/", "/about", "/communities") and not re.fullmatch(
        r"/communities/[a-zA-Z0-9_-]+", path
    ):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    if query:
        if path != "/communities" or set(query) != {"page"}:
            return None
        pages = query["page"]
        if len(pages) != 1 or not pages[0].isdigit() or int(pages[0]) < 1:
            return None
        number = int(pages[0])
        query_string = urlencode({"page": number}) if number != 1 else ""
    else:
        query_string = ""
    return urlunsplit((parts.scheme, parts.netloc, path, query_string, ""))


def fetch_public_page(url: str) -> str:
    if public_url(url) != url:
        raise WebsiteError("Refusing to fetch a URL outside the public assessment website.")
    try:
        response = requests.get(url, timeout=(5, 25), allow_redirects=False)
    except requests.RequestException as exc:
        raise WebsiteError(f"Public website request failed: {type(exc).__name__}.") from exc
    if response.status_code != 200:
        raise WebsiteError(f"Public website returned HTTP {response.status_code}.")
    if "text/html" not in response.headers.get("Content-Type", "").lower():
        raise WebsiteError("Expected an HTML page from the public website.")
    return response.text


def parse_location(url: str, soup: BeautifulSoup) -> Location:
    heading = soup.find("h1")
    details = soup.select_one("dl.detail")
    if heading is None or details is None:
        raise WebsiteError("A community detail page is missing its heading or detail fields.")
    fields = {}
    for label in details.find_all("dt"):
        value = label.find_next_sibling("dd")
        if value is not None:
            fields[label.get_text(" ", strip=True).casefold()] = value
    address_node = fields.get("address")
    care_node = fields.get("care offerings")
    if address_node is None or care_node is None:
        raise WebsiteError("A community is missing its address or care offerings.")
    address_lines = [line.strip() for line in address_node.get_text("\n", strip=True).splitlines()]
    if len(address_lines) < 2:
        raise WebsiteError("A community address is missing its street or city/state/ZIP line.")
    locality = re.fullmatch(
        r"(?P<city>.+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)",
        address_lines[-1],
    )
    if locality is None:
        raise WebsiteError("A community has an unrecognized city/state/ZIP format.")
    offerings = tuple(node.get_text(" ", strip=True) for node in care_node.select(".badge"))
    if not offerings:
        raise WebsiteError("A community has no care offerings.")
    name = heading.get_text(" ", strip=True)
    if not name or not all(address_lines):
        raise WebsiteError("A community has an empty name or address.")
    phone_node = fields.get("phone")
    return Location(
        key=urlsplit(url).path,
        name=name,
        address=", ".join(address_lines[:-1]),
        city=locality["city"],
        state=locality["state"],
        zip=locality["zip"],
        care_offerings=offerings,
        source_url=url,
        evidence=f"{name} | {'; '.join(address_lines)} | {'; '.join(offerings)}",
        phone=phone_node.get_text(" ", strip=True) if phone_node else "",
    )


def scrape_website(
    fetch: Callable[[str], str] = fetch_public_page,
) -> WebsiteSnapshot:
    pending = deque([BASE_URL + "/", BASE_URL + "/communities", BASE_URL + "/about"])
    visited: set[str] = set()
    locations: dict[str, Location] = {}
    directory_links: set[str] = set()
    directory_pages: set[int] = set()
    page_totals: set[int] = set()
    declared_counts: set[int] = set()
    sources = []
    errors = []
    warnings = []
    fetched_at = utcnow()
    while pending:
        url = pending.popleft()
        if url in visited:
            continue
        if len(visited) >= 200:
            errors.append("The public website exceeded the 200-page safety limit; crawl incomplete.")
            break
        visited.add(url)
        try:
            soup = BeautifulSoup(fetch(url), "html.parser")
            content = soup.select_one(".wrap") or soup.body
            if content is None:
                raise WebsiteError("A public page has no readable body.")
            sources.append(
                {"url": url, "text": content.get_text(" ", strip=True), "fetched_at": fetched_at}
            )
            path = urlsplit(url).path
            if re.fullmatch(r"/communities/[a-zA-Z0-9_-]+", path):
                locations[url] = parse_location(url, soup)
            elif path == "/communities":
                summary = re.search(
                    r"Page\s+(\d+)\s+of\s+(\d+).*?(\d+)\s+communities listed",
                    content.get_text(" ", strip=True),
                )
                if summary is None:
                    raise WebsiteError("The directory is missing its pagination/count summary.")
                page, total, count = map(int, summary.groups())
                directory_pages.add(page)
                page_totals.add(total)
                declared_counts.add(count)
                for node in soup.select(".card h3 a[href]"):
                    detail_url = public_url(node["href"], url)
                    if detail_url is not None:
                        directory_links.add(detail_url)
            for node in soup.select("a[href]"):
                discovered = public_url(node["href"], url)
                if discovered is not None and discovered not in visited:
                    pending.append(discovered)
        except (WebsiteError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    if len(page_totals) != 1 or len(declared_counts) != 1:
        errors.append("Directory page totals or community counts are missing or inconsistent.")
    else:
        total_pages = next(iter(page_totals))
        if directory_pages != set(range(1, total_pages + 1)):
            errors.append("Not every declared directory page was fetched.")
        if len(directory_links) != next(iter(declared_counts)):
            errors.append("The number of unique directory links differs from its declared count.")
    if not directory_links.issubset(locations):
        errors.append("One or more directory communities could not be parsed.")
    supplemental = set(locations) - directory_links
    if supplemental:
        warnings.append(
            f"Discovered {len(supplemental)} additional community page(s) outside the directory."
        )
    if not locations:
        errors.append("No valid communities were collected.")
    return WebsiteSnapshot(
        locations=tuple(locations[url] for url in sorted(locations)),
        sources=tuple(sources),
        directory_count=next(iter(declared_counts)) if len(declared_counts) == 1 else 0,
        directory_discovered=len(directory_links),
        fetched_at=fetched_at,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
