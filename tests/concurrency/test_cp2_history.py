"""CP2 races across independent SQLite connections, with an event-ledger oracle."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel

ALICE = m.AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice")
ADMIN = m.AuthenticatedActor(actor_id="audit", role="administrator")
TEST = m.AuthenticatedActor(actor_id="test", role="test_driver")


def node(index):
    cap = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                           ephemeral_storage_mib=1024, gpu_count=1, gpu_memory_mib=8192)
    return m.NodeInventory(
        node_id=f"n{index}", contributor_owner_id="supplier", state="Ready",
        capacity=cap, available=cap, observed_at=0.0,
        devices=(m.GPUDevice(device_id=f"g{index}", physical_gpu_id=f"g{index}",
                             model="A10", memory_mib=8192, state="available"),),
    )


def command(kernel, parameters, request_id, *, actor=ALICE, key=None):
    return kernel.commit(m.CommandEnvelope(
        request_id=request_id, actor=actor, operation=parameters.kind,
        parameters=parameters, idempotency_key=key,
    ))


def accepted(receipt, kind=None):
    assert receipt.outcome != "rejected", receipt
    if kind:
        return next(item.entity_id for item in receipt.objects if item.kind == kind)
    return receipt


def setup(path):
    kernel = Kernel(path, inventory=(node(1), node(2)), simulation_duration_seconds=100.0)
    aid = accepted(command(kernel, m.AllocationCreateParameters(
        kind="allocation.create", owner_id="alice", max_restarts=1,
        preemption_mode="restartable",
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                  ephemeral_storage_mib=32,
                                  gpu=m.GPURequest(count=1, model="A10")),
    ), "allocate"), "allocation")
    jid = accepted(command(kernel, m.JobCreateParameters(
        kind="job.create", allocation_id=aid, image="model-only", command=("work",),
        timeout_seconds=250,
    ), "job"), "job")
    accepted(command(kernel, m.RecallParameters(
        kind="test.contributor_demand", device_ids=("g1",), notice_seconds=5,
        duration_seconds=20,
    ), "recall", actor=TEST))
    return kernel, aid, jid


def parallel(kernels, fn):
    barrier = Barrier(len(kernels))

    def invoke(index):
        barrier.wait(timeout=30)
        return fn(kernels[index], index)

    with ThreadPoolExecutor(max_workers=len(kernels)) as pool:
        return list(pool.map(invoke, range(len(kernels))))


def audit(kernel):
    snap = kernel.read_snapshot(m.ReadQuery(actor=ADMIN, global_scope=True))
    # Reconstruct all prefix claims from recorded deltas, rather than free counters.
    held, devices = {}, {}
    for event in snap.events:
        for delta in event.resource_deltas:
            quantities = held.setdefault(delta.node_id, [0, 0, 0])
            for index, field in enumerate(("cpu_millicores", "memory_mib",
                                           "ephemeral_storage_mib")):
                quantities[index] += getattr(delta, field)
                assert 0 <= quantities[index] <= (4000, 8192, 1024)[index]
            for device in delta.device_ids_added:
                assert device not in devices, "One physical GPU acquired twice"
                devices[device] = delta.allocation_id
            for device in delta.device_ids_removed:
                assert devices.pop(device, None) == delta.allocation_id, "Duplicate/wrong return"
    projected = {}
    for allocation in snap.allocations:
        if allocation.state in {"Allocated", "Running", "PreemptRequested"}:
            assert allocation.placement is not None
            for part in allocation.placement.slices:
                for device in part.device_ids:
                    assert device not in projected
                    projected[device] = allocation.allocation_id
    assert devices == projected
    return snap


def test_32_resume_requests_commit_only_one_replacement_generation(tmp_path):
    path = tmp_path / "resume-race.sqlite3"
    kernel, aid, jid = setup(path)
    accepted(command(kernel, m.ClockAdvanceParameters(
        kind="clock.advance", advance_seconds=5.0, source="test_control",
    ), "cutoff", actor=TEST))
    kernels = [Kernel(path, simulation_duration_seconds=100.0) for _ in range(32)]
    results = parallel(kernels, lambda k, i: command(k, m.AllocationTargetParameters(
        kind="allocation.resume", allocation_id=aid,
    ), f"resume-{i}"))
    assert any(result.outcome != "rejected" for result in results)
    snap = audit(kernel)
    target = next(item for item in snap.jobs if item.job_id == jid)
    assert target.state == "Running" and target.attempt.generation == 2
    assert kernel.export_state(ADMIN)["allocations"][aid]["restart_count"] == 1
    generations = {event.attempt.generation for event in snap.events
                   if event.attempt and event.attempt.job_id == jid}
    assert generations == {1, 2}


def test_32_replayed_checkpoint_requests_release_old_claim_once(tmp_path):
    path = tmp_path / "checkpoint-race.sqlite3"
    kernel, aid, _ = setup(path)
    kernels = [Kernel(path, simulation_duration_seconds=100.0) for _ in range(32)]
    results = parallel(kernels, lambda k, i: command(k, m.CheckpointReportParameters(
        kind="allocation.checkpoint", allocation_id=aid, status="ready",
    ), f"ready-{i}", key="single-ready"))
    assert all(result.outcome != "rejected" for result in results)
    assert len({result.event_ids for result in results}) == 1
    snap = audit(kernel)
    assert next(item for item in snap.allocations if item.allocation_id == aid).state == "Preempted"
    returns = [delta for event in snap.events for delta in event.resource_deltas
               if delta.allocation_id == aid and "g1" in delta.device_ids_removed]
    assert len(returns) == 1


def test_resume_release_race_cannot_revive_terminal_allocation(tmp_path):
    path = tmp_path / "resume-release.sqlite3"
    kernel, aid, jid = setup(path)
    accepted(command(kernel, m.ClockAdvanceParameters(
        kind="clock.advance", advance_seconds=5.0, source="test_control",
    ), "cutoff", actor=TEST))
    kernels = [Kernel(path, simulation_duration_seconds=100.0) for _ in range(32)]
    parallel(kernels, lambda k, i: command(k, m.AllocationTargetParameters(
        kind="allocation.resume" if i % 2 else "allocation.release", allocation_id=aid,
    ), f"race-{i}"))
    snap = audit(kernel)
    assert next(item for item in snap.allocations if item.allocation_id == aid).state == "Released"
    assert next(item for item in snap.jobs if item.job_id == jid).state == "Cancelled"
    assert kernel.export_state(ADMIN)["allocations"][aid]["restart_count"] <= 1
