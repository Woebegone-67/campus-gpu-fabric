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


class SQLiteStore:
    SCHEMA_VERSION = 1

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
                conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            elif version != self.SCHEMA_VERSION or tables != {"fabric_state"}:
                raise RuntimeError("Unsupported database schema; refusing to overwrite state")
            columns = [row[1:3] for row in conn.execute("PRAGMA table_info(fabric_state)")]
            if columns != [("singleton", "INTEGER"), ("payload", "TEXT")]:
                raise RuntimeError("Invalid fabric_state schema")

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
            conn.execute(
                "INSERT OR IGNORE INTO fabric_state(singleton,payload) VALUES(1,?)",
                (payload,),
            )
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
        result = conn.execute(
            "UPDATE fabric_state SET payload=? WHERE singleton=1", (self._encode(state),)
        )
        if result.rowcount != 1:
            raise RuntimeError("Store has not been initialized")

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
