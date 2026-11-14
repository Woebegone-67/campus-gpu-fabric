"""Per-store writer FIFO preserves SQLite's cross-process transaction authority."""

import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from campus_fabric.storage import sqlite as store_module
from campus_fabric.storage.sqlite import SQLiteStore, _WriterAdmission


def wait_for_queued(store, count):
    admission = store._writer_admission
    with admission.condition:
        assert admission.condition.wait_for(lambda: len(admission.queue) == count, timeout=2)


def append(store, value, entered=None, release=None):
    with store.transaction() as connection:
        if entered is not None:
            entered.set()
            assert release.wait(3), "Test writer was not released"
        state = store.load(connection)
        state["order"].append(value)
        store.save(connection, state)


def test_actual_enqueue_order_is_fifo_and_later_writer_cannot_barge(tmp_path):
    store = SQLiteStore(tmp_path / "fifo.sqlite")
    store.initialize({"order": []})
    entered, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            with store.transaction():
                first = pool.submit(append, store, "first", entered, release)
                wait_for_queued(store, 2)
                second = pool.submit(append, store, "second")
                wait_for_queued(store, 3)
                third = pool.submit(append, store, "third")
                wait_for_queued(store, 4)
            assert entered.wait(2)
            late = pool.submit(append, store, "late")
            wait_for_queued(store, 4)
            release.set()
            for future in (first, second, third, late):
                future.result(timeout=3)
        finally:
            release.set()
    assert store.read()["order"] == ["first", "second", "third", "late"]
    assert not store._writer_admission.queue


def test_full_admission_waits_for_space_without_growing_or_failing(tmp_path):
    store = SQLiteStore(tmp_path / "capacity.sqlite")
    store.initialize({"order": []})
    store._writer_admission = _WriterAdmission(capacity=2, timeout_seconds=2)
    attempted = Event()

    def overflow():
        attempted.set()
        append(store, "overflow")

    with ThreadPoolExecutor(max_workers=2) as pool:
        with store.transaction():
            queued = pool.submit(append, store, "queued")
            wait_for_queued(store, 2)
            extra = pool.submit(overflow)
            assert attempted.wait(1)
            assert not extra.done()
            with store._writer_admission.condition:
                assert len(store._writer_admission.queue) == 2
        queued.result(timeout=3)
        extra.result(timeout=3)
    assert store.read()["order"] == ["queued", "overflow"]


@pytest.mark.parametrize("capacity", [1, 2])
def test_timeout_leaves_no_ticket_and_does_not_block_following_writer(tmp_path, capacity):
    store = SQLiteStore(tmp_path / "timeout.sqlite")
    store.initialize({"order": []})
    store._writer_admission = _WriterAdmission(capacity=capacity, timeout_seconds=0.1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction():
            waiting = pool.submit(append, store, "timed-out")
            with pytest.raises(sqlite3.OperationalError, match="database is locked") as error:
                waiting.result(timeout=2)
            assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert len(store._writer_admission.queue) == 1
        append(store, "following")
    assert store.read()["order"] == ["following"]


def test_admission_and_sqlite_begin_share_one_acquisition_budget(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "budget.sqlite")
    store.initialize({"order": []})
    clock, statements = {"now": 0.0}, []
    original = store._connect

    def connect():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection

    # Advance only this module's clock after a real waiter has entered the queue.
    monkeypatch.setattr(store_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(store, "_connect", connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction():
            waiting = pool.submit(append, store, "following")
            wait_for_queued(store, 2)
            clock["now"] = 5.0
        waiting.result(timeout=2)
    assert "PRAGMA busy_timeout=25000" in statements
    assert statements.count("PRAGMA busy_timeout=30000") == 3
    assert store.read()["order"] == ["following"]


def test_snapshot_reads_do_not_enter_writer_admission(tmp_path):
    store = SQLiteStore(tmp_path / "reader.sqlite")
    store.initialize({"order": []})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction() as connection:
            state = store.load(connection)
            state["order"].append("uncommitted")
            store.save(connection, state)
            assert pool.submit(store.read).result(timeout=2) == {"order": []}
            assert len(store._writer_admission.queue) == 1
    assert store.read() == {"order": ["uncommitted"]}


@pytest.mark.parametrize("failure", ["connect", "begin", "body", "commit", "rollback", "close"])
def test_every_transaction_exit_releases_admission(tmp_path, monkeypatch, failure):
    store = SQLiteStore(tmp_path / "exceptions.sqlite")
    store.initialize({"order": []})
    original = store._connect

    class BrokenConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if failure == "begin" and sql == "BEGIN IMMEDIATE":
                raise RuntimeError("injected begin")
            return self.connection.execute(sql, *args)

        def commit(self):
            if failure == "commit":
                raise RuntimeError("injected commit")
            return self.connection.commit()

        def rollback(self):
            self.connection.rollback()
            if failure == "rollback":
                raise RuntimeError("injected rollback")

        def close(self):
            self.connection.close()
            if failure == "close":
                raise RuntimeError("injected close")

    def connect():
        if failure == "connect":
            raise RuntimeError("injected connect")
        return BrokenConnection(original())

    monkeypatch.setattr(store, "_connect", connect)
    with pytest.raises(RuntimeError, match="injected"):
        with store.transaction() as connection:
            state = store.load(connection)
            state["order"].append("attempt")
            store.save(connection, state)
            if failure in {"body", "rollback"}:
                raise RuntimeError("injected body")
    assert not store._writer_admission.queue
    monkeypatch.undo()
    append(store, "following")
    expected = ["attempt", "following"] if failure == "close" else ["following"]
    assert store.read()["order"] == expected


def test_base_exception_also_releases_admission(tmp_path):
    store = SQLiteStore(tmp_path / "interrupt.sqlite")
    store.initialize({"order": []})

    class Interrupted(BaseException):
        pass

    with pytest.raises(Interrupted), store.transaction():
        raise Interrupted()
    append(store, "following")
    assert store.read()["order"] == ["following"]


def process_writer(path, start, ready, output, label):
    try:
        store = SQLiteStore(path)
        ready.put(label)
        assert start.wait(5)
        for _ in range(16):
            append(store, label)
        output.put((label, None))
    except BaseException as error:
        output.put((label, repr(error)))


def test_separate_process_stores_still_commit_without_lost_updates(tmp_path):
    path = tmp_path / "processes.sqlite"
    store = SQLiteStore(path)
    store.initialize({"order": []})
    context = multiprocessing.get_context("spawn")
    start, ready, output = context.Event(), context.Queue(), context.Queue()
    processes = [
        context.Process(target=process_writer, args=(path, start, ready, output, label))
        for label in ("a", "b")
    ]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=5) for _ in processes} == {"a", "b"}
        start.set()
        assert {output.get(timeout=10) for _ in processes} == {("a", None), ("b", None)}
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        ready.close()
        output.close()
    reopened = SQLiteStore(path)  # Audit the committed cells/tree/journal after both writers.
    order = reopened.read()["order"]
    assert len(order) == 32 and order.count("a") == order.count("b") == 16
    with reopened.snapshot() as state:
        assert state.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert state.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
