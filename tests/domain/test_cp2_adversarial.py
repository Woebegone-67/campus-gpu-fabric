"""CP2 adversarial command histories, specified from SEMANTICS, not timer internals.

These tests use public typed commands and authorized projections. Logical-model
success never establishes physical CPU stop/checkpoint evidence.
"""

from itertools import count

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel

ALICE = m.AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice")
BOB = m.AuthenticatedActor(actor_id="bob", role="researcher", owner_id="bob")
ADMIN = m.AuthenticatedActor(actor_id="admin", role="administrator")
TEST = m.AuthenticatedActor(actor_id="test", role="test_driver")
SYSTEM = m.AuthenticatedActor(actor_id="system", role="system")
REQUESTS = count()


def node(number=1):
    capacity = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                                ephemeral_storage_mib=1024, gpu_count=1,
                                gpu_memory_mib=8192)
    return m.NodeInventory(
        node_id=f"n{number}", contributor_owner_id=f"supplier{number}", state="Ready",
        capacity=capacity, available=capacity, observed_at=0.0,
        devices=(m.GPUDevice(device_id=f"g{number}", physical_gpu_id=f"g{number}",
                             model="A10", memory_mib=8192, state="available"),),
    )


def command(kernel, parameters, *, actor=ALICE, key=None, request_id=None):
    return kernel.commit(m.CommandEnvelope(
        request_id=request_id or f"adversarial-{next(REQUESTS)}", actor=actor,
        operation=parameters.kind, parameters=parameters, idempotency_key=key,
    ))


def accepted(receipt):
    assert receipt.outcome != "rejected", receipt
    return receipt


def entity(receipt, kind):
    accepted(receipt)
    return next(item.entity_id for item in receipt.objects if item.kind == kind)


def snapshot(kernel):
    return kernel.read_snapshot(m.ReadQuery(actor=ADMIN, global_scope=True))


def allocation(kernel, *, owner=ALICE, ttl=300, mode="restartable", restarts=2,
               checkpoint_timeout=15, preemptible=True):
    return entity(command(kernel, m.AllocationCreateParameters(
        kind="allocation.create", owner_id=owner.owner_id, ttl_seconds=ttl,
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                  ephemeral_storage_mib=32,
                                  gpu=m.GPURequest(count=1, model="A10", memory_mib=4096)),
        preemption_mode=mode, max_restarts=restarts,
        checkpoint_timeout_seconds=checkpoint_timeout, preemptible=preemptible,
    ), actor=owner), "allocation")


def job(kernel, allocation_id, *, retries=0, timeout=250, owner=ALICE):
    return entity(command(kernel, m.JobCreateParameters(
        kind="job.create", allocation_id=allocation_id, image="model-only",
        command=("not-an-actual-process",), timeout_seconds=timeout, max_retries=retries,
    ), actor=owner), "job")


def allocation_view(kernel, aid):
    return next(item for item in snapshot(kernel).allocations if item.allocation_id == aid)


def job_view(kernel, jid):
    return next(item for item in snapshot(kernel).jobs if item.job_id == jid)


def advance(kernel, delta):
    return accepted(command(kernel, m.ClockAdvanceParameters(
        kind="clock.advance", advance_seconds=float(delta), source="test_control",
    ), actor=TEST))


def recall(kernel, *, devices=("g1",), notice=5, duration=20, key=None):
    return accepted(command(kernel, m.RecallParameters(
        kind="test.contributor_demand", device_ids=devices,
        notice_seconds=notice, duration_seconds=duration,
    ), actor=TEST, key=key))


def checkpoint(kernel, aid, status="ready", uri=None, key=None):
    return command(kernel, m.CheckpointReportParameters(
        kind="allocation.checkpoint", allocation_id=aid, status=status, artifact_uri=uri,
    ), key=key)


def resume(kernel, aid, *, key=None):
    return command(kernel, m.AllocationTargetParameters(
        kind="allocation.resume", allocation_id=aid,
    ), key=key)


def set_node(kernel, state, number=1):
    return accepted(command(kernel, m.NodeStateParameters(
        kind="test.node_state", node_id=f"n{number}", state=state,
    ), actor=TEST))


def failure(kernel, jid, *, attempt=None, version=None, callback_id=None):
    snap = snapshot(kernel)
    return command(kernel, m.ExecutorReportParameters(
        kind="executor.report", event=m.ExecutorEvent(
            callback_id=callback_id or f"failure-{next(REQUESTS)}",
            version=version or snap.version, attempt=attempt or job_view(kernel, jid).attempt,
            kind="failed", mode="logical_simulation", observed_at=snap.now,
            error=m.ErrorDetail(code="internal_error", detail="Injected execution failure"),
        ),
    ), actor=SYSTEM)


