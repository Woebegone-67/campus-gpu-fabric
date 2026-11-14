"""Independent CP1 joint workflow challenges against the authoritative kernel.

These are automated, isolated logical-simulation checks, not two-human UI evidence.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.policies import make_policy
from campus_fabric.workflows.devhost import WorkflowWorker
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator

OWNER = m.AuthenticatedActor(actor_id="audit-alice", role="researcher", owner_id="alice")
SYSTEM = m.AuthenticatedActor(actor_id="audit-system", role="system")


def node():
    resources = m.ResourceAmounts(cpu_millicores=1000, memory_mib=1024)
    return m.NodeInventory(node_id="audit-node", contributor_owner_id="supplier",
        state="Ready", capacity=resources, available=resources, observed_at=0.0)


def registration(name="audit-work", settlement="release_on_terminal", timeout=100):
    return m.WorkIntentRegistration(intent_id=name, owner_id="alice",
        execution_mode="logical_simulation", settlement_action=settlement,
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id="alice",
                ttl_seconds=300, request=m.ResourceRequest(cpu_millicores=1000,
                    memory_mib=1024, gpu=m.GPURequest(count=0))),
            job=m.JobSpecification(image="logical-only", command=("not-executed",),
                                   timeout_seconds=timeout)))


def register(kernel, name="audit-work", **kwargs):
    result = kernel.register_intent(registration(name, **kwargs),
        kernel.decision_snapshot().version, actor=OWNER, request_id=f"register-{name}")
    assert result.outcome == "committed", result.error


def read(kernel):
    return kernel.read_snapshot(m.ReadQuery(actor=OWNER))


def advance(kernel, seconds):
    receipt = kernel.commit(m.CommandEnvelope(request_id="audit-clock", actor=SYSTEM,
        operation="clock.advance", parameters=m.ClockAdvanceParameters(
            kind="clock.advance", advance_seconds=float(seconds), source="manual")))
    assert receipt.outcome == "committed", receipt.error


@pytest.mark.parametrize("mode,policy", [("builtin", None), ("external", "B0"),
                                        ("external", "B1"), ("external", "B2")])
def test_worker_drains_two_intents_after_settlement_without_duplicate_bindings(tmp_path, mode, policy):
    kernel = Kernel(tmp_path / "state.sqlite", inventory=(node(),), scheduling_mode=mode)
    register(kernel, "first")
    register(kernel, "second")
    worker = WorkflowWorker(kernel, policy_name=policy)
    for _ in range(3):
        worker.tick()
    initial = read(kernel)
    assert len(initial.allocations) == 2 and len(initial.jobs) == 1
    assert sum(a.state == "Running" for a in initial.allocations) == 1
    assert sum(a.state == "Pending" for a in initial.allocations) == 1
    advance(kernel, 10)
    # Recreate both kernel and worker, exercising durable discovery after release.
    reopened = Kernel(tmp_path / "state.sqlite", scheduling_mode=mode)
    worker = WorkflowWorker(reopened, policy_name=policy)
    for _ in range(3):
        worker.tick()
    advance(reopened, 10)
    worker.tick()
    final = read(reopened)
    assert len(final.allocations) == len(final.jobs) == len(final.intents) == 2
    assert all(intent.state == "completed" for intent in final.intents)
    assert all(a.state == "Released" for a in final.allocations)
    assert reopened.decision_snapshot().inventory[0].available == node().capacity


@pytest.mark.parametrize("settlement,lease_state", [("keep_lease", "Allocated"),
    ("release_on_success", "Allocated"), ("release_on_terminal", "Released")])
def test_failed_execution_settlement_survives_worker_restart(tmp_path, settlement, lease_state):
    kernel = Kernel(tmp_path / "state.sqlite", inventory=(node(),))
    register(kernel, settlement=settlement, timeout=3)
    WorkflowWorker(kernel).tick()
    advance(kernel, 3)
    reopened = Kernel(tmp_path / "state.sqlite")
    worker = WorkflowWorker(reopened)
    for _ in range(3):
        worker.tick()
    final = read(reopened)
    assert final.intents[0].state == "failed"
    assert final.jobs[0].state == "Failed"
    assert final.allocations[0].state == lease_state
    assert len(final.jobs) == len(final.allocations) == 1


def test_32_concurrent_orchestrators_reconcile_one_intent_once(tmp_path):
    path = tmp_path / "state.sqlite"
    kernel = Kernel(path, inventory=(node(),))
    register(kernel)

    def run(_):
        isolated_kernel = Kernel(path)
        return WorkflowOrchestrator(isolated_kernel, isolated_kernel).advance(
            "audit-work", actor=OWNER, allocation_stage_id="allocate-v1", job_stage_id="job-v1")

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(run, range(32)))
    assert all(result.state in {"running", "waiting", "blocked", "partial_success"}
               for result in results)
    WorkflowWorker(kernel).tick()
    final = read(kernel)
    assert final.intents[0].state == "running"
    assert len(final.allocations) == len(final.jobs) == 1
    assert len(final.intents[0].attempts) == 1
    assert len(final.intents[0].stages) == 2


def test_lost_job_response_then_restart_does_not_reborrow(tmp_path):
    kernel = Kernel(tmp_path / "state.sqlite", inventory=(node(),))
    register(kernel)

    class ResponseLost:
        create_allocation_and_bind = kernel.create_allocation_and_bind

        def create_job_and_bind(self, *args, **kwargs):
            result = kernel.create_job_and_bind(*args, **kwargs)
            assert result.outcome == "committed"
            raise ConnectionError("injected loss after the transaction committed")

    progress = WorkflowOrchestrator(ResponseLost(), kernel).advance(
        "audit-work", actor=OWNER, allocation_stage_id="allocate-v1", job_stage_id="job-v1")
    assert progress.state == "unknown"
    before = read(kernel)
    reopened = Kernel(tmp_path / "state.sqlite")
    WorkflowWorker(reopened).tick()
    after = read(reopened)
    assert after.version == before.version
    assert after.intents == before.intents
    assert len(after.allocations) == len(after.jobs) == 1


def test_external_plan_cannot_misattribute_selected_intent(tmp_path):
    kernel = Kernel(tmp_path / "state.sqlite", inventory=(node(),), scheduling_mode="external")
    register(kernel)
    WorkflowOrchestrator(kernel, kernel).advance("audit-work", actor=OWNER)
    plan = make_policy("B2").plan(kernel.decision_snapshot())
    selected = next(candidate for candidate in plan.candidates
                    if candidate.candidate_id == plan.selected_candidate_id)
    altered = selected.model_copy(update={"intent_id": "unrelated-intent"})
    plan = plan.model_copy(update={"candidates": tuple(
        altered if item.candidate_id == selected.candidate_id else item for item in plan.candidates)})
    before = read(kernel)
    receipt = kernel.commit(m.CommandEnvelope(request_id="audit-plan", actor=SYSTEM,
        operation="decision.commit", expected_version=plan.snapshot_version,
        parameters=m.DecisionCommitParameters(kind="decision.commit", plan=plan)))
    assert receipt.outcome == "rejected", "Kernel accepted a plan attributed to an unrelated intent"
    assert read(kernel).version == before.version
