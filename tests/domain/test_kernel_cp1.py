"""CP1 command-port acceptance, independent of the production allocation helpers."""

from pathlib import Path

import pytest
from campus_fabric.domain.kernel import Kernel

from campus_fabric.contracts.models import (
    AllocationCreateParameters,
    AllocationRenewParameters,
    AllocationTargetParameters,
    AuthenticatedActor,
    ClockAdvanceParameters,
    CommandEnvelope,
    DecisionCommitParameters,
    DecisionPlan,
    GPUDevice,
    GPURequest,
    JobCancelParameters,
    JobCreateParameters,
    JobSpecification,
    NodeInventory,
    ReadQuery,
    ResetParameters,
    ResourceAmounts,
    ResourceRequest,
    SearchCoverage,
    VersionedArtifact,
    WorkIntentRegistration,
    WorkSpecification,
)

ALICE = AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice")
BOB = AuthenticatedActor(actor_id="bob", role="researcher", owner_id="bob")
ADMIN = AuthenticatedActor(actor_id="admin", role="administrator")
TEST = AuthenticatedActor(actor_id="test", role="test_driver")
SYSTEM = AuthenticatedActor(actor_id="system", role="system")


def node():
    capacity = ResourceAmounts(
        cpu_millicores=4000,
        memory_mib=8192,
        ephemeral_storage_mib=1024,
        gpu_count=1,
        gpu_memory_mib=8192,
    )
    return NodeInventory(
        node_id="n1", contributor_owner_id="supplier", state="Ready",
        capacity=capacity, available=capacity, observed_at=0.0,
        devices=(GPUDevice(device_id="g1", physical_gpu_id="g1", model="A10",
                           memory_mib=8192, state="available"),),
    )


def allocation(owner="alice", *, ttl=30, cpu=1000):
    return AllocationCreateParameters(
        kind="allocation.create", owner_id=owner, ttl_seconds=ttl,
        request=ResourceRequest(cpu_millicores=cpu, memory_mib=1024,
                                ephemeral_storage_mib=32,
                                gpu=GPURequest(count=1, model="A10", memory_mib=4096)),
    )


def commit(kernel, params, *, actor=ALICE, request_id="req", key=None, version=None):
    return kernel.commit(CommandEnvelope(
        request_id=request_id, actor=actor, operation=params.kind, parameters=params,
        idempotency_key=key, expected_version=version,
    ))


def entity(receipt, kind):
    assert receipt.outcome != "rejected", receipt
    return next(item.entity_id for item in receipt.objects if item.kind == kind)


def view(kernel, actor=ALICE):
    return kernel.read_snapshot(ReadQuery(actor=actor, global_scope=actor == ADMIN))


def advance(kernel, seconds, request_id="tick"):
    result = commit(kernel, ClockAdvanceParameters(
        kind="clock.advance", advance_seconds=float(seconds), source="test_control",
    ), actor=TEST, request_id=request_id)
    assert result.outcome != "rejected", result
    return result


def job(allocation_id, *, timeout=100):
    return JobCreateParameters(kind="job.create", allocation_id=allocation_id,
                               image="cp1-sim", command=("work",),
                               timeout_seconds=timeout, max_retries=0)


def registration(intent_id="intent1", *, settlement="keep_lease", timeout=100):
    return WorkIntentRegistration(
        intent_id=intent_id, owner_id="alice", execution_mode="logical_simulation",
        settlement_action=settlement,
        specification=WorkSpecification(
            allocation=allocation(ttl=300),
            job=JobSpecification(image="cp1-sim", command=("work",),
                                 timeout_seconds=timeout, max_retries=0),
        ),
    )


@pytest.fixture
def kernel(tmp_path):
    return Kernel(tmp_path / "state.sqlite3", inventory=(node(),))


def test_tenant_rejection_changes_no_resources_or_owner_events(kernel):
    aid = entity(commit(kernel, allocation()), "allocation")
    before = view(kernel)
    denied = commit(kernel, AllocationTargetParameters(
        kind="allocation.release", allocation_id=aid), actor=BOB, request_id="intruder")
    assert denied.outcome == "rejected"
    assert denied.error.code == "not_found"
    after = view(kernel)
    assert after.allocations == before.allocations
    foreign = view(kernel, BOB)
    assert not foreign.allocations and not foreign.jobs and not foreign.intents
    assert all(event.owner_id == "bob" for event in foreign.events)
    assert kernel.decision_snapshot().inventory[0].available.gpu_count == 0


