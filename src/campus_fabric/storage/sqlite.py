"""Atomic SQLite entity storage with transaction-local lazy state and audit roots.

WAL/FULL and BEGIN IMMEDIATE arbitrate separate-process writers. Entities, derived
query indexes, Merkle nodes and committed journal records share one transaction.
Legacy journal bytes survive migration; ordinary writes never reload full history.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from threading import Condition
from uuid import uuid4

from campus_fabric.storage import journal
from campus_fabric.storage.merkle import MerkleTree, create_schema
from campus_fabric.storage.state import (
    FILTER_FIELDS,
    StateDict,
    flatten,
    index_columns,
    materialize,
    unflatten,
)

JOURNAL_SCHEMA = "fabric-committed-state-v2"
TABLES = {"fabric_units", "fabric_meta", "fabric_journal", "fabric_tree"}
UNIT_COLUMNS = (
    ("key", "TEXT"), ("root_key", "TEXT"), ("kind", "TEXT"),
    ("item_key", "TEXT"), ("item_index", "INTEGER"), ("payload", "TEXT"),
    *((field, "REAL" if field == "unavailable_until" else "TEXT") for field in FILTER_FIELDS),
)


class _WriterAdmission:
    """Bounded FIFO among callers admitted to this store instance, not a DB lock."""

    def __init__(self, capacity=64, timeout_seconds=30.0):
        self.capacity = capacity
        self.timeout_seconds = timeout_seconds
        self.condition = Condition()
        self.queue = deque()

    @staticmethod
    def remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            error.sqlite_errorname = "SQLITE_BUSY"
            raise error
        return remaining

    @contextmanager
    def enter(self):
        deadline = time.monotonic() + self.timeout_seconds
        ticket = object()
        queued = False
        try:
            with self.condition:
                # Overflow callers wait within the same acquisition budget;
                # fairness starts when a caller actually enters this queue.
                while len(self.queue) >= self.capacity:
                    self.condition.wait(self.remaining(deadline))
                self.remaining(deadline)
                self.queue.append(ticket)
                queued = True
                self.condition.notify_all()
                while self.queue[0] is not ticket:
                    self.condition.wait(self.remaining(deadline))
                self.remaining(deadline)
            yield deadline
        finally:
            if queued:
                with self.condition:
                    self.queue.remove(ticket)
                    self.condition.notify_all()


def _record_chain(rows):
    previous = None
    for number, (sequence, payload) in enumerate(rows, 1):
        record = json.loads(payload)
        schema = record.get("schema")
        if schema not in (journal.SCHEMA, JOURNAL_SCHEMA):
            raise RuntimeError("Unsupported committed journal schema")
        first = previous is None
        expected_keys = {"schema", "sequence", "origin", "previous_record_sha256", "changes",
                         "record_sha256"}
        expected_keys.update(("before_sha256", "after_sha256") if schema == journal.SCHEMA
                             else ("before_root", "after_root"))
        if (record.keys() != expected_keys or sequence != number or record["sequence"] != sequence
                or not isinstance(record["changes"], list)
                or record["previous_record_sha256"] != (previous["record_sha256"] if previous else None)
                or record["origin"] not in (("initialize", "migration-baseline") if first else ("write",))
                or record["record_sha256"] != journal.digest(
                    {key: value for key, value in record.items() if key != "record_sha256"})):
            raise RuntimeError("Invalid committed journal chain")
        before_name = "before_sha256" if schema == journal.SCHEMA else "before_root"
        after_name = "after_sha256" if schema == journal.SCHEMA else "after_root"
        if first and record[before_name] is not None:
            raise RuntimeError("Journal initial state must be absent")
        if previous:
            if previous["schema"] == JOURNAL_SCHEMA and schema == journal.SCHEMA:
                raise RuntimeError("Journal cannot revert to legacy format")
            if previous["schema"] == schema and record[before_name] != previous[after_name]:
                raise RuntimeError("Journal state lineage is broken")
        previous = record
    return previous


def _legacy_replay(rows):
    """Validate the entire v1 prefix before migrating its exact stored bytes."""
    _record_chain(rows)
    state = None
    for _, payload in rows:
        record = json.loads(payload)
        if record["schema"] != journal.SCHEMA:
            raise RuntimeError("Legacy database contains a nonlegacy journal record")
        if record["before_sha256"] != (None if state is None else journal.digest(state)):
            raise RuntimeError("Legacy journal before-state mismatch")
        for change in record["changes"]:
            operation, path = change.get("op"), change.get("path")
            if not isinstance(path, list) or any(not isinstance(key, str) for key in path):
                raise RuntimeError("Invalid legacy journal path")
            keys = {"op", "path", "value"} if operation == "set" else (
                {"op", "path", "values"} if operation == "append" else {"op", "path"})
            if change.keys() != keys or operation not in ("set", "append", "delete"):
                raise RuntimeError("Invalid legacy journal change")
            if not path:
                if operation == "set":
                    state = copy.deepcopy(change["value"])
                elif operation == "append" and isinstance(state, list) and isinstance(change["values"], list):
                    state.extend(copy.deepcopy(change["values"]))
                else:
                    raise RuntimeError("Invalid legacy root change")
                continue
            parent = state
            for key in path[:-1]:
                if not isinstance(parent, dict) or key not in parent:
                    raise RuntimeError("Missing legacy journal parent")
                parent = parent[key]
            if not isinstance(parent, dict):
                raise RuntimeError("Invalid legacy journal parent")
            key = path[-1]
            if operation == "set":
                parent[key] = copy.deepcopy(change["value"])
            elif operation == "delete":
                if key not in parent:
                    raise RuntimeError("Missing legacy journal deletion")
                del parent[key]
            elif key in parent and isinstance(parent[key], list) and isinstance(change["values"], list):
                parent[key].extend(copy.deepcopy(change["values"]))
            else:
                raise RuntimeError("Invalid legacy append")
        if not isinstance(state, dict) or record["after_sha256"] != journal.digest(state):
            raise RuntimeError("Legacy journal after-state mismatch")
    return state


class SQLiteStore:
    SCHEMA_VERSION = 3

    def __init__(self, path: str | Path):
        self._writer_admission = _WriterAdmission()
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
            # Switching a legacy database out of DELETE mode can return BUSY
            # immediately even with busy_timeout, when two processes open it.
            deadline = time.monotonic() + 30
            while True:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if (exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                            or time.monotonic() >= deadline):
                        raise
                    time.sleep(0.01)
        finally:
            conn.close()
        try:
            with self.transaction() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if version == 0 and not tables:
                    self._create_schema(conn)
                elif version in (1, 2) and tables == (
                        {"fabric_state"} if version == 1 else {"fabric_state", "fabric_journal"}):
                    self._migrate(conn, version)
                elif version != self.SCHEMA_VERSION or tables not in (TABLES, TABLES | {"fabric_state"}):
                    raise RuntimeError("Unsupported database schema; refusing to overwrite state")
                self._validate_schema(conn)
                conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
                self._audit_current(conn)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            self.close()
            raise RuntimeError("Invalid persisted state or committed journal") from exc
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _columns(conn, table):
        return [row[1:3] for row in conn.execute(f"PRAGMA table_info({table})")]

    @classmethod
    def _validate_schema(cls, conn):
        expected = {
            "fabric_units": list(UNIT_COLUMNS),
            "fabric_meta": [("singleton", "INTEGER"), ("root_key", "TEXT"), ("root_hash", "TEXT")],
            "fabric_journal": [("sequence", "INTEGER"), ("payload", "TEXT")],
            "fabric_tree": [("key", "TEXT"), ("priority", "TEXT"), ("value_hash", "TEXT"),
                            ("left_key", "TEXT"), ("right_key", "TEXT"), ("root_hash", "TEXT")],
        }
        for table, columns in expected.items():
            if cls._columns(conn, table) != columns:
                raise RuntimeError(f"Invalid {table} schema")

    @staticmethod
    def _create_schema(conn):
        filters = ",".join(f"{field} {'REAL' if field == 'unavailable_until' else 'TEXT'}"
                           for field in FILTER_FIELDS)
        conn.execute(
            "CREATE TABLE fabric_units (key TEXT PRIMARY KEY,root_key TEXT NOT NULL,"
            "kind TEXT NOT NULL,item_key TEXT,item_index INTEGER,payload TEXT NOT NULL," + filters + ")")
        conn.execute("CREATE INDEX units_root ON fabric_units(root_key,kind)")
        conn.execute("CREATE INDEX units_markers ON fabric_units(kind,root_key)")
        for field in FILTER_FIELDS:
            conn.execute(f"CREATE INDEX units_{field} ON fabric_units(root_key,kind,{field})")
        # These conjunctions identify one attempt/lease. Independent single-field
        # indexes let SQLite choose the common action/state and scan old history.
        conn.execute("CREATE INDEX units_attempt_action ON "
                     "fabric_units(root_key,kind,attempt_id,action)")
        conn.execute("CREATE INDEX units_allocation_state ON "
                     "fabric_units(root_key,kind,allocation_id,state)")
        conn.execute("CREATE TABLE fabric_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                     "root_key TEXT,root_hash TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS fabric_journal "
                     "(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
        create_schema(conn)

    @staticmethod
    def _write_unit(conn, key, payload):
        derived = index_columns(key, payload)
        values = (key, *derived[:4], payload, *derived[4:])
        names = [name for name, _ in UNIT_COLUMNS]
        conn.execute(
            f"INSERT INTO fabric_units({','.join(names)}) VALUES({','.join('?' for _ in names)}) "
            "ON CONFLICT(key) DO UPDATE SET " +
            ",".join(f"{name}=excluded.{name}" for name in names[1:]), values)

    def _migrate(self, conn, version):
        if self._columns(conn, "fabric_state") != [("singleton", "INTEGER"), ("payload", "TEXT")]:
            raise RuntimeError("Invalid fabric_state schema")
        existing = conn.execute("SELECT payload FROM fabric_state WHERE singleton=1").fetchone()
        state = json.loads(existing[0]) if existing else None
        if state is not None and not isinstance(state, dict):
            raise RuntimeError("Invalid legacy state object")
        rows = []
        if version == 2:
            if self._columns(conn, "fabric_journal") != [("sequence", "INTEGER"), ("payload", "TEXT")]:
                raise RuntimeError("Invalid fabric_journal schema")
            rows = list(conn.execute("SELECT sequence,payload FROM fabric_journal ORDER BY sequence"))
            reconstructed = _legacy_replay(rows)
            if bool(existing) != bool(rows) or not journal.equal(state, reconstructed):
                raise RuntimeError("Legacy journal does not match persisted state")
        self._create_schema(conn)
        if existing is None:
            return
        if version == 1:
            record = {
                "schema": journal.SCHEMA, "sequence": 1, "origin": "migration-baseline",
                "previous_record_sha256": None, "before_sha256": None,
                "after_sha256": journal.digest(state), "changes": journal.changes(None, state),
            }
            record["record_sha256"] = journal.digest(record)
            conn.execute("INSERT INTO fabric_journal(sequence,payload) VALUES(1,?)", (journal.encode(record),))
        tree = MerkleTree(conn)
        for key, payload in sorted(flatten(state).items()):
            self._write_unit(conn, key, payload)
            tree.set(key, payload)
        conn.execute("INSERT INTO fabric_meta VALUES(1,?,?)", (tree.root_key, tree.root_hash))

    def _audit_current(self, conn):
        """Full open-time audit; never called in an ordinary business transaction."""
        rows = list(conn.execute("SELECT sequence,payload FROM fabric_journal ORDER BY sequence"))
        head = _record_chain(rows)
        meta = conn.execute("SELECT root_key,root_hash FROM fabric_meta WHERE singleton=1").fetchone()
        units = dict(conn.execute("SELECT key,payload FROM fabric_units"))
        if meta is None:
            if head or units or conn.execute("SELECT 1 FROM fabric_tree LIMIT 1").fetchone():
                raise RuntimeError("Uninitialized state has committed data")
            return
        if head is None:
            raise RuntimeError("Committed state lacks a journal")
        state = unflatten(units)
        tree = MerkleTree(conn, meta[0])
        tree.verify(units)
        if tree.root_hash != meta[1]:
            raise RuntimeError("Persisted state root mismatch")
        if ((head["schema"] == JOURNAL_SCHEMA and head["after_root"] != meta[1])
                or (head["schema"] == journal.SCHEMA and head["after_sha256"] != journal.digest(state))):
            raise RuntimeError("Committed journal does not match persisted state")
        names = "root_key,kind,item_key,item_index," + ",".join(FILTER_FIELDS)
        for row in conn.execute("SELECT key,payload," + names + " FROM fabric_units"):
            if list(row[2:]) != index_columns(row[0], row[1]):
                raise RuntimeError("Derived entity index disagrees with its authoritative cell")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None, uri=self._uri)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self):
        with self._writer_admission.enter() as deadline:
            conn = self._connect()
            try:
                remaining = self._writer_admission.remaining(deadline)
                conn.execute(f"PRAGMA busy_timeout={max(1, int(remaining * 1000))}")
                conn.execute("BEGIN IMMEDIATE")
                # Admission and BEGIN share the existing acquisition budget.
                # Subsequent SQL retains the original SQLite busy timeout.
                conn.execute("PRAGMA busy_timeout=30000")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def snapshot(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield self.load(conn)
        finally:
            conn.close()

    @staticmethod
    def _encode(state):
        if not isinstance(state, dict):
            raise TypeError("Authoritative state must be an object")
        return journal.encode(materialize(state))

    def initialize(self, initial_state):
        units = flatten(initial_state)
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM fabric_meta WHERE singleton=1").fetchone():
                self._save_changes(conn, units, "initialize")

    def load(self, conn):
        if not conn.execute("SELECT 1 FROM fabric_meta WHERE singleton=1").fetchone():
            raise RuntimeError("Store has not been initialized")
        return StateDict(conn)

    def save(self, conn, state):
        if not conn.in_transaction:
            raise RuntimeError("State writes require an active transaction")
        if not conn.execute("SELECT 1 FROM fabric_meta WHERE singleton=1").fetchone():
            raise RuntimeError("Store has not been initialized")
        if isinstance(state, StateDict) and state.connection is conn:
            changes = state.changed_units()
        else:
            after = flatten(state)
            before = dict(conn.execute("SELECT key,payload FROM fabric_units"))
            changes = {key: after.get(key) for key in before.keys() | after.keys()
                       if before.get(key) != after.get(key)}
        self._save_changes(conn, changes, "write")
        if isinstance(state, StateDict) and state.connection is conn:
            self.last_write_stats.update(
                decoded_rows=sum(container._decoded_rows for container in state._containers.values()),
                touched_rows=sum(len(container._cache) for container in state._containers.values()),
            )
            state.rebase(changes)

    def _save_changes(self, conn, changes, origin):
        meta = conn.execute("SELECT root_key,root_hash FROM fabric_meta WHERE singleton=1").fetchone()
        row = conn.execute("SELECT sequence,payload FROM fabric_journal ORDER BY sequence DESC LIMIT 1").fetchone()
        previous = json.loads(row[1]) if row else None
        tree = MerkleTree(conn, meta[0] if meta else None)
        before_root = meta[1] if meta else None
        if meta and tree.root_hash != before_root:
            raise RuntimeError("State root does not match its index")
        if previous and previous["schema"] == JOURNAL_SCHEMA and previous["after_root"] != before_root:
            raise RuntimeError("Journal head does not match the state being changed")
        edits = []
        for key, payload in sorted(changes.items()):
            if payload is None:
                conn.execute("DELETE FROM fabric_units WHERE key=?", (key,))
                tree.delete(key)
                edits.append({"op": "delete", "key": key})
            else:
                self._write_unit(conn, key, payload)
                tree.set(key, payload)
                edits.append({"op": "set", "key": key, "value": json.loads(payload)})
        record = {
            "schema": JOURNAL_SCHEMA, "sequence": row[0] + 1 if row else 1, "origin": origin,
            "previous_record_sha256": previous["record_sha256"] if previous else None,
            "before_root": before_root, "after_root": tree.root_hash, "changes": edits,
        }
        record["record_sha256"] = journal.digest(record)
        conn.execute("INSERT INTO fabric_journal(sequence,payload) VALUES(?,?)",
                     (record["sequence"], journal.encode(record)))
        conn.execute("INSERT INTO fabric_meta(singleton,root_key,root_hash) VALUES(1,?,?) "
                     "ON CONFLICT(singleton) DO UPDATE SET root_key=excluded.root_key,root_hash=excluded.root_hash",
                     (tree.root_key, tree.root_hash))
        # Per-store last-write diagnostics only; never persisted or read by domain logic.
        self.last_write_stats = {
            "changed_units": len(changes),
            "entity_rows_written": sum(len(json.loads(key)) == 2 for key in changes),
            "merkle_loaded_nodes": tree.loaded_nodes,
            "merkle_written_nodes": tree.written_nodes,
        }

    def export_journal(self):
        """Local full audit export in one coherent snapshot, outside public queries."""
        with self.snapshot() as state:
            conn = state.connection
            entries = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM fabric_journal ORDER BY sequence")]
            return {
                "schema": JOURNAL_SCHEMA, "entries": entries,
                "head_sequence": entries[-1]["sequence"],
                "head_record_sha256": entries[-1]["record_sha256"],
                "state_root": conn.execute("SELECT root_hash FROM fabric_meta WHERE singleton=1").fetchone()[0],
                "state_sha256": journal.digest(materialize(state)),
            }

    def read(self):
        with self.snapshot() as state:
            return materialize(state)

    def close(self):
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None
