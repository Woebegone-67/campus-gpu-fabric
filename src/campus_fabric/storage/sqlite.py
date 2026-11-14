"""SQLite serialization boundary for the authoritative CP1 state.

The state contains inventory, claims, events, timers and deduplication receipts.
Saving that value in the same transaction makes their visibility atomic. SQLite,
not a Python process lock, arbitrates writers from separate server processes.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from campus_fabric.storage import journal


class SQLiteStore:
    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path):
        self._anchor = None
        self._uri = str(path) == ":memory:"
        if self._uri:
            self.path = f"file:fabric-{uuid4().hex}?mode=memory&cache=shared"
            self._anchor = self._connect()
        else:
            resolved = Path(path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(resolved)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
        with self.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and not tables:
                conn.execute(
                    "CREATE TABLE fabric_state (singleton INTEGER PRIMARY KEY "
                    "CHECK(singleton=1), payload TEXT NOT NULL)"
                )
            elif not ((version == 1 and tables == {"fabric_state"}) or
                      (version == self.SCHEMA_VERSION and
                       tables == {"fabric_state", "fabric_journal"})):
                raise RuntimeError("Unsupported database schema; refusing to overwrite state")
            columns = [row[1:3] for row in conn.execute("PRAGMA table_info(fabric_state)")]
            if columns != [("singleton", "INTEGER"), ("payload", "TEXT")]:
                raise RuntimeError("Invalid fabric_state schema")
            if version in (0, 1):
                conn.execute(
                    "CREATE TABLE fabric_journal (sequence INTEGER PRIMARY KEY, "
                    "payload TEXT NOT NULL)"
                )
                existing = conn.execute(
                    "SELECT payload FROM fabric_state WHERE singleton=1"
                ).fetchone()
                if existing is not None:
                    # Old history cannot be invented. Migration records the exact
                    # existing state as an explicitly labelled replay baseline.
                    self._append_journal(conn, None, json.loads(existing[0]), "migration-baseline")
                conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            journal_columns = [row[1:3] for row in conn.execute("PRAGMA table_info(fabric_journal)")]
            if journal_columns != [("sequence", "INTEGER"), ("payload", "TEXT")]:
                raise RuntimeError("Invalid fabric_journal schema")
            state = conn.execute("SELECT payload FROM fabric_state WHERE singleton=1").fetchone()
            head = conn.execute(
                "SELECT payload FROM fabric_journal ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if bool(state) != bool(head) or (state and
                    journal.digest(json.loads(state[0])) != json.loads(head[0])["after_sha256"]):
                raise RuntimeError("Committed journal does not match persisted state")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None, uri=self._uri)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _encode(state: dict) -> str:
        if not isinstance(state, dict):
            raise TypeError("Authoritative state must be an object")
        return json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def initialize(self, initial_state: dict) -> None:
        payload = self._encode(initial_state)
        with self.transaction() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO fabric_state(singleton,payload) VALUES(1,?)",
                (payload,),
            )
            if inserted.rowcount == 1:
                self._append_journal(conn, None, initial_state, "initialize")
            self.load(conn)

    def load(self, conn: sqlite3.Connection) -> dict:
        row = conn.execute("SELECT payload FROM fabric_state WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("Store has not been initialized")
        state = json.loads(row[0])
        if not isinstance(state, dict):
            raise RuntimeError("Invalid persisted state object")
        return state

    def save(self, conn: sqlite3.Connection, state: dict) -> None:
        if not conn.in_transaction:
            raise RuntimeError("State writes require an active transaction")
        payload = self._encode(state)
        before = self.load(conn)
        result = conn.execute(
            "UPDATE fabric_state SET payload=? WHERE singleton=1", (payload,)
        )
        if result.rowcount != 1:
            raise RuntimeError("Store has not been initialized")
        self._append_journal(conn, before, state, "write")

    @staticmethod
    def _append_journal(conn, before, after, origin):
        row = conn.execute(
            "SELECT sequence,payload FROM fabric_journal ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = json.loads(row[1]) if row else None
        before_hash = None if before is None else journal.digest(before)
        if previous is not None and previous["after_sha256"] != before_hash:
            raise RuntimeError("Journal head does not match the state being changed")
        record = {
            "schema": journal.SCHEMA,
            "sequence": row[0] + 1 if row else 1,
            "origin": origin,
            "previous_record_sha256": previous["record_sha256"] if previous else None,
            "before_sha256": before_hash,
            "after_sha256": journal.digest(after),
            "changes": journal.changes(before, after),
        }
        record["record_sha256"] = journal.digest(record)
        conn.execute("INSERT INTO fabric_journal(sequence,payload) VALUES(?,?)",
                     (record["sequence"], journal.encode(record)))

    def export_journal(self) -> dict:
        """Local audit export in one read snapshot; never a public/Owner query."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            state = self.load(conn)
            entries = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM fabric_journal ORDER BY sequence")]
            return {"schema": journal.SCHEMA, "entries": entries,
                    "head_sequence": entries[-1]["sequence"],
                    "head_record_sha256": entries[-1]["record_sha256"],
                    "state_sha256": journal.digest(state)}
        finally:
            conn.close()

    def read(self) -> dict:
        # One SELECT sees a coherent committed version without reserving the writer.
        conn = self._connect()
        try:
            return self.load(conn)
        finally:
            conn.close()

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None
