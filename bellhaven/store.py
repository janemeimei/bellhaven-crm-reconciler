from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bellhaven.models import Proposal, WebsiteSnapshot, canonical_json, utcnow


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path, scope: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise StoreError("The database must not be a symbolic link.")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
        info = self.path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StoreError("The local database must be owned by you and use mode 0600.")
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    website TEXT NOT NULL, accounts TEXT NOT NULL, summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, intent_key TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, latest_payload TEXT NOT NULL, plan_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    decision TEXT NOT NULL DEFAULT 'pending',
                    execution TEXT NOT NULL DEFAULT 'not_started',
                    active INTEGER NOT NULL DEFAULT 1, message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
                    revision INTEGER NOT NULL, decision TEXT NOT NULL, plan_hash TEXT NOT NULL,
                    payload TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    proposal_id TEXT NOT NULL REFERENCES proposals(id),
                    revision INTEGER NOT NULL, step INTEGER NOT NULL, state TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}', message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (proposal_id, revision, step)
                );
                CREATE TABLE IF NOT EXISTS demo_accounts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_links (
                    location_key TEXT PRIMARY KEY, account_id TEXT NOT NULL
                );
                """
            )
            existing = connection.execute(
                "SELECT value FROM metadata WHERE key = 'scope'"
            ).fetchone()
            if existing is not None and existing["value"] != scope:
                raise StoreError("This database belongs to another mode or CRM credential.")
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('scope', ?)", (scope,)
            )

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def execution_lock(self):
        descriptor = os.open(
            str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StoreError("Another local execution is running. Try again when it finishes.") from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def save_run(
        self,
        website: WebsiteSnapshot,
        accounts: list[dict[str, Any]],
        proposals: list[Proposal],
        summary: dict[str, Any],
    ) -> str:
        run_id = uuid.uuid4().hex
        now = utcnow()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (
                    run_id, now, canonical_json(website.to_dict()),
                    canonical_json(accounts), canonical_json(summary),
                ),
            )
            connection.execute("UPDATE proposals SET active = 0")
            for proposal in proposals:
                payload = canonical_json(proposal.to_dict())
                existing = connection.execute(
                    "SELECT id, decision, execution FROM proposals WHERE intent_key = ?",
                    (proposal.key,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO proposals
                        (id, intent_key, payload, latest_payload, plan_hash, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (uuid.uuid4().hex, proposal.key, payload, payload, proposal.plan_hash, now, now),
                    )
                elif existing["decision"] == "pending":
                    connection.execute(
                        """UPDATE proposals SET payload = ?, latest_payload = ?, plan_hash = ?,
                        active = 1, updated_at = ? WHERE id = ?""",
                        (payload, payload, proposal.plan_hash, now, existing["id"]),
                    )
                else:
                    connection.execute(
                        """UPDATE proposals SET latest_payload = ?, active = 1, updated_at = ?
                        WHERE id = ?""",
                        (payload, now, existing["id"]),
                    )
        return run_id

    def latest_run(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"], "created_at": row["created_at"],
            "website": WebsiteSnapshot.from_dict(json.loads(row["website"])),
            "accounts": json.loads(row["accounts"]), "summary": json.loads(row["summary"]),
        }

    @staticmethod
    def decode_proposal(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["proposal"] = Proposal.from_dict(json.loads(value.pop("payload")))
        value["latest_proposal"] = Proposal.from_dict(json.loads(value.pop("latest_payload")))
        return value

    def proposals(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM proposals ORDER BY rowid").fetchall()
        return [self.decode_proposal(row) for row in rows]

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise StoreError("The proposal no longer exists.")
        return self.decode_proposal(row)

    def decide(self, proposal_id: str, plan_hash: str, decision: str, reason: str = "") -> None:
        if decision not in ("approved", "rejected"):
            raise StoreError("A decision must be approved or rejected.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None or row["decision"] != "pending" or not row["active"]:
                raise StoreError("Only an active, pending proposal can be decided.")
            proposal = Proposal.from_dict(json.loads(row["payload"]))
            if row["plan_hash"] != plan_hash:
                raise StoreError("The proposal changed. Reload and review its current version.")
            if decision == "approved" and (not proposal.actionable or not proposal.steps):
                raise StoreError("This case has no executable plan. Resolve its evidence first.")
            connection.execute(
                """INSERT INTO decisions
                (proposal_id, revision, decision, plan_hash, payload, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal_id, row["revision"], decision, plan_hash,
                    row["payload"], reason, utcnow(),
                ),
            )
            connection.execute(
                "UPDATE proposals SET decision = ?, updated_at = ? WHERE id = ?",
                (decision, utcnow(), proposal_id),
            )

    def reopen(self, proposal_id: str) -> None:
        with self.execution_lock(), self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None or not row["active"] or row["decision"] == "pending":
                raise StoreError("Only a currently observed, decided proposal can be reopened.")
            operations = connection.execute(
                "SELECT state FROM operations WHERE proposal_id = ? AND revision = ?",
                (proposal_id, row["revision"]),
            ).fetchall()
            if any(operation["state"] != "failed" for operation in operations):
                raise StoreError("Reconcile existing writes before reopening this proposal.")
            latest = Proposal.from_dict(json.loads(row["latest_payload"]))
            connection.execute(
                """UPDATE proposals SET payload = latest_payload, plan_hash = ?,
                revision = revision + 1, decision = 'pending', execution = 'not_started',
                message = '', updated_at = ? WHERE id = ?""",
                (latest.plan_hash, utcnow(), proposal_id),
            )

    def set_execution(self, proposal_id: str, state: str, message: str = "") -> None:
        if state not in ("running", "applied", "failed", "uncertain", "stale"):
            raise StoreError("Unknown execution state.")
        with self.connection() as connection:
            connection.execute(
                "UPDATE proposals SET execution = ?, message = ?, updated_at = ? WHERE id = ?",
                (state, message, utcnow(), proposal_id),
            )

    def operation(self, proposal_id: str, revision: int, step: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE proposal_id = ? AND revision = ? AND step = ?",
                (proposal_id, revision, step),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"])
        return result

    def record_operation(
        self, proposal_id: str, revision: int, step: int, state: str,
        result: dict[str, Any] | None = None, message: str = "",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO operations
                (proposal_id, revision, step, state, result, message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id, revision, step) DO UPDATE SET
                state = excluded.state, result = excluded.result,
                message = excluded.message, updated_at = excluded.updated_at""",
                (proposal_id, revision, step, state, canonical_json(result or {}), message, utcnow()),
            )

    def audit(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT proposal_id, revision, decision, plan_hash, reason, created_at
                FROM decisions ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def demo_accounts(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT payload FROM demo_accounts ORDER BY id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def put_demo_account(self, account: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO demo_accounts VALUES (?, ?) ON CONFLICT(id) DO UPDATE
                SET payload = excluded.payload""",
                (account["account_id"], canonical_json(account)),
            )

    def identity_links(self) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM identity_links").fetchall()
        return {row["location_key"]: row["account_id"] for row in rows}

    def bind_identity(self, location_key: str, account_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO identity_links VALUES (?, ?) ON CONFLICT(location_key)
                DO UPDATE SET account_id = excluded.account_id""",
                (location_key, account_id),
            )

    def add_proposal(self, proposal: Proposal) -> str:
        now = utcnow()
        payload = canonical_json(proposal.to_dict())
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT id, decision FROM proposals WHERE intent_key = ?", (proposal.key,)
            ).fetchone()
            if existing is not None:
                if existing["decision"] == "pending":
                    connection.execute(
                        """UPDATE proposals SET payload = ?, latest_payload = ?, plan_hash = ?,
                        active = 1, updated_at = ? WHERE id = ?""",
                        (payload, payload, proposal.plan_hash, now, existing["id"]),
                    )
                return existing["id"]
            proposal_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO proposals
                (id, intent_key, payload, latest_payload, plan_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (proposal_id, proposal.key, payload, payload, proposal.plan_hash, now, now),
            )
            return proposal_id