def assert_no_overcommit(kernel):
    """Independent claim summation; does not call production placement helpers."""
    snap = snapshot(kernel)
    amounts = {}
    devices = set()
    for item in snap.allocations:
        if item.state not in {"Allocated", "Running", "PreemptRequested"}:
            assert item.placement is None
            continue
        assert item.placement is not None
        for part in item.placement.slices:
            for device in part.device_ids:
                assert device not in devices
                devices.add(device)
            total = amounts.setdefault(part.node_id, [0, 0, 0])
            for index, field in enumerate(("cpu_millicores", "memory_mib",
                                           "ephemeral_storage_mib")):
                total[index] += getattr(part.resources, field)
    for inventory in kernel.decision_snapshot().inventory:
        for index, field in enumerate(("cpu_millicores", "memory_mib",
                                       "ephemeral_storage_mib")):
            assert amounts.get(inventory.node_id, [0, 0, 0])[index] <= getattr(
                inventory.capacity, field)


def facts(value):
    """Find explicit checkpoint evidence independent of its projection nesting."""
    if isinstance(value, dict):
        if "report_status" in value and "verification_status" in value:
            yield value
        for child in value.values():
            yield from facts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from facts(child)


@pytest.fixture
def kernel(tmp_path):
    return Kernel(tmp_path / "cp2.sqlite3", inventory=(node(), node(2)),
                  simulation_duration_seconds=100.0)


@pytest.mark.parametrize("uri", [None, "file:///does-not-exist/checkpoint.json"])
def test_ready_report_does_not_fabricate_verified_artifact(kernel, uri):
    aid = allocation(kernel, mode="checkpoint")
    jid = job(kernel, aid)
    old_attempt = job_view(kernel, jid).attempt
    advance(kernel, 2)
    recall(kernel)
    accepted(checkpoint(kernel, aid, uri=uri))
    assert allocation_view(kernel, aid).state == "Preempted"
    reports = list(facts(kernel.export_state(ADMIN)))
    assert reports, "Accepted checkpoint reports must retain inspectable evidence"
    assert all(item["verification_status"] != "verified" for item in reports)
    assert all(item["continuation_status"] != "applied" for item in reports)
    accepted(resume(kernel, aid))
    current = job_view(kernel, jid)
    assert current.job_id == jid and current.attempt.generation == old_attempt.generation + 1
    assert current.attempt.attempt_id != old_attempt.attempt_id
    assert allocation_view(kernel, aid).expires_at == 300
    assert_no_overcommit(kernel)


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_strict_checkpoint_failure_or_timeout_evicts_without_success(kernel, status):
    aid = allocation(kernel, mode="checkpoint", checkpoint_timeout=2)
    jid = job(kernel, aid)
    recall(kernel, notice=5)
    if status == "failed":
        accepted(checkpoint(kernel, aid, "failed"))
    else:
        advance(kernel, 2)
    assert allocation_view(kernel, aid).state == "Evicted"
    assert job_view(kernel, jid).state == "Failed"
    denied = resume(kernel, aid)
    assert denied.outcome == "rejected"
    assert all(item["verification_status"] != "verified"
               for item in facts(kernel.export_state(ADMIN)))
    assert_no_overcommit(kernel)


@pytest.mark.parametrize("checkpoint_timeout,notice", [(2, 5), (10, 5)])
def test_ready_exactly_at_effective_cutoff_is_late(kernel, checkpoint_timeout, notice):
    aid = allocation(kernel, mode="checkpoint", checkpoint_timeout=checkpoint_timeout)
    jid = job(kernel, aid)
    recall(kernel, notice=notice)
    advance(kernel, min(checkpoint_timeout, notice))
    before = snapshot(kernel)
    denied = checkpoint(kernel, aid)
    assert denied.outcome == "rejected"
    assert denied.error.code in {"stage_conflict", "lease_unavailable"}
    after = snapshot(kernel)
    assert after.allocations == before.allocations and after.jobs == before.jobs
    assert job_view(kernel, jid).state == "Failed"


def test_expiry_beats_recall_and_completion_at_same_instant(tmp_path):
    current = Kernel(tmp_path / "tie.sqlite3", inventory=(node(),),
                     simulation_duration_seconds=30.0)
    aid = allocation(current, ttl=30, checkpoint_timeout=30)
    jid = job(current, aid, timeout=30)
    advance(current, 25)
    recall(current, notice=5)
    advance(current, 5)
    assert allocation_view(current, aid).state == "Expired"
    assert job_view(current, jid).state == "Failed"
    assert checkpoint(current, aid).outcome == "rejected"
    assert resume(current, aid).outcome == "rejected"
    assert_no_overcommit(current)


