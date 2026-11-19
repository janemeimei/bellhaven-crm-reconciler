from __future__ import annotations

from bellhaven.domain import Account
from bellhaven.models import Location, WebsiteSnapshot, utcnow


PARENT_ID = "DEMO-BELLHAVEN"


def account(
    account_id: str, name: str, street: str = "", *, parent: str = PARENT_ID,
    revenue: int | None = 0, ar: int | None = 0, care: str = "Assisted Living",
    current: str = "",
) -> Account:
    return {
        "account_id": account_id, "name": name, "parent_id": parent,
        "parent_name": "Bellhaven Senior Living" if parent == PARENT_ID else "Former Operator",
        "billing_street": street, "billing_city": "Exampleton" if street else "",
        "billing_state": "OH" if street else "", "billing_zip": "43000" if street else "",
        "care_type": care if street else "", "status": "Active", "phone": "",
        "lifetime_revenue": revenue, "outstanding_ar": ar, "chow_current_account": current,
        "duplicate_of_account": "", "note": "Synthetic demonstration data.",
        "created_by_candidate": False, "updated_at": "2026-01-01T00:00:00+00:00",
    }


def location(slug: str, name: str, street: str, care: str = "Assisted Living") -> Location:
    return Location(
        key="demo:" + slug, name=name, address=street, city="Exampleton", state="OH",
        zip="43000", care_offerings=(care,), source_url="demo://communities/" + slug,
        evidence=f"SYNTHETIC: {name}; {street}, Exampleton, OH 43000; {care}",
    )


def demo_website() -> WebsiteSnapshot:
    locations = (
        location("willow", "Bellhaven Willow House", "101 Example Street"),
        location("cedar", "Bellhaven Cedar House", "202 Example Road"),
        location("birch", "Bellhaven Birch House", "303 Example Avenue"),
        location("meadow", "Bellhaven Meadow House", "404 Example Lane"),
        location("orchard", "Bellhaven Orchard House", "505 Example Drive"),
        location("campus-north", "Bellhaven Campus North", "606 Campus Way"),
        location("campus-south", "Bellhaven Campus South", "606 Campus Way", "Memory Support"),
        location("harbor", "Bellhaven Harbor House", "707 Example Court"),
        location("financial-review", "Bellhaven Aspen House", "808 Example Boulevard"),
    )
    return WebsiteSnapshot(
        locations=locations,
        sources=(
            {
                "url": "demo://directory",
                "text": "Synthetic directory. Campus North and Campus South are separately listed.",
                "fetched_at": utcnow(),
            },
            {
                "url": "demo://about",
                "text": "Synthetic context: selected communities changed operators. This is not real assessment evidence.",
                "fetched_at": utcnow(),
            },
        ),
        directory_count=len(locations), directory_discovered=len(locations), fetched_at=utcnow(),
    )


def demo_accounts() -> list[Account]:
    return [
        account(PARENT_ID, "Bellhaven Senior Living", parent=""),
        account("DEMO-FORMER", "Former Operator", parent=""),
        account("DEMO-WILLOW", "Willow Manor", "101 Example St", revenue=5000),
        account("DEMO-CEDAR", "Cedar Care Center", "202 Example Rd", parent="DEMO-FORMER", revenue=84000, ar=2300),
        account("DEMO-BIRCH", "Bellhaven Birch House", "303 Example Ave", parent="DEMO-FORMER", ar=125),
        account("DEMO-ORCHARD-A", "Bellhaven Orchard House", "505 Example Dr", revenue=4000),
        account("DEMO-ORCHARD-B", "Bellhaven Orchard House", "505 Example Dr"),
        account("DEMO-CAMPUS", "Former Campus Center", "606 Campus Way", care="Assisted Living; Memory Support"),
        account("DEMO-HARBOR-OLD", "Harbor Care Center", "707 Example Ct", parent="DEMO-FORMER", revenue=55000, ar=900, current="DEMO-HARBOR"),
        account("DEMO-HARBOR", "Bellhaven Harbor House", "707 Example Court"),
        account("DEMO-ASPEN", "Aspen Care", "808 Example Blvd", parent="DEMO-FORMER", revenue=None, ar=100),
        account("DEMO-UNLISTED", "Former Bellhaven Elm House", "909 Example Circle", revenue=2000),
    ]
