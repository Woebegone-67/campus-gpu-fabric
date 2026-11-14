"""Small, discriminating histories for resource locality; no performance claims.

The unchanged full inventory path is a differential reference. Independent claim
folding checks physical conservation, and one case observes an actual CPU child.
"""
import copy
import importlib.util
import time
from functools import lru_cache
from itertools import count
from pathlib import Path

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.executors.cpu_reference import CpuReferenceConfig, CpuReferenceExecutor
from campus_fabric.executors.driver import CpuDriver
from campus_fabric.storage.journal import encode
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator

ALICE = m.AuthenticatedActor(actor_id="alice", role="member", owner_id="alice")
BOB = m.AuthenticatedActor(actor_id="bob", role="member", owner_id="bob")
SUPPLIER = m.AuthenticatedActor(actor_id="supplier", role="member", owner_id="supplier")
TEST = m.AuthenticatedActor(actor_id="test", role="test_driver")
REQUESTS = count()
SCALARS = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")


@lru_cache(maxsize=1)
def frozen_baseline():
    """Load the fixed pre-change fixture; shallow clones and source archives work."""
    path = Path(__file__).with_name("_resource_locality_oracle.py")
    spec = importlib.util.spec_from_file_location("resource_locality_frozen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frozen_inventory(state):
    return frozen_baseline().inventory_for_state(state)


def node(name="a", parent=None, now=0.0):
    parent = parent or f"gpu-{name}"
    capacity = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                                ephemeral_storage_mib=1024, gpu_count=1, gpu_memory_mib=16384)
    devices = [m.GPUDevice(device_id=parent, physical_gpu_id=parent, model="A100",
                           memory_mib=16384, state="available")]
    devices.extend(m.GPUDevice(device_id=f"{parent}-s{index}", physical_gpu_id=parent,
                               model="A100", profile="half", memory_mib=8192, state="available")
                   for index in range(2))
    return m.NodeInventory(node_id=name, contributor_owner_id="supplier", state="Ready",
                           capacity=capacity, available=capacity, devices=tuple(devices),
                           labels=(m.KeyValue(key="node", value=name),), observed_at=now)


def command(kernel, params, *, actor=ALICE, accept=True):
    receipt = kernel.commit(m.CommandEnvelope(request_id=f"locality-{next(REQUESTS)}",
                                              actor=actor, operation=params.kind, parameters=params))
    if accept:
        assert receipt.outcome != "rejected", receipt
    return receipt


def allocation(kernel, *, selected=None, profile=None, actor=ALICE):
    params = m.AllocationCreateParameters(
        kind="allocation.create", owner_id=actor.owner_id, ttl_seconds=300,
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                  ephemeral_storage_mib=32,
                                  gpu=m.GPURequest(count=1, model="A100", profile=profile)),
        node_selector=() if selected is None else (m.KeyValue(key="node", value=selected),),
    )
    receipt = command(kernel, params, actor=actor)
    return next(obj.entity_id for obj in receipt.objects if obj.kind == "allocation")


def independent_claims(kernel):
    """Scalar and physical-parent conservation, independent of placement helpers."""
    state = kernel.store.read()
    usage, occupied = {}, {}
    for record in state["allocations"].values():
        if record["state"] not in {"Allocated", "Running", "PreemptRequested"}:
            continue
        for part in (record.get("placement") or {}).get("slices", []):
            assert part["node_id"] in state["nodes"]
            totals = usage.setdefault(part["node_id"], dict.fromkeys(SCALARS, 0))
            for dimension in SCALARS:
                totals[dimension] += part["resources"][dimension]
            devices = {item["device_id"]: item for item in state["nodes"][part["node_id"]]["devices"]}
            for ident in part["device_ids"]:
                assert ident in devices
                assert ident not in occupied
                occupied[ident] = devices[ident]
    for ident, totals in usage.items():
        for dimension, amount in totals.items():
            assert amount <= state["nodes"][ident]["capacity"][dimension]
    physical = {}
    for device in occupied.values():
        physical.setdefault(device["physical_gpu_id"], []).append(device)
    all_devices = {item["device_id"]: item for n in state["nodes"].values() for item in n["devices"]}
    for parent, claims in physical.items():
        assert not (len(claims) > 1 and any(item["profile"] is None for item in claims))
        assert sum(item["memory_mib"] for item in claims) <= all_devices[parent]["memory_mib"]
    # The complete projection remains observable after each accepted history step.
    assert kernel.decision_snapshot().inventory == frozen_inventory(state)
    return state