def test_renewal_deadline_and_double_release_return_once(kernel):
    aid = entity(commit(kernel, allocation()), "allocation")
    advance(kernel, 20)
    renewed = commit(kernel, AllocationRenewParameters(
        kind="allocation.renew", allocation_id=aid, ttl_seconds=30), request_id="renew")
    assert renewed.outcome != "rejected"
    assert view(kernel).allocations[0].expires_at == 50
    advance(kernel, 29, "tick29")
    assert view(kernel).allocations[0].state == "Allocated"
    advance(kernel, 1, "deadline")
    assert view(kernel).allocations[0].state == "Expired"
    denied = commit(kernel, AllocationRenewParameters(
        kind="allocation.renew", allocation_id=aid), request_id="late")
    assert denied.outcome == "rejected"
    for suffix in ("1", "2"):
        commit(kernel, AllocationTargetParameters(kind="allocation.release", allocation_id=aid),
               request_id="release" + suffix)
    inventory = kernel.decision_snapshot().inventory[0]
    assert inventory.available == inventory.capacity


def test_pending_lease_begins_on_grant_not_arrival(kernel):
    first = entity(commit(kernel, allocation(), request_id="first"), "allocation")
    second = entity(commit(kernel, allocation("bob"), actor=BOB,
                           request_id="second"), "allocation")
    assert view(kernel, BOB).allocations[0].expires_at is None
    advance(kernel, 25)
    commit(kernel, AllocationTargetParameters(kind="allocation.release", allocation_id=first),
           request_id="free")
    pending = next(a for a in view(kernel, BOB).allocations if a.allocation_id == second)
    assert pending.state == "Allocated"
    assert pending.expires_at == 55
    advance(kernel, 5, "not-yet")
    assert view(kernel, BOB).allocations[0].state == "Allocated"


def test_one_active_job_and_terminal_job_retains_lease(kernel):
    aid = entity(commit(kernel, allocation(ttl=300)), "allocation")
    jid = entity(commit(kernel, job(aid), request_id="start"), "job")
    second = commit(kernel, job(aid), request_id="duplicate-job")
    assert second.outcome == "rejected"
    assert len(view(kernel).jobs) == 1
    advance(kernel, 10)
    snapshot = view(kernel)
    assert snapshot.jobs[0].job_id == jid and snapshot.jobs[0].state == "Succeeded"
    assert snapshot.allocations[0].state == "Allocated"
    assert kernel.decision_snapshot().inventory[0].available.gpu_count == 0
    entity(commit(kernel, job(aid), request_id="later-job"), "job")
    assert len(view(kernel).jobs) == 2


def test_timeout_is_not_work_duration_and_cancel_retains_lease(kernel):
    aid = entity(commit(kernel, allocation(ttl=300)), "allocation")
    jid = entity(commit(kernel, job(aid, timeout=3), request_id="short-timeout"), "job")
    advance(kernel, 3)
    snapshot = view(kernel)
    assert snapshot.jobs[0].job_id == jid and snapshot.jobs[0].state == "Failed"
    assert snapshot.allocations[0].state == "Allocated"
    jid2 = entity(commit(kernel, job(aid, timeout=100), request_id="long-timeout"), "job")
    advance(kernel, 1, "one-more")
    receipt = commit(kernel, JobCancelParameters(kind="job.cancel", job_id=jid2),
                     request_id="cancel")
    assert receipt.outcome != "rejected"
    assert next(j for j in view(kernel).jobs if j.job_id == jid2).state == "Cancelled"
    assert view(kernel).allocations[0].state == "Allocated"


def _normal_state(kernel):
    snapshot = view(kernel, ADMIN)
    return (
        [(a.owner_id, a.state, a.created_at, a.expires_at, a.placement)
         for a in snapshot.allocations],
        [(j.owner_id, j.state, j.created_at) for j in snapshot.jobs],
        [(n.node_id, n.available) for n in kernel.decision_snapshot().inventory],
        snapshot.now,
    )


def test_large_clock_matches_boundary_steps(tmp_path):
    kernels = [Kernel(tmp_path / (name + ".sqlite3"), inventory=(node(),),
                      simulation_duration_seconds=10.0) for name in ("large", "steps")]
    for current in kernels:
        aid = entity(commit(current, allocation()), "allocation")
        commit(current, job(aid), request_id="job")
        commit(current, allocation("bob"), actor=BOB, request_id="queued")
    advance(kernels[0], 60)
    for index, delta in enumerate((10, 20, 10, 20)):
        advance(kernels[1], delta, f"step{index}")
    assert _normal_state(kernels[0]) == _normal_state(kernels[1])
    assert all(a.state == "Expired" for a in view(kernels[0], ADMIN).allocations)


