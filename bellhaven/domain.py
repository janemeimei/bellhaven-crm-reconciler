from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


Account = dict[str, Any]

WRITABLE_FIELDS = frozenset(
    {
        "name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip",
        "care_type", "status", "phone", "note", "chow_current_account", "duplicate_of_account",
    }
)
STATUSES = frozenset({"Active", "Inactive", "Needs Review"})


class FinanceError(ValueError):
    pass


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def identity_name(value: Any) -> str:
    generic = {
        "bellhaven", "senior", "living", "care", "center", "centre", "community",
        "house", "manor", "healthcare", "rehabilitation", "nursing", "of", "at", "the", "and",
    }
    return " ".join(word for word in normalize(value).split() if word not in generic)


def street_key(value: Any) -> str:
    aliases = {
        "street": "st", "road": "rd", "avenue": "ave", "boulevard": "blvd",
        "drive": "dr", "lane": "ln", "court": "ct", "highway": "hwy",
        "north": "n", "south": "s", "east": "e", "west": "w",
        "suite": "unit", "ste": "unit", "apartment": "unit", "apt": "unit",
    }
    return " ".join(aliases.get(word, word) for word in normalize(str(value or "").replace("#", " unit ")).split())


def zip_key(value: Any) -> str:
    match = re.fullmatch(r"(\d{5})(?:-\d{4})?", str(value or "").strip())
    return match[1] if match else ""


def care_key(value: Any) -> set[str]:
    text = normalize(value)
    result = set()
    if "assisted" in text or text == "al":
        result.add("assisted")
    if "memory" in text or text == "mc":
        result.add("memory")
    if any(word in text for word in ("nursing", "rehab", "skilled")) or text == "snf":
        result.add("nursing")
    return result or ({text} if text else set())


def money(account: Account, field: str) -> Decimal:
    value = account.get(field)
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise FinanceError(f"{field} is missing; missing financial data is not zero.")
    try:
        result = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise FinanceError(f"{field} is not a valid monetary value.") from exc
    if not result.is_finite():
        raise FinanceError(f"{field} must be finite.")
    return result


def requires_chow(account: Account) -> bool:
    revenue = money(account, "lifetime_revenue")
    ar = money(account, "outstanding_ar")
    if revenue < 0:
        raise FinanceError("Negative lifetime revenue needs a history review before re-parenting.")
    return revenue > 0 and ar > 0


def account_snapshot(account: Account) -> Account:
    return {
        key: value for key, value in account.items()
        if key not in {"updated_at", "parent_name"}
    }


def validate_payload(payload: dict[str, Any]) -> None:
    unknown = set(payload) - WRITABLE_FIELDS
    if unknown:
        raise ValueError(f"Unsupported or protected write fields: {', '.join(sorted(unknown))}.")
    if "status" in payload and payload["status"] not in STATUSES:
        raise ValueError("Status must be Active, Inactive, or Needs Review.")


def validate_account(value: Any) -> Account:
    if not isinstance(value, dict) or not isinstance(value.get("account_id"), str):
        raise ValueError("Expected an account object with a string account_id.")
    required = ("name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip")
    if any(not isinstance(value.get(key), str) for key in required):
        raise ValueError("The CRM account is missing required string identity/address fields.")
    return dict(value)
