from __future__ import annotations

from typing import Any

from bellhaven.crm import CRM, AcknowledgedWrite, CRMError, UncertainWrite
from bellhaven.domain import (
    Account, account_snapshot, care_key, normalize, requires_chow, street_key,
    validate_payload, zip_key,
)
from bellhaven.models import Proposal
from bellhaven.store import Store, StoreError


class ExecutionError(RuntimeError):
    pass


class StaleProposal(ExecutionError):
    pass


def resolve(value: Any, references: dict[str, str]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"ref"}:
            if value["ref"] not in references:
                raise ExecutionError("A prerequisite account has not been confirmed.")
            return references[value["ref"]]
        return {key: resolve(item, references) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, references) for item in value]
    return value


def contains_values(account: Account, payload: dict[str, Any]) -> bool:
    return all(account.get(key) == value for key, value in payload.items())


class Executor:
    def __init__(self, store: Store, crm: CRM):
        self.store = store
        self.crm = crm

    @staticmethod
    def validate_plan(proposal: Proposal) -> None:
        protected = set(proposal.metadata.get("chow_old_ids", []))
        for step in proposal.steps:
            operation = step.get("op")
            if operation not in {"create", "patch", "bind"}:
                raise ExecutionError("The plan contains an unsupported operation.")
            if operation == "bind":
                continue
            payload = step["payload"]
            validate_payload(payload)
            if "parent_id" in payload and (
                not isinstance(payload["parent_id"], str) or not payload["parent_id"]
            ):
                raise ExecutionError("A parent change requires an explicit parent account ID.")
            if operation == "create":
                if not proposal.metadata.get("parent_id") or payload.get("parent_id") != proposal.metadata["parent_id"]:
                    raise ExecutionError("A new account must use the approved target parent.")
                continue
            account_id = step["account_id"]
            if account_id not in proposal.before:
                raise ExecutionError("A write is missing its original account snapshot.")
            before = proposal.before[account_id]
            if account_id in protected and set(payload) != {"chow_current_account"}:
                raise ExecutionError("The old account in a change of ownership may only receive chow_current_account.")
            if "parent_id" in payload and payload["parent_id"] != before["parent_id"]:
                if requires_chow(before):
                    raise ExecutionError("The SOP prohibits re-parenting this billing account.")
            if before.get("chow_current_account") and set(payload) != {"chow_current_account"}:
                raise ExecutionError("Preserve historical change-of-ownership accounts; do not modify their other fields.")

    def _expected(
        self, proposal_id: str, revision: int, proposal: Proposal, references: dict[str, str],
    ) -> dict[str, Account]:
        expected = {key: dict(value) for key, value in proposal.before.items()}
        for index, step in enumerate(proposal.steps):
            operation = self.store.operation(proposal_id, revision, index)
            if operation and operation["state"] == "done" and step["op"] == "patch":
                expected[step["account_id"]].update(resolve(step["payload"], references))
        return expected

    def _preflight(
        self, proposal_id: str, revision: int, proposal: Proposal, references: dict[str, str],
    ) -> None:
        expected = self._expected(proposal_id, revision, proposal, references)
        for account_id, before in expected.items():
            current = account_snapshot(self.crm.get_account(account_id))
            if current != before:
                raise StaleProposal(
                    f"Account {account_id} changed since review. No further writes were attempted."
                )
        parent_ids = {
            step["payload"]["parent_id"] for step in proposal.steps
            if step["op"] in {"create", "patch"} and "parent_id" in step["payload"]
        }
        for parent_id in parent_ids:
            self.crm.get_account(parent_id)
        for index, step in enumerate(proposal.steps):
            operation = self.store.operation(proposal_id, revision, index)
            if step["op"] != "create" or (operation and operation["state"] == "done"):
                continue
            payload = resolve(step["payload"], references)
            for account in self.crm.list_accounts():
                if (
                    account["parent_id"] == payload["parent_id"]
                    and normalize(account["name"]) == normalize(payload["name"])
                    and street_key(account.get("billing_street")) == street_key(payload["billing_street"])
                    and zip_key(account.get("billing_zip")) == zip_key(payload["billing_zip"])
                    and care_key(account.get("care_type")).intersection(care_key(payload["care_type"]))
                    and not account.get("chow_current_account") and not account.get("duplicate_of_account")
                ):
                    raise StaleProposal("A matching current account now exists. Review it instead of creating another.")

    def _verify(
        self, proposal: Proposal, step: dict[str, Any], account_id: str,
        references: dict[str, str],
    ) -> Account:
        payload = resolve(step["payload"], references)
        current = self.crm.get_account(account_id)
        if not contains_values(current, payload):
            raise UncertainWrite("The readback does not confirm the approved field values.")
        if step["op"] == "patch":
            expected = dict(proposal.before[account_id])
            expected.update(payload)
            if account_snapshot(current) != expected:
                raise UncertainWrite("Readback found an unexpected change outside the approved patch.")
        return current

    def _reconcile_step(
        self, proposal: Proposal, step: dict[str, Any], previous: dict[str, Any],
        references: dict[str, str],
    ) -> tuple[str, Account | None]:
        if step["op"] == "bind":
            return step["account_id"], None
        known_id = previous["result"].get("account_id")
        if step["op"] == "patch":
            known_id = step["account_id"]
        if known_id:
            return known_id, self._verify(proposal, step, known_id, references)
        payload = resolve(step["payload"], references)
        note = payload.get("note", "")
        if "[bh:" not in note:
            raise UncertainWrite("An uncertain create has no approved reconciliation marker.")
        matches = [
            account for account in self.crm.list_accounts()
            if contains_values(account, payload)
        ]
        if len(matches) != 1:
            raise UncertainWrite(
                "Read-only reconciliation did not find exactly one created account. "
                "The create was not retried."
            )
        return matches[0]["account_id"], matches[0]

    def _record_change(
        self, proposal_id: str, revision: int, index: int, proposal: Proposal,
        step: dict[str, Any], current: Account | None, references: dict[str, str],
        verifier: str, reason: str,
    ) -> None:
        if current is None:
            return
        operation = self.store.operation(proposal_id, revision, index)
        if operation is None:
            raise ExecutionError("The write has no operation receipt.")
        before = proposal.before.get(current["account_id"], {})
        changes = {
            field: {"before": before.get(field), "after": current.get(field)}
            for field in resolve(step["payload"], references)
            if before.get(field) != current.get(field)
        }
        self.store.record_event(
            proposal_id, revision, f"write:{index}",
            "Account created" if step["op"] == "create" else "Account updated",
            operation["result"].get("actor") or "Not recorded",
            {
                "account_id": current["account_id"], "changes": changes,
                "crm_updated_at": current.get("updated_at"),
                "initiated_at": operation["result"].get("started_at"),
                "verified_by": verifier, "reason": reason,
            },
        )

    def apply(self, proposal_id: str, *, actor: str = "Local application") -> str:
        actor = self.store.checked_actor(actor)
        if self.crm.is_live and not getattr(self.crm, "writes_enabled", False):
            raise ExecutionError("Live writes are locked until the owner explicitly enables them.")
        with self.store.execution_lock():
            row = self.store.get_proposal(proposal_id)
            proposal: Proposal = row["proposal"]
            if row["decision"] != "approved" or row["plan_hash"] != proposal.plan_hash:
                raise ExecutionError("Only this exact approved plan can execute.")
            if row["execution"] == "applied":
                return "Already applied; no writes were performed."
            if not row["active"]:
                raise ExecutionError("Refresh and review this proposal; it is no longer active.")
            self.validate_plan(proposal)
            revision = row["revision"]
            approvals = [
                event for event in self.store.case_activity(proposal_id)
                if event["revision"] == revision and event["event"] == "Approved"
            ]
            if not approvals:
                raise ExecutionError("The approved plan has no decision receipt.")
            approval_reason = approvals[-1]["details"]["reason"]
            references: dict[str, str] = {}
            for index, step in enumerate(proposal.steps):
                previous = self.store.operation(proposal_id, revision, index)
                if previous and previous["state"] == "done" and step["op"] == "create":
                    references[step["ref"]] = previous["result"]["account_id"]
            self.store.set_execution(proposal_id, "running")
            try:
                for index, step in enumerate(proposal.steps):
                    previous = self.store.operation(proposal_id, revision, index)
                    if previous and previous["state"] == "done":
                        continue
                    if previous and previous["state"] in {"started", "written", "uncertain"}:
                        account_id, current = self._reconcile_step(proposal, step, previous, references)
                        if step["op"] == "create":
                            references[step["ref"]] = account_id
                        self._record_change(
                            proposal_id, revision, index, proposal, step, current,
                            references, actor, approval_reason,
                        )
                        self.store.record_operation(
                            proposal_id, revision, index, "done", {"account_id": account_id}
                        )
                        continue
                    self._preflight(proposal_id, revision, proposal, references)
                    self.store.record_operation(proposal_id, revision, index, "started", actor=actor)
                    try:
                        # The write API's response bodies are undocumented, so readback (not the body) confirms each write.
                        response: dict[str, str] = {}
                        if step["op"] == "bind":
                            account_id = step["account_id"]
                        elif step["op"] == "create":
                            try:
                                account = self.crm.create_account(resolve(step["payload"], references))
                                account_id = account["account_id"]
                            except AcknowledgedWrite as exc:
                                response = {"response_shape": exc.shape}
                                account_id, _ = self._reconcile_step(proposal, step, {"result": {}}, references)
                            references[step["ref"]] = account_id
                        else:
                            account_id = step["account_id"]
                            try:
                                self.crm.update_account(account_id, resolve(step["payload"], references))
                            except AcknowledgedWrite as exc:
                                response = {"response_shape": exc.shape}
                        self.store.record_operation(
                            proposal_id, revision, index, "written", {"account_id": account_id, **response}
                        )
                        if step["op"] != "bind":
                            current = self._verify(proposal, step, account_id, references)
                            self._record_change(
                                proposal_id, revision, index, proposal, step, current,
                                references, actor, approval_reason,
                            )
                        self.store.record_operation(
                            proposal_id, revision, index, "done", {"account_id": account_id}
                        )
                    except UncertainWrite:
                        raise
                    except CRMError as exc:
                        recorded = self.store.operation(proposal_id, revision, index)
                        if recorded and recorded["state"] == "written":
                            raise UncertainWrite(
                                "The write was acknowledged, but readback failed. "
                                "Its account ID is retained; no write will be blindly retried."
                            ) from exc
                        self.store.record_operation(
                            proposal_id, revision, index, "failed", message=str(exc)
                        )
                        raise
                self._preflight(proposal_id, revision, proposal, references)
                for location_key, target in proposal.metadata.get("bindings", {}).items():
                    account_id = resolve(target, references)
                    previous_link = self.store.identity_links().get(location_key)
                    if previous_link != account_id:
                        self.store.bind_identity(location_key, account_id)
                        self.store.record_event(
                            proposal_id, revision, "binding:" + location_key,
                            "Identity link updated", actor,
                            {
                                "location_key": location_key, "before": previous_link,
                                "after": account_id, "reason": approval_reason,
                            },
                        )
                if proposal.metadata.get("action_type") == "custom":
                    scope_ids = set(proposal.before) | set(references.values())
                    scope_ids.update(
                        account["account_id"] for account in
                        proposal.evidence.get("accounts", []) + proposal.evidence.get("related_accounts", [])
                    )
                    self.store.save_custom_outcome(
                        proposal_id, revision, proposal,
                        [self.crm.get_account(account_id) for account_id in sorted(scope_ids)],
                    )
                self.store.set_execution(proposal_id, "applied", "Every approved step was read back.")
                return "Applied and verified against the local synthetic CRM."
            except StaleProposal as exc:
                self.store.set_execution(proposal_id, "stale", str(exc))
                raise
            except UncertainWrite as exc:
                self.store.set_execution(proposal_id, "uncertain", str(exc))
                raise
            except (CRMError, StoreError, ValueError, ExecutionError) as exc:
                self.store.set_execution(proposal_id, "failed", str(exc))
                raise ExecutionError(str(exc)) from exc