@pytest.mark.parametrize("corrupt_grant", [False, True])
def test_renew_tail_scheduling_checks_new_grant_and_rolls_back_whole_command(
    tmp_path, monkeypatch, corrupt_grant,
):
    kernel = Kernel(tmp_path / "renew.sqlite", inventory=(node("a"), node("b")))
    active = allocation(kernel, selected="a")
    # A valid pending request can exist while resources are free. Do not infer
    # the resource write set solely from the renewal's own target allocation.
    with monkeypatch.context() as setup:
        setup.setattr(kernel, "_schedule", lambda state, envelope: None)
        pending = allocation(kernel, selected="b")
    before = kernel.store.read()
    before_journal = kernel.store.export_journal()
    original_grant = kernel._grant

    def grant(state, envelope, record, placement, decision=None):
        original_grant(state, envelope, record, placement, decision)
        if corrupt_grant and record["allocation_id"] == pending:
            record["placement"]["slices"][0]["resources"]["cpu_millicores"] = 5000

    monkeypatch.setattr(kernel, "_grant", grant)
    params = m.AllocationRenewParameters(kind="allocation.renew", allocation_id=active, ttl_seconds=600)
    if corrupt_grant:
        with pytest.raises(ValueError):
            command(kernel, params)
        assert kernel.store.read() == before
        assert kernel.store.export_journal() == before_journal
    else:
        receipt = command(kernel, params)
        after = independent_claims(kernel)
        assert after["allocations"][active]["expires_at"] == 600
        assert after["allocations"][pending]["state"] == "Allocated"
        kinds = [event["event_type"] for event in after["events"] if event["event_id"] in receipt.event_ids]
        assert kinds == ["allocation.renewed", "allocation.granted"]


def test_cancel_settlement_releases_then_grants_pending_in_same_commit(tmp_path):
    kernel = Kernel(tmp_path / "cancel.sqlite", inventory=(node(),))
    registration = m.WorkIntentRegistration(
        intent_id="cancel-work", owner_id="alice", execution_mode="logical_simulation",
        settlement_action="release_on_terminal", specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id="alice", ttl_seconds=300,
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024, gpu=m.GPURequest(count=1))),
            job=m.JobSpecification(image="declared-only", command=("declared-only",)),
        ),
    )
    started = WorkflowOrchestrator(kernel, kernel).start(
        registration, kernel.decision_snapshot().version, actor=ALICE, request_id="start-cancel")
    assert started.state == "running"
    pending = allocation(kernel, actor=BOB)
    intent = kernel.store.read()["intents"]["cancel-work"]
    original = intent["allocation_ids"][0]
    command(kernel, m.JobCancelParameters(kind="job.cancel", job_id=intent["job_ids"][0]))
    state = independent_claims(kernel)
    assert state["intents"]["cancel-work"]["state"] == "cancelled"
    assert state["allocations"][original]["state"] == "Released"
    assert state["allocations"][original]["placement"] is None
    assert state["allocations"][pending]["state"] == "Allocated"
    assert kernel.decision_snapshot().inventory[0].available.gpu_count == 0