def test_notice_immediately_blocks_new_job_and_new_placement(tmp_path):
    current = Kernel(tmp_path / "notice.sqlite3", inventory=(node(),))
    aid = allocation(current)
    recall(current)
    denied = command(current, m.JobCreateParameters(
        kind="job.create", allocation_id=aid, image="model", command=("work",),
    ))
    assert denied.outcome == "rejected"
    assert denied.error.code in {"stage_conflict", "lease_unavailable"}
    command(current, m.AllocationTargetParameters(kind="allocation.release", allocation_id=aid))
    second = allocation(current, owner=BOB)
    assert allocation_view(current, second).state == "Pending"
    advance(current, 24)
    assert allocation_view(current, second).state == "Pending"
    advance(current, 1)
    assert allocation_view(current, second).state == "Allocated"
    assert allocation_view(current, second).expires_at == 325


def test_replayed_recall_and_ready_do_not_double_return_resources(kernel):
    aid = allocation(kernel)
    job(kernel, aid)
    first = recall(kernel, key="recall-once")
    repeated = recall(kernel, key="recall-once")
    assert repeated.event_ids == first.event_ids
    ready = accepted(checkpoint(kernel, aid, key="ready-once"))
    replay = accepted(checkpoint(kernel, aid, key="ready-once"))
    assert replay.event_ids == ready.event_ids
    assert allocation_view(kernel, aid).state == "Preempted"
    assert_no_overcommit(kernel)


def test_overlapping_recalls_do_not_reopen_capacity_at_first_end(tmp_path):
    current = Kernel(tmp_path / "overlap.sqlite3", inventory=(node(),),
                     simulation_duration_seconds=100.0)
    aid = allocation(current)
    job(current, aid)
    recall(current, notice=5, duration=10)
    advance(current, 2)
    recall(current, notice=5, duration=20)
    advance(current, 3)
    assert allocation_view(current, aid).state == "Preempted"
    waiting = allocation(current, owner=BOB)
    advance(current, 10)
    assert allocation_view(current, waiting).state == "Pending"
    advance(current, 11)
    assert allocation_view(current, waiting).state == "Pending"
    advance(current, 1)
    assert allocation_view(current, waiting).state == "Allocated"
    assert allocation_view(current, waiting).expires_at == 327
    assert_no_overcommit(current)


def test_unknown_gpu_recall_is_atomic_and_contributor_cannot_recall_another_owner(kernel):
    aid = allocation(kernel)
    before = snapshot(kernel)
    bad = command(kernel, m.RecallParameters(
        kind="test.contributor_demand", device_ids=("g1", "unknown"),
        notice_seconds=5, duration_seconds=20,
    ), actor=TEST)
    assert bad.outcome == "rejected"
    assert snapshot(kernel).allocations == before.allocations
    assert not kernel.decision_snapshot().notices
    intruder = m.AuthenticatedActor(actor_id="supplier2", role="contributor",
                                   owner_id="supplier2")
    bad = command(kernel, m.RecallParameters(
        kind="contribution.recall", device_ids=("g1",), notice_seconds=5,
        duration_seconds=20,
    ), actor=intruder)
    assert bad.outcome == "rejected"
    assert allocation_view(kernel, aid).state == "Allocated"
    assert not kernel.decision_snapshot().notices


@pytest.mark.parametrize("mode,preemptible", [("non_preemptible", True),
                                           ("restartable", False)])
def test_contributor_ownership_recall_overrides_scheduler_protection(kernel, mode, preemptible):
    aid = allocation(kernel, mode=mode, preemptible=preemptible)
    job(kernel, aid)
    recall(kernel)
    assert allocation_view(kernel, aid).state == "PreemptRequested"
    advance(kernel, 5)
    assert allocation_view(kernel, aid).state == "Preempted"


