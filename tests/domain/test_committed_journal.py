"""Persistence/replay tests include real crashes and a schema-1 migration."""
from __future__ import annotations

import copy
import importlib.util
import json
import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from campus_fabric.storage.sqlite import SQLiteStore

REPLAY_PATH = Path(__file__).resolve().parents[1] / "acceptance/cp3/cp3_state_replay.py"
spec = importlib.util.spec_from_file_location("independent_state_replay", REPLAY_PATH)
independent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(independent)


def test_complete_fields_deletions_list_replacement_and_numeric_types(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    original = {"epoch": 0, "intents": {}, "outbox": {}, "callbacks": {}, "dedup": {},
                "payloads": {}, "events": [], "nullable": None, "value": 0}
    store.initialize(original)
    observations = {1: independent.checksum(store.read())}
    states = [
        {**original, "intents": {"w": {"stages": [{"receipt": {"revision": 2}}]}},
         "outbox": {"d": {"action": "start", "acknowledged": False}},
         "callbacks": {"c": {"terminal": False}}, "dedup": {"key": {"receipt": "r"}},
         "payloads": {"r": {"body": {"success": True}}}, "events": [{"seq": 1}], "value": 0.0},
        {"epoch": 1, "intents": {}, "events": [], "value": False},
    ]
    for number, state in enumerate(states, 2):
        with store.transaction() as connection:
            store.save(connection, state)
        observations[number] = independent.checksum(store.read())
    rebuilt = independent.replay(store.export_journal(), observed_prefixes=observations)
    assert rebuilt == store.read() == states[-1]
    for number in range(1, len(states) + 2):
        journal = store.export_journal()
        journal["entries"] = journal["entries"][:number]
        journal.update(head_sequence=number,
                       head_record_sha256=journal["entries"][-1]["record_sha256"],
                       state_sha256=journal["entries"][-1]["after_sha256"])
        assert independent.checksum(independent.replay(journal)) == observations[number]


def test_rollback_keeps_state_and_journal_atomic(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    store.initialize({"value": 0})
    before = store.export_journal()
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction() as connection:
            store.save(connection, {"value": 1})
            raise RuntimeError("abort")
    assert store.read() == {"value": 0}
    assert store.export_journal() == before


@pytest.mark.parametrize('before_value,after_value', [(0.0, -0.0), (-0.0, 0.0)])
def test_signed_zero_scalar_and_list_prefix_are_losslessly_replayed(tmp_path, before_value, after_value):
    store = SQLiteStore(tmp_path / 'signed-zero.sqlite')
    store.initialize({'scalar': before_value, 'rows': [{'value': before_value}]})
    expected = {'scalar': after_value, 'rows': [{'value': after_value}, {'appended': True}]}
    with store.transaction() as connection:
        store.save(connection, expected)
    journal = store.export_journal()
    rebuilt = independent.replay(journal)
    assert independent.checksum(rebuilt) == independent.checksum(expected)
    # A prefix equality that ignores the sign would append while retaining the
    # old zero. The independent reducer must reject that internally rehashed log.
    bad = copy.deepcopy(journal)
    bad['entries'][-1]['changes'] = [{'op': 'append', 'path': ['rows'], 'values': [{'appended': True}]}]
    record = bad['entries'][-1]
    record['record_sha256'] = independent.checksum({k: v for k, v in record.items() if k != 'record_sha256'})
    bad['head_record_sha256'] = record['record_sha256']
    with pytest.raises(ValueError, match='state'):
        independent.replay(bad)


def test_private_executor_bookkeeping_and_reset_are_reconstructible(tmp_path):
    """Actual trusted ports, synthetic observations; not real CPU execution proof."""
    from campus_fabric.contracts import models as m
    from campus_fabric.domain.kernel import Kernel
    from campus_fabric.workflows.experiment_runner import _actor, _registration
    from campus_fabric.workflows.orchestrator import WorkflowOrchestrator

    root = Path(__file__).resolve().parents[2]
    inventory = tuple(m.NodeInventory.model_validate_json(json.dumps(node)) for node in
                      json.loads((root / "configs/cp1_inventory.json").read_text())["nodes"])
    kernel = Kernel(tmp_path / "cpu-protocol.sqlite", inventory=inventory,
                    execution_mode="cpu_reference")
    registration = _registration({"intent_id": "journal-work", "owner_id": "alice",
                                  "gpu_model": "A10", "timeout_seconds": 100}).model_copy(
                                      update={"execution_mode": "cpu_reference"})
    workflow = WorkflowOrchestrator(kernel, kernel)
    result = workflow.start(registration, kernel.decision_snapshot().version,
                            actor=_actor("alice"), request_id="register-journal-work")
    assert all(receipt.outcome != "rejected" for receipt in result.receipts)
    workflow.advance("journal-work", actor=_actor("alice"))
    observations = {}

    def observe():
        export = kernel.store.export_journal()
        state = kernel.store.read()
        observations[export["head_sequence"]] = independent.checksum(state)
        assert independent.replay(export, observed_prefixes=observations) == state
        return state

    started = observe()
    assert started["outbox"] and started["jobs"]
    dispatch_id = next(iter(started["outbox"]))
    kernel.acknowledge_dispatch(dispatch_id, started["epoch"])
    delivered = observe()
    assert delivered["revision"] == started["revision"]
    assert delivered["outbox"][dispatch_id]["status"] == "delivered"
    kernel.sync_cpu_clock("synthetic-boot", 1_000_000_000)
    anchored = observe()
    assert anchored["cpu_clock"]["monotonic_ns"] == 1_000_000_000
    job = next(iter(anchored["jobs"].values()))
    event = m.ExecutorEvent(callback_id="synthetic-start", version=kernel.decision_snapshot().version,
                            attempt=m.AttemptRef.model_validate(job["attempt"]), kind="started",
                            mode="cpu_reference", observed_at=anchored["now"])
    receipt = kernel.commit(m.CommandEnvelope(
        request_id="protocol-start", actor=m.AuthenticatedActor(actor_id="driver", role="system"),
        operation="executor.report", parameters=m.ExecutorReportParameters(kind="executor.report", event=event)))
    assert receipt.outcome != "rejected"
    assert observe()["callbacks"]["synthetic-start"]["kind"] == "started"
    receipt = kernel.commit(m.CommandEnvelope(
        request_id="protocol-reset", actor=m.AuthenticatedActor(actor_id="test", role="test_driver"),
        operation="test.reset", parameters=m.ResetParameters(kind="test.reset")))
    assert receipt.outcome != "rejected"
    assert observe()["retired"]
    assert kernel.reconcile_retired(event.model_copy(update={"kind": "stopped", "callback_id": "synthetic-stop"}))
    final = observe()
    assert not final["retired"] and not final["callbacks"] and not final["intents"]


def test_old_schema_migrates_once_with_exact_baseline_under_concurrency(tmp_path):
    path = tmp_path / "legacy.sqlite"
    state = {"epoch": 4, "intents": {"old-work": {"stages": [1, 2]}}, "dedup": {"x": "saved"}}
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fabric_state (singleton INTEGER PRIMARY KEY "
                           "CHECK(singleton=1), payload TEXT NOT NULL)")
        connection.execute("INSERT INTO fabric_state VALUES(1,?)", (json.dumps(state),))
        connection.execute("PRAGMA user_version=1")
    def reopen(_):
        store = SQLiteStore(path)
        store.initialize({"must_not_replace": True})
        return store.read(), store.export_journal()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reopen, range(8)))
    for actual, journal in results:
        assert actual == state == independent.replay(journal)
        assert len(journal["entries"]) == 1
        assert journal["entries"][0]["origin"] == "migration-baseline"