@pytest.mark.parametrize("operation,valid", [
    ("move", True), ("delete", True), ("release", True),
    ("nested_cpu", False), ("duplicate_slice", False), ("whole_slice", False),
    ("node_capacity", False),
])
def test_lazy_mutations_agree_with_full_inventory_without_preloading_oracle(
    tmp_path, operation, valid,
):
    kernel = Kernel(tmp_path / "differential.sqlite", inventory=(node("a"), node("b")))
    first = allocation(kernel, selected="a", profile="half")
    second = allocation(kernel, selected="a", profile="half")
    before = kernel.store.read()
    before_journal = kernel.store.export_journal()

    def mutate(state):
        if operation == "delete":
            del state["allocations"][second]
        elif operation == "release":
            state["allocations"][second].update(state="Released", placement=None)
        elif operation == "node_capacity":
            state["nodes"]["a"]["capacity"]["cpu_millicores"] = 500
            state["nodes"]["a"]["available"]["cpu_millicores"] = 500
        else:
            part = state["allocations"][second]["placement"]["slices"][0]
            if operation == "move":
                part["node_id"] = "b"
                part["device_ids"][:] = ["gpu-b-s0"]
            elif operation == "nested_cpu":
                part["resources"]["cpu_millicores"] = 4000
            elif operation == "duplicate_slice":
                part["device_ids"][:] = state["allocations"][first]["placement"]["slices"][0]["device_ids"]
            else:
                part["device_ids"][:] = ["gpu-a"]
                part["resources"]["gpu_memory_mib"] = 16384

    expected = copy.deepcopy(before)
    mutate(expected)
    if valid:
        expected_inventory = frozen_inventory(expected)
        with kernel.store.transaction() as conn:
            lazy = kernel.store.load(conn)
            mutate(lazy)
            kernel._validate_resources(lazy)
            kernel.store.save(conn, lazy)
        assert encode(kernel.store.read()) == encode(expected)
        assert kernel.decision_snapshot().inventory == expected_inventory
        independent_claims(kernel)
    else:
        with pytest.raises(ValueError):
            frozen_inventory(expected)
        with pytest.raises(ValueError):
            with kernel.store.transaction() as conn:
                lazy = kernel.store.load(conn)
                mutate(lazy)
                kernel._validate_resources(lazy)
                kernel.store.save(conn, lazy)
        assert kernel.store.read() == before
        assert kernel.store.export_journal() == before_journal


def test_global_node_change_and_owner_rejection_preserve_full_state(tmp_path):
    kernel = Kernel(tmp_path / "node.sqlite", inventory=(node("a"), node("b")))
    first = allocation(kernel, selected="a")
    pending = allocation(kernel, selected="a", actor=BOB)
    command(kernel, m.NodeStateParameters(kind="test.node_state", node_id="a", state="NotReady"), actor=TEST)
    state = independent_claims(kernel)
    assert state["allocations"][first]["state"] == "Evicted"
    assert state["allocations"][pending]["state"] == "Pending"
    assert kernel.decision_snapshot().inventory[0].available.cpu_millicores == 0
    command(kernel, m.NodeStateParameters(kind="test.node_state", node_id="a", state="Ready"), actor=TEST)
    state = independent_claims(kernel)
    assert state["allocations"][pending]["state"] == "Allocated"
    before, journal = kernel.store.read(), kernel.store.export_journal()
    denied = command(kernel, m.AllocationTargetParameters(kind="allocation.release", allocation_id=pending),
                     actor=ALICE, accept=False)
    assert denied.outcome == "rejected" and denied.error.code == "not_found"
    assert kernel.store.read() == before
    assert kernel.store.export_journal() == journal