def test_waiting_resume_keeps_deadline_and_does_not_consume_restart_budget(tmp_path):
    current = Kernel(tmp_path / "pending.sqlite3", inventory=(node(),),
                     simulation_duration_seconds=100.0)
    aid = allocation(current, ttl=300, restarts=1)
    jid = job(current, aid)
    first_attempt = job_view(current, jid).attempt
    recall(current)
    advance(current, 5)
    for _ in range(4):
        accepted(resume(current, aid))
        assert allocation_view(current, aid).state == "Pending"
        assert job_view(current, jid).attempt == first_attempt
        assert allocation_view(current, aid).expires_at == 300
        assert current.export_state(ADMIN)["allocations"][aid]["restart_count"] == 0
    advance(current, 20)
    assert allocation_view(current, aid).state == "Running"
    assert job_view(current, jid).attempt.generation == first_attempt.generation + 1
    assert allocation_view(current, aid).expires_at == 300
    assert current.export_state(ADMIN)["allocations"][aid]["restart_count"] == 1
    recall(current)
    advance(current, 5)
    denied = resume(current, aid)
    assert denied.outcome == "rejected" and denied.error.code == "budget_exhausted"
    assert_no_overcommit(current)


def test_waiting_resume_expires_on_original_deadline(tmp_path):
    current = Kernel(tmp_path / "pending-expiry.sqlite3", inventory=(node(),),
                     simulation_duration_seconds=100.0)
    aid = allocation(current, ttl=30)
    jid = job(current, aid, timeout=100)
    recall(current, duration=100)
    advance(current, 5)
    accepted(resume(current, aid))
    advance(current, 25)
    assert allocation_view(current, aid).state == "Expired"
    assert job_view(current, jid).state == "Failed"
    advance(current, 100)
    assert allocation_view(current, aid).state == "Expired"
    assert job_view(current, jid).attempt.generation == 1


def test_timeout_includes_suspended_wait_and_cannot_be_reset_by_resume(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid, timeout=10)
    recall(kernel)
    advance(kernel, 5)
    advance(kernel, 4)
    accepted(resume(kernel, aid))
    assert job_view(kernel, jid).state == "Running"
    advance(kernel, 1)
    assert job_view(kernel, jid).state == "Failed"
    assert allocation_view(kernel, aid).state == "Allocated"


def test_retry_and_restart_charge_separate_budgets(kernel):
    aid = allocation(kernel, restarts=1)
    jid = job(kernel, aid, retries=1)
    accepted(failure(kernel, jid))
    advance(kernel, 1)
    retried = job_view(kernel, jid).attempt
    assert retried.generation == 2 and job_view(kernel, jid).state == "Running"
    state = kernel.export_state(ADMIN)
    assert state["jobs"][jid]["retry_count"] == 1
    assert state["allocations"][aid]["restart_count"] == 0
    recall(kernel)
    advance(kernel, 5)
    accepted(resume(kernel, aid))
    resumed = job_view(kernel, jid).attempt
    assert resumed.generation == 3 and resumed.attempt_id != retried.attempt_id
    state = kernel.export_state(ADMIN)
    assert state["jobs"][jid]["retry_count"] == 1
    assert state["allocations"][aid]["restart_count"] == 1
    accepted(failure(kernel, jid))
    advance(kernel, 2)
    assert job_view(kernel, jid).state == "Failed"
    assert job_view(kernel, jid).attempt == resumed
    assert_no_overcommit(kernel)


def test_expiry_beats_due_retry_without_charging_nonexistent_attempt(tmp_path):
    current = Kernel(tmp_path / "retry-expiry.sqlite3", inventory=(node(),),
                     simulation_duration_seconds=100.0)
    aid = allocation(current, ttl=30)
    jid = job(current, aid, retries=1)
    advance(current, 29)
    accepted(failure(current, jid))
    advance(current, 1)
    assert allocation_view(current, aid).state == "Expired"
    assert job_view(current, jid).state == "Failed"
    assert job_view(current, jid).attempt.generation == 1
    assert current.export_state(ADMIN)["jobs"][jid]["retry_count"] == 0


def test_total_job_timeout_beats_retry_and_does_not_restart_timeout(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid, retries=2, timeout=2)
    advance(kernel, 1)
    accepted(failure(kernel, jid))
    advance(kernel, 1)
    assert job_view(kernel, jid).state == "Failed"
    assert job_view(kernel, jid).attempt.generation == 1
    assert kernel.export_state(ADMIN)["jobs"][jid]["retry_count"] == 0
    assert allocation_view(kernel, aid).state == "Allocated"


def test_replayed_failure_callback_does_not_queue_second_retry(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid, retries=1)
    original = job_view(kernel, jid).attempt
    version = snapshot(kernel).version
    first = accepted(failure(kernel, jid, attempt=original, version=version,
                             callback_id="same-failure"))
    before = snapshot(kernel)
    second = failure(kernel, jid, attempt=original, version=version,
                     callback_id="same-failure")
    assert second.outcome in {"rejected", "noop"} or second.idempotency_replayed
    assert snapshot(kernel).jobs == before.jobs
    advance(kernel, 1)
    assert job_view(kernel, jid).attempt.generation == 2
    assert kernel.export_state(ADMIN)["jobs"][jid]["retry_count"] == 1
    assert first.event_ids


