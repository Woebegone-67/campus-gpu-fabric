"""B ports against A's real isolated SQLite CP1 kernel; logical simulation only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.policies import make_policy
from campus_fabric.workflows import WorkflowOrchestrator

ROOT = Path(__file__).resolve().parents[2]
OWNER = m.AuthenticatedActor(actor_id="researcher-a", role="researcher", owner_id="alice")
SYSTEM = m.AuthenticatedActor(actor_id="policy-worker", role="system")


def inventory():
    rows = json.loads((ROOT / "configs/cp1_inventory.json").read_text())["nodes"]
    return tuple(m.NodeInventory.model_validate_json(json.dumps(row)) for row in rows)


def registration(intent_id="work-a", *, settlement="keep_lease", profile=None):
    return m.WorkIntentRegistration(
        intent_id=intent_id, owner_id="alice", execution_mode="logical_simulation",
        settlement_action=settlement,
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id="alice",
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                    gpu=m.GPURequest(count=1, profile=profile, memory_mib=1024))),
            job=m.JobSpecification(image="declared-simulation", command=("no-real-execution",)),
        ),
    )


def read(kernel):
    return kernel.read_snapshot(m.ReadQuery(actor=OWNER))


def start(kernel, **kwargs):
    return WorkflowOrchestrator(kernel, kernel).start(registration(**kwargs),
        kernel.decision_snapshot().version, actor=OWNER, request_id="register-request")


def advance_clock(kernel, seconds):
    return kernel.commit(m.CommandEnvelope(request_id="advance-clock", actor=SYSTEM,
        operation="clock.advance", parameters=m.ClockAdvanceParameters(
            kind="clock.advance", advance_seconds=float(seconds), source="manual")))


def commit_plan(kernel, plan):
    return kernel.commit(m.CommandEnvelope(request_id=f"apply-{plan.decision_id}", actor=SYSTEM,
        operation="decision.commit", expected_version=plan.snapshot_version,
        parameters=m.DecisionCommitParameters(kind="decision.commit", plan=plan)))


@pytest.mark.parametrize("settlement,lease_state", [
    ("keep_lease", "Allocated"),
    ("release_on_success", "Released"),
    ("release_on_terminal", "Released"),
])
def test_workflow_runs_and_settles_using_actual_kernel(tmp_path, settlement, lease_state):
    kernel = Kernel(tmp_path / "flow.sqlite", inventory=inventory())
    result = start(kernel, settlement=settlement)
    assert result.state == "running"
    assert result.intent.first_arrival_at == 0.0
    assert len(read(kernel).allocations) == len(read(kernel).jobs) == 1
    assert advance_clock(kernel, 10.0).outcome == "committed"
    result = WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=OWNER)
    assert result.state == "terminal"
    assert result.intent.state == "completed"
    assert read(kernel).allocations[0].state == lease_state
    assert lease_state in result.reason
    # Completion is synthetic and does not invent result files or CPU execution.
    assert result.intent.result.artifact_uris == ()


def test_replacing_kernel_and_orchestrator_preserves_bound_objects(tmp_path):
    path = tmp_path / "restart.sqlite"
    kernel = Kernel(path, inventory=inventory())
    started = start(kernel)
    old = read(kernel)
    reopened = Kernel(path)
    result = WorkflowOrchestrator(reopened, reopened).advance("work-a", actor=OWNER)
    assert result.state == "running"
    assert result.intent.job_ids == started.intent.job_ids
    assert read(reopened).version == old.version
    assert len(read(reopened).allocations) == len(read(reopened).jobs) == 1


@pytest.mark.parametrize("name", ["B0", "B1", "B2"])
def test_external_policy_plan_is_accepted_by_real_kernel(tmp_path, name):
    kernel = Kernel(tmp_path / f"external-{name}.sqlite", inventory=inventory(),
                    scheduling_mode="external")
    initial = start(kernel)
    assert initial.state == "waiting"
    assert read(kernel).allocations[0].state == "Pending"
    plan = make_policy(name).plan(kernel.decision_snapshot())
    assert plan.selected_candidate_id is not None
    receipt = commit_plan(kernel, plan)
    assert receipt.outcome == "committed", receipt.error
    assert read(kernel).allocations[0].state == "Allocated"
    result = WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=OWNER)
    assert result.state == "running"
    assert read(kernel).decisions[0].decision_id == plan.decision_id


def test_stale_plan_does_not_grant_a_lease(tmp_path):
    kernel = Kernel(tmp_path / "stale.sqlite", inventory=inventory(), scheduling_mode="external")
    start(kernel)
    plan = make_policy("B2").plan(kernel.decision_snapshot())
    advance_clock(kernel, 1.0)
    before = read(kernel)
    receipt = commit_plan(kernel, plan)
    assert receipt.outcome == "rejected" and receipt.error.code == "stale_version"
    assert read(kernel).version == before.version
    assert read(kernel).allocations[0].state == "Pending"


def test_slice_remains_placeable_when_no_whole_parent_is_available(tmp_path):
    kernel = Kernel(tmp_path / "mig.sqlite", inventory=inventory(), scheduling_mode="external")
    for i in range(2):
        name = f"slice-work-{i}"
        start(kernel, intent_id=name, profile="mig-2g.20gb")
        snapshot = kernel.decision_snapshot()
        if i == 1:
            gpu_node = next(n for n in snapshot.inventory if n.node_id == "sim-a100")
            assert gpu_node.available.gpu_count == 0
        plan = make_policy("B2").plan(snapshot)
        assert plan.selected_candidate_id is not None
        assert commit_plan(kernel, plan).outcome == "committed"
        assert WorkflowOrchestrator(kernel, kernel).advance(name, actor=OWNER).state == "running"
    assert len(read(kernel).jobs) == 2
    assert len({a.placement.slices[0].device_ids[0] for a in read(kernel).allocations}) == 2


def test_hidden_model_duration_cannot_change_admission_plan(tmp_path):
    plans = []
    for duration in (10.0, 1000.0):
        kernel = Kernel(tmp_path / f"hidden-{duration}.sqlite", inventory=inventory(),
                        scheduling_mode="external", simulation_duration_seconds=duration)
        start(kernel)
        plans.append(make_policy("B2").plan(kernel.decision_snapshot()))
    assert plans[0] == plans[1]


def test_job_stage_version_race_preserves_lease_then_retries_safely(tmp_path):
    kernel = Kernel(tmp_path / "partial.sqlite", inventory=inventory())

    class RacePort:
        race = True
        register_intent = kernel.register_intent
        create_allocation_and_bind = kernel.create_allocation_and_bind

        def create_job_and_bind(self, intent_id, stage_id, expected_version, *, actor, request_id):
            if self.race:
                self.race = False
                # A real concurrent command advances the version after B's read.
                advance_clock(kernel, 1.0)
            return kernel.create_job_and_bind(intent_id, stage_id, expected_version,
                                              actor=actor, request_id=request_id)

    port = RacePort()
    orchestrator = WorkflowOrchestrator(port, kernel)
    result = orchestrator.start(registration(), kernel.decision_snapshot().version,
                                actor=OWNER, request_id="register-race")
    assert result.state == "partial_success"
    assert result.receipts[-1].error.code == "stale_version"
    assert len(read(kernel).allocations) == 1 and len(read(kernel).jobs) == 0
    lease_id = read(kernel).allocations[0].allocation_id
    # A stores successful stages; rejected stages may be safely retried against
    # the refreshed version using the same stable identity.
    result = orchestrator.advance("work-a", actor=OWNER)
    assert result.state == "running"
    assert len(read(kernel).allocations) == len(read(kernel).jobs) == 1
    assert result.allocation_id == lease_id
