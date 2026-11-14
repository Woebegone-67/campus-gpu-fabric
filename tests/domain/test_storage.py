import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

from campus_fabric.storage.sqlite import SQLiteStore


def increment(path, count):
    store = SQLiteStore(path)
    for _ in range(count):
        with store.transaction() as conn:
            state = store.load(conn)
            state["counter"] += 1
            state["events"].append(state["counter"])
            store.save(conn, state)


def test_reopen_does_not_replace_existing_state_and_reads_are_copies(tmp_path):
    path = tmp_path / "state.sqlite"
    store = SQLiteStore(path)
    store.initialize({"counter": 0, "events": []})
    increment(path, 1)
    reopened = SQLiteStore(path)
    reopened.initialize({"counter": 999, "events": [999]})
    state = reopened.read()
    assert state == {"counter": 1, "events": [1]}
    state["events"].append(99)
    assert reopened.read()["events"] == [1]


def test_transaction_rollback_keeps_state_events_and_receipt_together(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    store.initialize({"counter": 0, "events": [], "receipts": {}})
    with pytest.raises(RuntimeError, match="crash"):
        with store.transaction() as conn:
            state = store.load(conn)
            state.update(counter=1, events=[1], receipts={"key": "committed"})
            store.save(conn, state)
            raise RuntimeError("crash before commit")
    assert store.read() == {"counter": 0, "events": [], "receipts": {}}


def test_independent_connections_serialize_32_concurrent_writers(tmp_path):
    path = tmp_path / "threads.sqlite"
    store = SQLiteStore(path)
    store.initialize({"counter": 0, "events": []})
    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(lambda _: increment(path, 3), range(32)))
    assert store.read() == {"counter": 96, "events": list(range(1, 97))}


def test_separate_process_writers_cannot_lose_updates(tmp_path):
    path = str(tmp_path / "processes.sqlite")
    store = SQLiteStore(path)
    store.initialize({"counter": 0, "events": []})
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        list(pool.map(increment, [path] * 4, [7] * 4))
    assert store.read() == {"counter": 28, "events": list(range(1, 29))}


def test_unknown_schema_and_nonfinite_state_are_rejected(tmp_path):
    path = tmp_path / "foreign.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE foreign_data(value TEXT)")
    with pytest.raises(RuntimeError, match="Unsupported"):
        SQLiteStore(path)
    store = SQLiteStore(tmp_path / "ours.sqlite")
    with pytest.raises(ValueError):
        store.initialize({"bad": float("nan")})


def test_in_memory_store_is_available_across_transactions():
    store = SQLiteStore(":memory:")
    store.initialize({"counter": 0})
    with store.transaction() as conn:
        store.save(conn, {"counter": 1})
    assert store.read() == {"counter": 1}
    store.close()
