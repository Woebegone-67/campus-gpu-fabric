"""Real SQLite writer contention must not stall unrelated public HTTP reads."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, Timer

import httpx
import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.workflows.devhost import create_app

ALICE = {"Authorization": "Bearer alice-token", "X-Owner": "alice"}
TEST = {"X-Test-Key": "isolated-test"}
TARGET = "GET /v1/capacity"
DRIVER = m.AuthenticatedActor(actor_id="isolated-test", role="test_driver")


def product_app(directory):
    resources = {"cpu_millicores": 8000, "memory_mib": 16000,
                 "ephemeral_storage_mib": 20000, "gpu_count": 0, "gpu_memory_mib": 0}
    node = m.NodeInventory.model_validate_json(json.dumps({
        "node_id": "node-1", "contributor_owner_id": "supplier", "state": "Ready",
        "capacity": resources, "available": resources, "observed_at": 0, "devices": [],
    }))
    return create_app(data_dir=directory, inventory=(node,), worker_enabled=False,
                      test_key=TEST["X-Test-Key"], auth_tokens={
                          "alice-token": {"actor_id": "alice", "owner_id": "alice",
                                          "role": "researcher"}})


async def create_job(client):
    allocation = await client.post("/v1/allocations", headers=ALICE, json={
        "owner": "alice", "request": {"cpu_millicores": 1000, "memory_mib": 1024,
                                        "gpu": {"count": 0}}, "ttl_seconds": 300})
    assert allocation.status_code == 201, allocation.text
    job = await client.post("/v1/jobs", headers=ALICE, json={
        "allocation_id": allocation.json()["id"], "image": "example", "command": ["work"]})
    assert job.status_code == 201, job.text
    return job.json()["id"]


@contextmanager
def held_writer(store):
    """Hold an actual writer connection; watchdog bounds a blocked-loop regression."""
    entered, release = Event(), Event()

    def hold():
        with store.transaction():
            entered.set()
            assert release.wait(10), "Test writer was not released"

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(hold)
        assert entered.wait(2), "Test writer did not acquire SQLite's write lock"
        watchdog = Timer(5, release.set)
        watchdog.start()
        try:
            yield release
        finally:
            release.set()
            watchdog.cancel()
            writer.result(timeout=2)


@pytest.mark.parametrize("rule", [None, {"mode": "none", "count": 7},
                                  {"mode": "503", "count": 0}])
def test_inactive_fault_public_reads_finish_while_sqlite_writer_is_held(tmp_path, rule):
    app = product_app(tmp_path)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            jid = await create_job(client)
            if rule:
                response = await client.post("/_test/fault", headers=TEST,
                                             json={"operation": TARGET, **rule})
                assert response.status_code == 204
            with held_writer(app.state.kernel.store) as release:
                capacity, job = await asyncio.gather(
                    client.get("/v1/capacity", headers=ALICE),
                    client.get(f"/v1/jobs/{jid}", headers=ALICE))
                assert not release.is_set(), "Public reads waited for the unrelated writer"
                assert capacity.status_code == job.status_code == 200
                assert job.json()["id"] == jid

    asyncio.run(scenario())


@pytest.mark.parametrize("target", [TARGET, "GET /v1/jobs/{job_id}"])
def test_active_fault_waits_off_loop_without_blocking_other_public_reads(
        tmp_path, monkeypatch, target):
    app = product_app(tmp_path)
    entered = Event()
    original = app.state.kernel.take_fault

    def observed_take_fault(operation):
        if operation == target:
            entered.set()
        return original(operation)

    monkeypatch.setattr(app.state.kernel, "take_fault", observed_take_fault)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            jid = await create_job(client)
            job_path = f"/v1/jobs/{jid}"
            fault_path, other_path = (("/v1/capacity", job_path) if target == TARGET
                                      else (job_path, "/v1/capacity"))
            response = await client.post("/_test/fault", headers=TEST,
                                         json={"operation": target, "mode": "503", "count": 1})
            assert response.status_code == 204
            entered.clear()
            with held_writer(app.state.kernel.store) as release:
                faulted = asyncio.create_task(client.get(fault_path, headers=ALICE))
                assert await asyncio.to_thread(entered.wait, 2)
                healthy = await client.get(other_path, headers=ALICE)
                unauthorized = await client.get(fault_path, headers={
                    "Authorization": "Bearer invalid", "X-Owner": "alice"})
                assert not release.is_set(), "Fault consumption blocked the event loop"
                assert healthy.status_code == 200
                assert unauthorized.status_code == 401
                assert not faulted.done(), "Active fault bypassed its atomic write claim"
                release.set()
                assert (await faulted).status_code == 503
            assert (await client.get(fault_path, headers=ALICE)).status_code == 200

    asyncio.run(scenario())


def configure(kernel, *, mode="503", count=1):
    receipt = kernel.commit(m.CommandEnvelope(
        request_id=f"configure-{mode}-{count}", actor=DRIVER, operation="test.fault",
        parameters=m.FaultParameters(kind="test.fault", target_operation=TARGET,
                                     mode=mode, count=count)))
    assert receipt.outcome == "committed"


def test_two_positive_read_checks_cannot_consume_one_fault_twice(tmp_path, monkeypatch):
    first = Kernel(tmp_path / "state.sqlite")
    second = Kernel(tmp_path / "state.sqlite")
    configure(first)
    checked = Barrier(2)

    def synchronized_snapshot(original):
        @contextmanager
        def snapshot():
            with original() as state:
                yield state
            checked.wait(timeout=3)
        return snapshot

    for kernel in (first, second):
        monkeypatch.setattr(kernel.store, "snapshot",
                            synchronized_snapshot(kernel.store.snapshot))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(kernel.take_fault, TARGET) for kernel in (first, second)]
        results = [future.result(timeout=5) for future in futures]
    assert results.count({"mode": "503", "delay_ms": 0}) == 1
    assert results.count(None) == 1
    monkeypatch.undo()
    assert first.take_fault(TARGET) is None
    assert first.store.read()["faults"][TARGET]["remaining"] == 0


@pytest.mark.parametrize("change", ["replace", "disable", "reset", "consume"])
def test_positive_read_rechecks_rule_after_competing_write(tmp_path, monkeypatch, change):
    first = Kernel(tmp_path / "state.sqlite")
    second = Kernel(tmp_path / "state.sqlite")
    configure(first)
    original = first.store.snapshot

    @contextmanager
    def changed_after_read():
        with original() as state:
            yield state
        if change == "replace":
            configure(second, mode="429", count=2)
        elif change == "disable":
            configure(second, mode="none")
        elif change == "reset":
            second.commit(m.CommandEnvelope(request_id="reset", actor=DRIVER,
                operation="test.reset", parameters=m.ResetParameters(kind="test.reset")))
        else:
            assert second.take_fault(TARGET) == {"mode": "503", "delay_ms": 0}

    monkeypatch.setattr(first.store, "snapshot", changed_after_read)
    expected = {"mode": "429", "delay_ms": 0} if change == "replace" else None
    assert first.take_fault(TARGET) == expected
    monkeypatch.undo()
    if change == "replace":
        assert second.take_fault(TARGET) == expected
    assert second.take_fault(TARGET) is None


def test_negative_read_linearizes_before_later_configuration(tmp_path, monkeypatch):
    first = Kernel(tmp_path / "state.sqlite")
    second = Kernel(tmp_path / "state.sqlite")
    original = first.store.snapshot

    @contextmanager
    def configured_after_read():
        with original() as state:
            yield state
        configure(second)

    monkeypatch.setattr(first.store, "snapshot", configured_after_read)
    assert first.take_fault(TARGET) is None
    monkeypatch.undo()
    # The configuration committed during the earlier check, after its snapshot.
    # It remains available exactly once to a later check, without a stale cache.
    assert first.take_fault(TARGET) == {"mode": "503", "delay_ms": 0}
    assert first.take_fault(TARGET) is None