def test_expiry_wins_exactly_at_execution_boundary(tmp_path):
    current = Kernel(tmp_path / "expiry.sqlite3", inventory=(node(),),
                     simulation_duration_seconds=30.0)
    aid = entity(commit(current, allocation()), "allocation")
    commit(current, job(aid, timeout=30), request_id="job")
    advance(current, 30)
    snapshot = view(current)
    assert snapshot.allocations[0].state == "Expired"
    assert snapshot.jobs[0].state != "Succeeded"
    assert current.decision_snapshot().inventory[0].available.gpu_count == 1


def test_snapshot_is_immutable_detached_causal_prefix(kernel):
    before = kernel.decision_snapshot()
    aid = entity(commit(kernel, allocation()), "allocation")
    assert before.inventory[0].available.gpu_count == 1
    assert kernel.decision_snapshot().inventory[0].available.gpu_count == 0
    with pytest.raises((AttributeError, ValueError)):
        before.inventory[0].available.gpu_count = 50
    assert aid not in str(view(kernel, BOB).model_dump())


def test_workflow_replay_survives_reopen_and_preserves_first_arrival(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    current = Kernel(path, inventory=(node(),))
    initial = current.decision_snapshot().version
    registered = current.register_intent(registration(), initial, actor=ALICE, request_id="intent")
    assert registered.outcome != "rejected"
    alloc_version = current.decision_snapshot().version
    allocated = current.create_allocation_and_bind("intent1", "allocate", alloc_version,
                                                  actor=ALICE, request_id="allocate")
    aid = entity(allocated, "allocation")
    job_version = current.decision_snapshot().version
    started = current.create_job_and_bind("intent1", "job", job_version,
                                         actor=ALICE, request_id="job")
    jid = entity(started, "job")
    # A successful commit whose HTTP response was lost: only durable state is reused.
    reopened = Kernel(path)
    replay = reopened.create_job_and_bind("intent1", "job", job_version,
                                         actor=ALICE, request_id="retry-job")
    assert entity(replay, "job") == jid
    replay_a = reopened.create_allocation_and_bind("intent1", "allocate", alloc_version,
                                                   actor=ALICE, request_id="retry-allocation")
    assert entity(replay_a, "allocation") == aid
    assert len(view(reopened).jobs) == len(view(reopened).allocations) == 1
    intent = view(reopened).intents[0]
    assert intent.first_arrival_at == 0
    assert intent.allocation_ids == (aid,) and intent.job_ids == (jid,)
    assert len({s.stage_id for s in intent.stages}) == len(intent.stages)


def test_workflow_partial_success_does_not_reborrow_after_job_failure(kernel):
    kernel.register_intent(registration(timeout=1), kernel.decision_snapshot().version,
                           actor=ALICE, request_id="intent")
    alloc_v = kernel.decision_snapshot().version
    alloc_r = kernel.create_allocation_and_bind("intent1", "allocate", alloc_v,
                                               actor=ALICE, request_id="allocation")
    aid = entity(alloc_r, "allocation")
    job_v = kernel.decision_snapshot().version
    job_r = kernel.create_job_and_bind("intent1", "job", job_v,
                                      actor=ALICE, request_id="start")
    jid = entity(job_r, "job")
    advance(kernel, 1)
    snapshot = view(kernel)
    assert snapshot.jobs[0].state == "Failed"
    assert snapshot.allocations[0].state == "Allocated"
    assert snapshot.intents[0].state == "failed"
    assert entity(kernel.create_allocation_and_bind(
        "intent1", "allocate", alloc_v, actor=ALICE, request_id="again-a"), "allocation") == aid
    assert entity(kernel.create_job_and_bind(
        "intent1", "job", job_v, actor=ALICE, request_id="again-j"), "job") == jid
    assert len(view(kernel).allocations) == len(view(kernel).jobs) == 1


@pytest.mark.parametrize("settlement", ["release_on_success", "release_on_terminal"])
def test_workflow_explicit_settlement_releases_lease(kernel, settlement):
    kernel.register_intent(registration(settlement=settlement), kernel.decision_snapshot().version,
                           actor=ALICE, request_id="intent")
    kernel.create_allocation_and_bind("intent1", "allocate", kernel.decision_snapshot().version,
                                      actor=ALICE, request_id="allocation")
    kernel.create_job_and_bind("intent1", "job", kernel.decision_snapshot().version,
                              actor=ALICE, request_id="job")
    advance(kernel, 10)
    snapshot = view(kernel)
    assert snapshot.intents[0].state == "completed"
    assert snapshot.allocations[0].state == "Released"
    assert kernel.decision_snapshot().inventory[0].available.gpu_count == 1


def test_conflicting_registration_and_cross_operation_stage_are_rejected(kernel):
    first_version = kernel.decision_snapshot().version
    registered = kernel.register_intent(registration(), first_version,
                                        actor=ALICE, request_id="intent")
    assert registered.outcome != "rejected"
    conflict = kernel.register_intent(registration(timeout=7), first_version,
                                     actor=ALICE, request_id="changed")
    assert conflict.outcome == "rejected"
    version = kernel.decision_snapshot().version
    kernel.create_allocation_and_bind("intent1", "same-stage", version,
                                      actor=ALICE, request_id="allocation")
    collision = kernel.create_job_and_bind("intent1", "same-stage", version,
                                           actor=ALICE, request_id="job")
    assert collision.outcome == "rejected"
    assert not view(kernel).jobs


def test_stale_plan_cannot_modify_inventory(kernel):
    old = kernel.decision_snapshot()
    commit(kernel, allocation())
    current = kernel.decision_snapshot()
    artifact = VersionedArtifact(name="test", version="1", sha256="a" * 64)
    plan = DecisionPlan(
        decision_id="obsolete", snapshot_version=old.version, decision_at=old.now,
        policy=artifact, model=artifact, candidates=(),
        coverage=SearchCoverage(scope="complete", domain_description="empty queue",
                                examined_count=0, domain_size=0),
        selected_candidate_id=None, rationale="No demand", tie_break_rule="stable ID",
    )
    receipt = commit(kernel, DecisionCommitParameters(kind="decision.commit", plan=plan),
                     actor=SYSTEM, request_id="plan", version=old.version)
    assert receipt.outcome == "rejected" and receipt.error.code == "stale_version"
    assert kernel.decision_snapshot().inventory == current.inventory


def test_reset_persists_epoch_and_clears_inventory(tmp_path):
    path = Path(tmp_path) / "reset.sqlite3"
    current = Kernel(path, inventory=(node(),))
    receipt = commit(current, allocation(), key="same-key")
    old_epoch = receipt.version.epoch
    reset = commit(current, ResetParameters(kind="test.reset"), actor=TEST, request_id="reset")
    assert reset.outcome != "rejected"
    reopened = Kernel(path, inventory=(node(),))
    assert reopened.decision_snapshot().version.epoch == old_epoch + 1
    assert not reopened.decision_snapshot().inventory
    assert not view(reopened).allocations


def test_process_exit_after_stage_commit_lost_response_is_retryable(tmp_path):
    import json
    import subprocess
    import sys

    path = tmp_path / "abrupt.sqlite3"
    script = r'''
import json, os, sys
from campus_fabric.domain.kernel import Kernel
from campus_fabric.contracts.models import (
    NodeInventory, WorkIntentRegistration, AuthenticatedActor,
)
data = json.loads(sys.stdin.read())
node = NodeInventory.model_validate_json(json.dumps(data["node"]))
registration = WorkIntentRegistration.model_validate_json(json.dumps(data["registration"]))
actor = AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice")
kernel = Kernel(sys.argv[1], inventory=(node,))
r = kernel.register_intent(registration, kernel.decision_snapshot().version,
                          actor=actor, request_id="register-child")
assert r.outcome != "rejected", r
r = kernel.create_allocation_and_bind("intent1", "allocate", kernel.decision_snapshot().version,
                                     actor=actor, request_id="allocate-child")
assert r.outcome != "rejected", r
r = kernel.create_job_and_bind("intent1", "job", kernel.decision_snapshot().version,
                              actor=actor, request_id="job-child")
assert r.outcome != "rejected", r
# No response or graceful application shutdown after the committed stage.
os._exit(0)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        input=json.dumps({"node": node().model_dump(mode="json"),
                          "registration": registration().model_dump(mode="json")}),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    reopened = Kernel(path)
    before = view(reopened)
    assert len(before.jobs) == len(before.allocations) == len(before.intents) == 1
    old_version = before.intents[0].stages[-1].receipt.version
    replay = reopened.create_job_and_bind("intent1", "job", old_version,
                                         actor=ALICE, request_id="after-restart")
    assert entity(replay, "job") == before.jobs[0].job_id
    assert len(view(reopened).jobs) == len(view(reopened).allocations) == 1