def test_distinct_late_failure_cannot_postpone_already_queued_retry(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid, retries=1)
    original = job_view(kernel, jid).attempt
    accepted(failure(kernel, jid, attempt=original))
    advance(kernel, 0.5)
    late = failure(kernel, jid, attempt=original)
    assert late.outcome in {"rejected", "noop"}
    advance(kernel, 0.5)
    assert job_view(kernel, jid).state == "Running"
    assert job_view(kernel, jid).attempt.generation == 2


def test_callback_after_confirmed_interruption_does_not_fail_suspended_work(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid)
    original = job_view(kernel, jid).attempt
    recall(kernel)
    advance(kernel, 5)
    assert job_view(kernel, jid).state == "Preempted"
    before = snapshot(kernel)
    late = failure(kernel, jid, attempt=original)
    assert late.outcome in {"rejected", "noop"}
    assert snapshot(kernel).jobs == before.jobs
    accepted(resume(kernel, aid))
    assert job_view(kernel, jid).state == "Running"


def test_empty_recalled_lease_can_resume_with_zero_execution_restart_budget(kernel):
    aid = allocation(kernel, restarts=0)
    recall(kernel)
    advance(kernel, 5)
    accepted(resume(kernel, aid))
    assert allocation_view(kernel, aid).state == "Allocated"
    assert kernel.export_state(ADMIN)["allocations"][aid]["restart_count"] == 0
    jid = job(kernel, aid)
    assert job_view(kernel, jid).attempt.generation == 1


def test_whole_gpu_recall_interrupts_slice_and_blocks_both_views(tmp_path):
    raw = node()
    sliced = raw.model_copy(update={"devices": raw.devices + (
        m.GPUDevice(device_id="g1s", physical_gpu_id="g1", model="A10",
                    memory_mib=4096, profile="half", state="available"),
    )})
    current = Kernel(tmp_path / "parent-recall.sqlite3", inventory=(sliced,),
                     simulation_duration_seconds=100.0)
    parameters = m.AllocationCreateParameters(
        kind="allocation.create", owner_id="alice", preemption_mode="restartable",
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                  gpu=m.GPURequest(count=1, model="A10", memory_mib=4096,
                                                   profile="half")),
    )
    aid = entity(command(current, parameters), "allocation")
    job(current, aid)
    recall(current)
    assert allocation_view(current, aid).state == "PreemptRequested"
    advance(current, 5)
    accepted(resume(current, aid))
    assert allocation_view(current, aid).state == "Pending"
    devices = current.decision_snapshot().inventory[0].devices
    assert {device.device_id for device in devices} == {"g1", "g1s"}
    assert all(device.state == "unavailable" for device in devices)
    advance(current, 20)
    assert allocation_view(current, aid).state == "Running"
    assert_no_overcommit(current)


def test_stale_attempt_cannot_complete_or_fail_replacement(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid)
    old = job_view(kernel, jid).attempt
    recall(kernel)
    advance(kernel, 5)
    accepted(resume(kernel, aid))
    before = snapshot(kernel)
    denied = failure(kernel, jid, attempt=old)
    assert denied.outcome == "rejected" and denied.error.code == "attempt_stale"
    after = snapshot(kernel)
    assert after.allocations == before.allocations and after.jobs == before.jobs
    assert after.events == before.events


def test_reset_fences_old_epoch_callback_even_after_new_resources_and_job(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid)
    old = job_view(kernel, jid).attempt
    version = snapshot(kernel).version
    accepted(command(kernel, m.ResetParameters(kind="test.reset"), actor=TEST))
    supplier = m.AuthenticatedActor(actor_id="supplier1", role="contributor",
                                   owner_id="supplier1")
    accepted(command(kernel, m.ContributionRegisterParameters(
        kind="contribution.register", node=node(),
    ), actor=supplier))
    new_aid = allocation(kernel)
    new_jid = job(kernel, new_aid)
    before = snapshot(kernel)
    denied = failure(kernel, jid, attempt=old, version=version)
    assert denied.outcome == "rejected" and denied.error.code == "attempt_stale"
    after = snapshot(kernel)
    assert after.allocations == before.allocations and after.jobs == before.jobs
    assert job_view(kernel, new_jid).state == "Running"
    assert_no_overcommit(kernel)


