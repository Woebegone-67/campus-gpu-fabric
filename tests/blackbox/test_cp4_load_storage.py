"""Load observations must follow current authority across storage generations."""

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from campus_fabric.storage.sqlite import SQLiteStore
from experiments import cp4_load as load


def initial_state():
    return {"epoch": 0, "revision": 1, "now": 0, "nodes": {}, "allocations": {},
            "jobs": {}, "events": [], "extension": {"preserved": [None, True, 1.5]}}


def database_path(directory):
    path = directory / "database/fabric.sqlite3"
    path.parent.mkdir(parents=True)
    return path


def legacy_database(path, state, version=1):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fabric_state (singleton INTEGER PRIMARY KEY "
                           "CHECK(singleton=1), payload TEXT NOT NULL)")
        connection.execute("INSERT INTO fabric_state VALUES(1,?)", (json.dumps(state),))
        connection.execute(f"PRAGMA user_version={version}")
        if version == 2:
            # The read-only observer need not load or trust the legacy journal.
            connection.execute("CREATE TABLE fabric_journal "
                               "(sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL)")


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_load_observation_still_reads_complete_state(tmp_path, version):
    expected = initial_state()
    legacy_database(database_path(tmp_path), expected, version)
    assert load.read_state(tmp_path) == expected


@pytest.mark.parametrize("migrated", [False, True])
def test_entity_observation_uses_current_cells_not_retained_legacy_state(tmp_path, monkeypatch, migrated):
    path, old = database_path(tmp_path), initial_state()
    if migrated:
        legacy_database(path, old)
    store = SQLiteStore(path)
    try:
        store.initialize(old)
        expected = copy.deepcopy(old)
        expected.update(revision=2, now=10)
        expected["jobs"]["j1"] = {"job_id": "j1", "state": "Cancelled"}
        expected["events"].append({"event_type": "job.cancelled", "sequence": 1})
        with store.transaction() as connection:
            store.save(connection, expected)
        if migrated:
            with sqlite3.connect(path) as connection:
                retained = json.loads(connection.execute(
                    "SELECT payload FROM fabric_state WHERE singleton=1").fetchone()[0])
            assert retained == old and retained != expected

        def forbidden_production_read(*args, **kwargs):
            raise AssertionError("Evidence observation must not reuse a production serializer")

        monkeypatch.setattr(SQLiteStore, "read", forbidden_production_read)
        monkeypatch.setattr(SQLiteStore, "load", forbidden_production_read)
        monkeypatch.setattr(SQLiteStore, "export_journal", forbidden_production_read)
        assert load.read_state(tmp_path) == expected
    finally:
        store.close()


def test_entity_observation_remains_one_snapshot_during_a_commit(tmp_path, monkeypatch):
    store = SQLiteStore(database_path(tmp_path))
    old = initial_state()
    store.initialize(old)
    new = copy.deepcopy(old)
    new.update(revision=2, now=10)
    new["jobs"]["j1"] = {"job_id": "j1", "state": "Cancelled"}
    new["events"].append({"event_type": "job.cancelled", "sequence": 1})
    captured, committed = Event(), Event()
    independent_reader = load.read_sql_state

    def read_across_commit(connection):
        # Establish the observer's actual SQLite snapshot, then let another
        # connection commit before independently reconstructing the entities.
        connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        captured.set()
        assert committed.wait(5), "Concurrent writer did not finish"
        return independent_reader(connection)

    def write_next_state():
        try:
            assert captured.wait(5), "Observer did not establish its snapshot"
            with store.transaction() as connection:
                store.save(connection, new)
        finally:
            committed.set()

    try:
        monkeypatch.setattr(load, "read_sql_state", read_across_commit)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(write_next_state)
            observed = load.read_state(tmp_path)
            future.result(timeout=5)
        assert observed == old
        monkeypatch.setattr(load, "read_sql_state", independent_reader)
        assert load.read_state(tmp_path) == new
    finally:
        store.close()


def test_malformed_current_cells_cannot_fall_back_to_valid_legacy_state(tmp_path):
    path = database_path(tmp_path)
    legacy_database(path, initial_state())
    store = SQLiteStore(path)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE fabric_units SET payload=? WHERE key=?",
                           ('["scalar", 999]', '["revision"]'))
    with pytest.raises(ValueError, match="not canonical"):
        load.read_state(tmp_path)
