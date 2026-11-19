from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


BASE_URL = "https://analyst-assessment-production.up.railway.app"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Location:
    key: str
    name: str
    address: str
    city: str
    state: str
    zip: str
    care_offerings: tuple[str, ...]
    source_url: str
    evidence: str
    phone: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Location:
        data = dict(value)
        data["care_offerings"] = tuple(data["care_offerings"])
        return cls(**data)


@dataclass(frozen=True)
class WebsiteSnapshot:
    locations: tuple[Location, ...]
    sources: tuple[dict[str, Any], ...]
    directory_count: int
    directory_discovered: int
    fetched_at: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.errors and bool(self.locations)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WebsiteSnapshot:
        data = dict(value)
        data["locations"] = tuple(Location.from_dict(item) for item in data["locations"])
        for key in ("sources", "errors", "warnings"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


@dataclass(frozen=True)
class Proposal:
    key: str
    kind: str
    title: str
    rationale: str
    location_keys: tuple[str, ...]
    account_ids: tuple[str, ...]
    evidence: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    before: dict[str, dict[str, Any]]
    warnings: tuple[str, ...] = ()
    actionable: bool = True
    group_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def plan_hash(self) -> str:
        return fingerprint(
            {
                "key": self.key,
                "kind": self.kind,
                "steps": self.steps,
                "before": self.before,
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Proposal:
        data = dict(value)
        for key in ("location_keys", "account_ids", "steps", "warnings"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)