def test_draining_preserves_existing_execution_but_blocks_new_placement(tmp_path):
    current = Kernel(tmp_path / "draining.sqlite3", inventory=(node(),))
    aid = allocation(current)
    jid = job(current, aid)
    set_node(current, "Draining")
    advance(current, 10)
    assert job_view(current, jid).state == "Succeeded"
    accepted(command(current, m.AllocationTargetParameters(
        kind="allocation.release", allocation_id=aid,
    )))
    second = allocation(current, owner=BOB)
    assert allocation_view(current, second).state == "Pending"
    set_node(current, "Ready")
    assert allocation_view(current, second).state == "Allocated"


def test_notready_then_ready_never_resurrects_evicted_execution(kernel):
    aid = allocation(kernel)
    jid = job(kernel, aid)
    old = job_view(kernel, jid).attempt
    set_node(kernel, "NotReady")
    assert allocation_view(kernel, aid).state == "Evicted"
    assert job_view(kernel, jid).state == "Failed"
    set_node(kernel, "Ready")
    assert allocation_view(kernel, aid).state == "Evicted"
    assert job_view(kernel, jid).attempt == old
    assert resume(kernel, aid).outcome == "rejected"
    assert failure(kernel, jid, attempt=old).outcome == "rejected"
    assert_no_overcommit(kernel)


def test_ready_node_does_not_cancel_unfinished_contributor_recall(tmp_path):
    current = Kernel(tmp_path / "ready-recall.sqlite3", inventory=(node(),))
    recall(current)
    set_node(current, "Draining")
    set_node(current, "Ready")
    aid = allocation(current)
    assert allocation_view(current, aid).state == "Pending"
    advance(current, 25)
    assert allocation_view(current, aid).state == "Allocated"


def normalized_state(kernel):
    snap = snapshot(kernel)
    # Revision and clock-event request IDs legitimately differ between advances.
    return {
        "now": snap.now,
        "allocations": [item.model_dump() for item in snap.allocations],
        "jobs": [item.model_dump() for item in snap.jobs],
        "inventory": [item.model_dump() for item in kernel.decision_snapshot().inventory],
        "notices": [item.model_dump() for item in kernel.decision_snapshot().notices],
        "lifecycle_history": [
            (event.logical_time, event.event_type, event.owner_id,
             [change.model_dump() for change in event.state_changes],
             [delta.model_dump() for delta in event.resource_deltas],
             event.attempt.model_dump() if event.attempt else None)
            for event in snap.events if event.event_type != "clock.advanced"
        ],
    }


def test_large_advance_equals_every_boundary_with_recall_retry_and_expiry(tmp_path):
    kernels = [Kernel(tmp_path / f"clock-{i}.sqlite3", inventory=(node(),),
                      simulation_duration_seconds=40.0) for i in range(2)]
    for current in kernels:
        aid = allocation(current, ttl=60)
        jid = job(current, aid, retries=1, timeout=55)
        accepted(failure(current, jid))
        advance(current, 1)
        recall(current, notice=4, duration=10)
        advance(current, 4)
        accepted(resume(current, aid))
        allocation(current, owner=BOB, ttl=30)
    advance(kernels[0], 100)
    for step in (9, 1, 24, 1, 15, 5, 10, 35):
        advance(kernels[1], step)
    assert normalized_state(kernels[0]) == normalized_state(kernels[1])
    assert_no_overcommit(kernels[0])


def test_restart_from_disk_preserves_pending_resume_and_its_deadline(tmp_path):
    path = tmp_path / "durable.sqlite3"
    current = Kernel(path, inventory=(node(),), simulation_duration_seconds=100.0)
    aid = allocation(current, ttl=60, restarts=1)
    jid = job(current, aid)
    recall(current)
    advance(current, 5)
    accepted(resume(current, aid))
    restored = Kernel(path, simulation_duration_seconds=100.0)
    assert normalized_state(restored) == normalized_state(current)
    advance(restored, 20)
    assert allocation_view(restored, aid).expires_at == 60
    assert job_view(restored, jid).attempt.generation == 2
    advance(restored, 35)
    assert allocation_view(restored, aid).state == "Expired"
    assert_no_overcommit(restored)


