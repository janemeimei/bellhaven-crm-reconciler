from __future__ import annotations

import json
import re
import uuid
from typing import Any, Protocol

import requests

from bellhaven.domain import Account, validate_account, validate_payload
from bellhaven.models import BASE_URL, utcnow
from bellhaven.store import Store


class CRMError(RuntimeError):
    pass


class UncertainWrite(CRMError):
    pass


class AcknowledgedWrite(UncertainWrite):
    # A 2xx write whose body is not a recognizable account; the executor's readback decides the outcome.
    def __init__(self, message: str, shape: str):
        super().__init__(message)
        self.shape = shape


def response_shape(value: Any) -> str:
    if isinstance(value, dict):
        return "object keys: " + ", ".join(sorted(str(key) for key in value)[:12])
    return type(value).__name__


class CRM(Protocol):
    is_live: bool

    def list_accounts(self) -> list[Account]: ...
    def get_account(self, account_id: str) -> Account: ...
    def create_account(self, payload: dict[str, Any]) -> Account: ...
    def update_account(self, account_id: str, payload: dict[str, Any]) -> Account: ...


class DemoCRM:
    is_live = False

    def __init__(self, store: Store, seed: list[Account] | None = None):
        self.store = store
        if seed:
            existing = {account["account_id"] for account in store.demo_accounts()}
            for account in seed:
                if account["account_id"] not in existing:
                    store.put_demo_account(account)

    def list_accounts(self) -> list[Account]:
        return self.store.demo_accounts()

    def get_account(self, account_id: str) -> Account:
        matches = [row for row in self.list_accounts() if row["account_id"] == account_id]
        if len(matches) != 1:
            raise CRMError("The requested synthetic account does not exist.")
        return matches[0]

    def create_account(self, payload: dict[str, Any]) -> Account:
        validate_payload(payload)
        if not payload.get("name") or not payload.get("parent_id"):
            raise CRMError("A new facility needs a name and a parent.")
        account = {
            "account_id": "DEMO-" + uuid.uuid4().hex[:12],
            "name": "", "parent_id": "", "parent_name": "", "billing_street": "",
            "billing_city": "", "billing_state": "", "billing_zip": "", "care_type": "",
            "status": "Active", "phone": "", "lifetime_revenue": 0, "outstanding_ar": 0,
            "chow_current_account": "", "duplicate_of_account": "", "note": "",
            "created_by_candidate": True, "updated_at": utcnow(),
        }
        account.update(payload)
        account["parent_name"] = self.get_account(account["parent_id"])["name"]
        self.store.put_demo_account(account)
        return account

    def update_account(self, account_id: str, payload: dict[str, Any]) -> Account:
        validate_payload(payload)
        account = self.get_account(account_id)
        account.update(payload)
        if "parent_id" in payload:
            account["parent_name"] = self.get_account(payload["parent_id"])["name"]
        account["updated_at"] = utcnow()
        self.store.put_demo_account(account)
        return account


class LiveCRM:
    is_live = True

    def __init__(self, token: str, *, confirmed: bool = False, allow_writes: bool = False):
        self._confirmed = confirmed
        self._allow_writes = allow_writes
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.headers["Authorization"] = "Bearer " + token

    @property
    def writes_enabled(self) -> bool:
        return self._confirmed and self._allow_writes

    @staticmethod
    def _account_path(account_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            raise CRMError("Invalid account identifier.")
        return "/accounts/" + account_id

    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self._confirmed:
            raise CRMError("Live connections are locked until the owner confirms live testing.")
        writing = method != "GET"
        if writing and not self._allow_writes:
            raise CRMError("This CRM client is read-only.")
        try:
            response = self._session.request(
                method, BASE_URL + "/api/v1" + path, json=payload, params=params,
                timeout=(5, 25), allow_redirects=False,
            )
        except requests.RequestException as exc:
            error = UncertainWrite if writing else CRMError
            raise error(f"CRM {method} failed: {type(exc).__name__}. No automatic retry.") from None
        if not 200 <= response.status_code < 300:
            error = UncertainWrite if writing and response.status_code >= 500 else CRMError
            raise error(
                f"CRM {method} returned HTTP {response.status_code}{self._error_detail(response)}. "
                "No automatic retry."
            )
        try:
            return response.json()
        except requests.exceptions.JSONDecodeError:
            error = UncertainWrite if writing else CRMError
            raise error("The CRM returned invalid JSON; the operation needs verification.") from None

    def _error_detail(self, response: Any) -> str:
        # The server's own reason (for example a rejected field) is kept short and never includes the token.
        try:
            body = response.json()
            detail = body.get("detail") or body.get("error") or body.get("message")
        except (ValueError, AttributeError):
            return ""
        if not detail:
            return ""
        text = detail if isinstance(detail, str) else json.dumps(detail)
        token = self._session.headers["Authorization"].removeprefix("Bearer ")
        return ": " + text.replace(token, "[redacted]")[:300]

    @staticmethod
    def _unpack_account(value: Any) -> Account:
        if isinstance(value, dict) and isinstance(value.get("data"), dict):
            value = value["data"]
        try:
            return validate_account(value)
        except ValueError as exc:
            raise CRMError(str(exc)) from exc

    def _list(self, filters: dict[str, Any]) -> list[Account]:
        result = []
        seen: set[str] = set()
        expected_total = None
        page = 1
        while True:
            value = self._request("GET", "/accounts", params={**filters, "page": page, "page_size": 50})
            if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                raise CRMError("Expected the documented data/page/page_size/total account envelope.")
            if not isinstance(value.get("total"), int) or value["total"] < 0:
                raise CRMError("The account response is missing a valid total.")
            if value.get("page") != page:
                raise CRMError("The CRM returned an unexpected page number.")
            if expected_total is None:
                expected_total = value["total"]
            if value["total"] != expected_total:
                raise CRMError("The CRM changed during pagination; refresh the complete snapshot.")
            for raw in value["data"]:
                account = self._unpack_account(raw)
                if account["account_id"] in seen:
                    raise CRMError("Duplicate account IDs were encountered during pagination.")
                seen.add(account["account_id"])
                result.append(account)
            if len(result) == expected_total:
                return result
            if not value["data"] or len(result) > expected_total:
                raise CRMError("Account pagination ended before its declared total.")
            page += 1

    def list_accounts(self) -> list[Account]:
        return self._list({})

    def list_children(self, parent_id: str) -> list[Account]:
        return self._list({"parent_id": parent_id})

    def get_account(self, account_id: str) -> Account:
        return self._unpack_account(self._request("GET", self._account_path(account_id)))

    def create_account(self, payload: dict[str, Any]) -> Account:
        validate_payload(payload)
        value = self._request("POST", "/accounts", payload=payload)
        try:
            return self._unpack_account(value)
        except CRMError:
            raise AcknowledgedWrite(
                "The CRM accepted the create, but its response does not identify the account.",
                response_shape(value),
            ) from None

    def update_account(self, account_id: str, payload: dict[str, Any]) -> Account:
        validate_payload(payload)
        value = self._request("PATCH", self._account_path(account_id), payload=payload)
        try:
            return self._unpack_account(value)
        except CRMError:
            raise AcknowledgedWrite(
                "The CRM accepted the update, but its response is not an account.",
                response_shape(value),
            ) from None