def test_lazy_first_fit_matches_frozen_whole_and_mig_transcript(tmp_path):
    kernel = Kernel(tmp_path / "first-fit.sqlite", inventory=(node("z"), node("a"), node("m")))
    frozen_placement = frozen_baseline()
    identities = []
    for profile in (None, "half", "half", None, "half"):
        request = m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                    ephemeral_storage_mib=32,
                                    gpu=m.GPURequest(count=1, model="A100", profile=profile))
        expected = frozen_placement.choose_placement(request, (), frozen_inventory(kernel.store.read()))
        ident = allocation(kernel, profile=profile)
        identities.append(ident)
        state = independent_claims(kernel)
        assert state["allocations"][ident]["placement"] == (
            None if expected is None else expected.model_dump(mode="json"))
    assert kernel.store.read()["allocations"][identities[-1]]["state"] == "Pending"
    expected_state = kernel.store.read()
    expected_state["allocations"][identities[0]].update(state="Released", placement=None)
    pending = expected_state["allocations"][identities[-1]]
    expected = frozen_placement.choose_placement(
        m.ResourceRequest.model_validate(pending["request"]), (), frozen_inventory(expected_state))
    command(kernel, m.AllocationTargetParameters(kind="allocation.release", allocation_id=identities[0]))
    state = independent_claims(kernel)
    assert state["allocations"][identities[-1]]["placement"] == expected.model_dump(mode="json")


def drive_until(driver, predicate, timeout=7):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        driver.tick(advance_wall=False)
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("Real CPU lifecycle did not settle within the bounded wait")


def test_real_cpu_stop_fence_survives_reset_and_parent_reregistration(tmp_path):
    kernel = Kernel(tmp_path / "cpu.sqlite", inventory=(node(),), execution_mode="cpu_reference")
    executor = CpuReferenceExecutor(tmp_path / "cpu", CpuReferenceConfig(
        total_steps=10_000_000, batch_steps=1000, batch_pause_seconds=0.01))
    driver = CpuDriver(kernel, executor, lock_path=tmp_path / "driver.lock")
    driver.start()
    try:
        aid = allocation(kernel)
        receipt = command(kernel, m.JobCreateParameters(kind="job.create", allocation_id=aid,
            image="declared-only", command=("declared-only",), timeout_seconds=250))
        jid = next(obj.entity_id for obj in receipt.objects if obj.kind == "job")
        drive_until(driver, lambda: any(record["status"] == "running" for record in executor.inspect()["attempts"]))
        waiting = allocation(kernel, actor=BOB)
        command(kernel, m.AllocationTargetParameters(kind="allocation.release", allocation_id=aid))
        # No driver tick occurs between request and observation: time alone is no stop proof.
        state = independent_claims(kernel)
        assert state["jobs"][jid]["physical_live"]
        assert state["allocations"][aid]["placement"] is not None
        assert state["allocations"][aid]["pending_terminal"] == "Released"
        assert state["allocations"][waiting]["state"] == "Pending"
        assert not executor.inspect()["attempts"][0]["stop_confirmed"]
        command(kernel, m.ResetParameters(kind="test.reset"), actor=TEST)
        retired = kernel.store.read()["retired"]
        assert retired and next(iter(retired.values()))["physical_gpu_ids"] == ["gpu-a"]
        command(kernel, m.ContributionRegisterParameters(kind="contribution.register",
            node=node("renamed", parent="gpu-a", now=kernel.runtime_metadata()["now"])), actor=SUPPLIER)
        new_request = allocation(kernel, selected="renamed", actor=BOB)
        state = independent_claims(kernel)
        assert state["allocations"][new_request]["state"] == "Pending"
        assert kernel.decision_snapshot().inventory[0].available.cpu_millicores == 0
        drive_until(driver, lambda: not kernel.store.read()["retired"])
        state = independent_claims(kernel)
        assert state["allocations"][new_request]["state"] == "Allocated"
        assert all(record["stop_confirmed"] for record in executor.inspect()["attempts"])
    finally:
        for record in list(kernel.store.read()["allocations"].values()):
            command(kernel, m.AllocationTargetParameters(kind="allocation.release",
                allocation_id=record["allocation_id"]), actor=ALICE if record["owner_id"] == "alice" else BOB)
        drive_until(driver, lambda: not kernel.store.read().get("retired") and not any(
            job.get("physical_live") for job in kernel.store.read()["jobs"].values()))
        driver.close()