def test_work_intent_preserves_arrival_job_identity_and_truthful_attempt_history(kernel):
    specification = m.WorkSpecification(
        allocation=m.AllocationCreateParameters(
            kind="allocation.create", owner_id="alice", preemption_mode="checkpoint",
            request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                      gpu=m.GPURequest(count=1, model="A10")),
        ),
        job=m.JobSpecification(image="model-only", command=("work",),
                               timeout_seconds=250, max_retries=0),
    )
    accepted(kernel.register_intent(m.WorkIntentRegistration(
        intent_id="stable-work", owner_id="alice", execution_mode="logical_simulation",
        settlement_action="release_on_success", specification=specification,
    ), snapshot(kernel).version, actor=ALICE, request_id="register-work"))
    aid = entity(kernel.create_allocation_and_bind(
        "stable-work", "allocate", snapshot(kernel).version, actor=ALICE,
        request_id="allocate-work"), "allocation")
    advance(kernel, 3)
    jid = entity(kernel.create_job_and_bind(
        "stable-work", "job", snapshot(kernel).version, actor=ALICE,
        request_id="start-work"), "job")
    advance(kernel, 20)
    recall(kernel)
    accepted(checkpoint(kernel, aid))
    advance(kernel, 7)
    accepted(resume(kernel, aid))
    working = snapshot(kernel).intents[0]
    assert working.first_arrival_at == 0 and working.job_ids == (jid,)
    assert working.allocation_ids == (aid,)
    assert [attempt.ref.generation for attempt in working.attempts] == [1, 2]
    assert working.attempts[0].finished_at == 23
    assert working.attempts[0].checkpoint.verification_status == "unverified"
    assert working.attempts[1].started_at == 30
    advance(kernel, 79)
    assert job_view(kernel, jid).state == "Running"
    advance(kernel, 1)
    completed = snapshot(kernel).intents[0]
    assert completed.state == "completed" and completed.result.kind == "success"
    assert completed.first_arrival_at == 0 and completed.result.recorded_at == 110
    assert allocation_view(kernel, aid).state == "Released"
    assert all(attempt.checkpoint is None or
               attempt.checkpoint.continuation_status != "applied"
               for attempt in completed.attempts)
    assert_no_overcommit(kernel)


def cpu_observation(kernel, jid, kind, *, attempt=None):
    snap = snapshot(kernel)
    return command(kernel, m.ExecutorReportParameters(
        kind="executor.report", event=m.ExecutorEvent(
            callback_id=f"protocol-only-{next(REQUESTS)}", version=snap.version,
            attempt=attempt or job_view(kernel, jid).attempt,
            kind=kind, mode="cpu_reference", observed_at=snap.now,
        ),
    ), actor=SYSTEM)


def test_cpu_protocol_clock_and_unknown_observation_cannot_fabricate_stop(tmp_path):
    """Protocol-level synthetic callbacks: this is NOT physical execution evidence."""
    current = Kernel(tmp_path / "physical-protocol.sqlite3", inventory=(node(),),
                     simulation_duration_seconds=1.0, execution_mode="cpu_reference")
    aid = allocation(current)
    jid = job(current, aid)
    accepted(cpu_observation(current, jid, "started"))
    advance(current, 200)
    assert job_view(current, jid).state == "Running", "Clock cannot fabricate CPU completion"
    recall(current)
    advance(current, 5)
    assert allocation_view(current, aid).state == "PreemptRequested"
    accepted(cpu_observation(current, jid, "unknown"))
    waiting = allocation(current, owner=BOB)
    advance(current, 100)
    assert allocation_view(current, waiting).state == "Pending"
    assert allocation_view(current, aid).placement is not None
    assert resume(current, aid).outcome == "rejected"
    accepted(cpu_observation(current, jid, "stopped"))
    assert allocation_view(current, aid).state == "Expired"
    assert allocation_view(current, waiting).state == "Allocated"
    assert job_view(current, jid).state == "Failed"
    assert_no_overcommit(current)


def test_cpu_protocol_public_ready_is_not_verified_resume_permission(tmp_path):
    """A synthetic stop receipt cannot upgrade a public URI into verified artifact."""
    current = Kernel(tmp_path / "physical-unverified.sqlite3", inventory=(node(), node(2)),
                     execution_mode="cpu_reference")
    aid = allocation(current, mode="checkpoint")
    jid = job(current, aid)
    recall(current)
    accepted(checkpoint(current, aid, uri="file:///unverified.json"))
    assert allocation_view(current, aid).state == "PreemptRequested"
    assert allocation_view(current, aid).placement is not None
    accepted(cpu_observation(current, jid, "stopped"))
    assert allocation_view(current, aid).state == "Preempted"
    denied = resume(current, aid)
    assert denied.outcome == "rejected" and denied.error.code == "execution_unconfirmed"
    assert job_view(current, jid).attempt.generation == 1
    assert current.export_state(ADMIN)["allocations"][aid]["restart_count"] == 0