def test_32_concurrent_writes_have_one_contiguous_replayable_history(tmp_path):
    path = tmp_path / "writers.sqlite"
    store = SQLiteStore(path)
    store.initialize({"counter": 0, "events": []})

    def write(_):
        other = SQLiteStore(path)
        with other.transaction() as connection:
            state = other.load(connection)
            state["counter"] += 1
            state["events"].append(state["counter"])
            other.save(connection, state)

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(write, range(32)))
    journal = store.export_journal()
    assert journal["head_sequence"] == 33
    assert independent.replay(journal) == store.read() == {
        "counter": 32, "events": list(range(1, 33))}


def killed_journal_writer(path, window, pipe):
    store = SQLiteStore(path)
    with store.transaction() as connection:
        store.save(connection, {"value": 1, "receipts": {"key": "saved"}})
        if window == "before":
            pipe.send("barrier")
            pipe.recv()
    if window == "after":
        pipe.send("barrier")
        pipe.recv()


@pytest.mark.parametrize("window", ["before", "after"])
def test_sigkill_on_both_sides_of_commit_preserves_replay(tmp_path, window):
    path = tmp_path / "crash.sqlite"
    store = SQLiteStore(path)
    store.initialize({"value": 0, "receipts": {}})
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=killed_journal_writer, args=(str(path), window, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15), "Writer did not reach the transaction boundary"
        assert parent.recv() == "barrier"
        visible = store.read()
        assert visible["value"] == (window == "after")
        assert independent.replay(store.export_journal()) == visible
        process.kill()
        process.join(10)
        assert process.exitcode == -9
        recovered = SQLiteStore(path)
        assert independent.replay(recovered.export_journal()) == recovered.read() == visible
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()


@pytest.mark.parametrize("damage", ["omit", "reorder", "payload", "head", "observation"])
def test_independent_reducer_rejects_damage_and_wrong_prefix_oracle(tmp_path, damage):
    store = SQLiteStore(tmp_path / "state.sqlite")
    store.initialize({"intents": {}})
    for value in (1, 2):
        with store.transaction() as connection:
            store.save(connection, {"intents": {"w": {"stages": [value]}}})
    journal = copy.deepcopy(store.export_journal())
    observed = {}
    if damage == "omit":
        journal["entries"].pop(1)
    elif damage == "reorder":
        journal["entries"][1:] = journal["entries"][1:][::-1]
    elif damage == "payload":
        journal["entries"][-1]["changes"][0]["value"] = "lost workflow stage"
    elif damage == "head":
        journal["entries"].pop()
    else:
        observed[2] = independent.checksum({"intents": {}})
    with pytest.raises(ValueError):
        independent.replay(journal, observed_prefixes=observed)