def test_cpu_protocol_reset_quarantine_cannot_be_bypassed_by_renaming_node(tmp_path):
    """Reset does not prove a physical stop; old device IDs must remain fenced."""
    current = Kernel(tmp_path / "reset-alias.sqlite3", inventory=(node(),),
                     execution_mode="cpu_reference")
    aid = allocation(current)
    job(current, aid)
    accepted(command(current, m.ResetParameters(kind="test.reset"), actor=TEST))
    assert not snapshot(current).allocations and not snapshot(current).jobs
    supplier = m.AuthenticatedActor(actor_id="supplier1", role="contributor",
                                   owner_id="supplier1")
    alias = node().model_copy(update={"node_id": "renamed-node"})
    registration = command(current, m.ContributionRegisterParameters(
        kind="contribution.register", node=alias,
    ), actor=supplier)
    if registration.outcome != "rejected":
        new_aid = allocation(current)
        assert allocation_view(current, new_aid).state == "Pending", (
            "An unconfirmed old process still owns g1; a new node name cannot free it")
    assert all(item.state not in {"Allocated", "Running"}
               for item in snapshot(current).allocations)


def test_cpu_protocol_cancel_during_recall_stop_completes_both_transitions(tmp_path):
    """Synthetic receipts test control-plane ordering, not actual process stopping."""
    current = Kernel(tmp_path / "cancel-recall.sqlite3", inventory=(node(), node(2)),
                     execution_mode="cpu_reference")
    aid = allocation(current)
    jid = job(current, aid)
    recall(current)
    accepted(checkpoint(current, aid))
    cancelled = accepted(command(current, m.JobCancelParameters(kind="job.cancel", job_id=jid)))
    assert cancelled.outcome == "committed" and cancelled.event_ids, (
        "A durable CPU cancellation request is an effect, even before stop acknowledgment")
    assert allocation_view(current, aid).placement is not None
    assert job_view(current, jid).state != "Cancelled", "Stop has not yet been acknowledged"
    accepted(cpu_observation(current, jid, "stopped"))
    assert job_view(current, jid).state == "Cancelled"
    assert allocation_view(current, aid).state == "Preempted"
    assert allocation_view(current, aid).placement is None
    assert_no_overcommit(current)


@pytest.mark.parametrize("recall_first", [True, False])
def test_cpu_protocol_timeout_and_recall_stop_complete_in_both_orders(tmp_path, recall_first):
    """A timeout and contributor interruption share one eventual stop receipt."""
    current = Kernel(tmp_path / "timeout-recall.sqlite3", inventory=(node(), node(2)),
                     execution_mode="cpu_reference")
    aid = allocation(current)
    jid = job(current, aid, timeout=2)
    if recall_first:
        recall(current)
        accepted(checkpoint(current, aid))
        advance(current, 2)
    else:
        advance(current, 2)
        recall(current, notice=1)
        advance(current, 1)
    assert allocation_view(current, aid).placement is not None
    accepted(cpu_observation(current, jid, "stopped"))
    assert job_view(current, jid).state == "Failed"
    assert allocation_view(current, aid).state == "Preempted"
    assert allocation_view(current, aid).placement is None
    assert current.export_state(ADMIN)["allocations"][aid]["restart_count"] == 0
    assert_no_overcommit(current)


@pytest.mark.parametrize("job_terminal", ["cancel", "timeout"])
def test_cpu_protocol_explicit_release_wins_pending_recall_and_job_finish(tmp_path, job_terminal):
    """Lease release remains terminal when its delayed physical stop finally arrives."""
    current = Kernel(tmp_path / "release-recall.sqlite3", inventory=(node(), node(2)),
                     execution_mode="cpu_reference")
    aid = allocation(current)
    jid = job(current, aid, timeout=2)
    recall(current)
    accepted(checkpoint(current, aid))
    if job_terminal == "cancel":
        accepted(command(current, m.JobCancelParameters(kind="job.cancel", job_id=jid)))
    else:
        advance(current, 2)
    released = accepted(command(current, m.AllocationTargetParameters(
        kind="allocation.release", allocation_id=aid,
    )))
    assert released.outcome == "committed" and released.event_ids
    assert allocation_view(current, aid).placement is not None
    old_attempt = job_view(current, jid).attempt
    accepted(cpu_observation(current, jid, "stopped"))
    assert allocation_view(current, aid).state == "Released"
    assert allocation_view(current, aid).placement is None
    assert job_view(current, jid).state in {"Cancelled", "Failed"}
    assert cpu_observation(current, jid, "stopped", attempt=old_attempt).outcome == "rejected"
    assert allocation_view(current, aid).state == "Released"
    assert_no_overcommit(current)
