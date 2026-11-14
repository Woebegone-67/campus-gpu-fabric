"""Independent CP2 experiment/policy audit; not CP3 performance acceptance.

These tests execute real isolated transactional kernels with a declared logical
work model. They make no claim about CPU/GPU execution or policy superiority.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.policies import make_policy
from campus_fabric.policies.candidate import _can_coexist
from campus_fabric.workflows import experiment_runner as runner
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator

ROOT = Path(__file__).resolve().parents[2]
ADMIN = m.AuthenticatedActor(actor_id="joint-audit", role="administrator")
SCENARIOS = tuple(row["scenario_id"] for row in runner.catalog())


def assert_resource_accounting(snapshot):
    """Recompute claims independently from the policy's feasibility flags."""
    claims = Counter()
    for node in snapshot.inventory:
        pieces = [piece for hold in snapshot.occupancy for piece in hold.placement.slices
                  if piece.node_id == node.node_id]
        for field in ("cpu_millicores", "memory_mib", "ephemeral_storage_mib"):
            used = sum(getattr(piece.resources, field) for piece in pieces)
            assert used <= getattr(node.capacity, field)
            if node.state == "Ready":
                assert used + getattr(node.available, field) == getattr(node.capacity, field)
            else:
                assert getattr(node.available, field) == 0
        devices = {device.device_id: device for device in node.devices}
        parent_memory = Counter()
        parent_whole = set()
        parent_slices = set()
        for piece in pieces:
            for identity in piece.device_ids:
                claims[identity] += 1
                device = devices[identity]
                parent_memory[device.physical_gpu_id] += device.memory_mib
                (parent_whole if device.profile is None else parent_slices).add(
                    device.physical_gpu_id)
        assert not parent_whole.intersection(parent_slices)
        assert all(amount <= devices[parent].memory_mib
                   for parent, amount in parent_memory.items())
    assert all(count == 1 for count in claims.values())


@pytest.mark.parametrize("scenario_id", SCENARIOS)
@pytest.mark.parametrize("policy_name", runner.POLICIES)
def test_all_registered_scenarios_keep_full_cohort_and_valid_current_resources(
        tmp_path, scenario_id, policy_name):
    inputs = runner.scenario_input(ROOT, scenario_id, 7)
    before = copy.deepcopy(inputs)
    output = runner.execute_scenario(inputs, policy_name, db_path=tmp_path / "run.sqlite")
    assert inputs == before
    assert output["failures"] == []
    assert output["snapshot"].now == inputs["horizon"]
    assert all(intent.execution_mode == "logical_simulation"
               for intent in output["snapshot"].intents)
    assert {intent.intent_id for intent in output["snapshot"].intents} == {
        work["intent_id"] for work in inputs["works"]}
    for record in output["decisions"]:
        snapshot = m.DecisionSnapshot.model_validate_json(json.dumps(record["snapshot"]))
        plan = m.DecisionPlan.model_validate_json(json.dumps(record["plan"]))
        assert_resource_accounting(snapshot)
        assert len(plan.candidates) <= runner.MAX_CANDIDATES
        assert plan.coverage.scope == "partial" and not plan.asserts_pool_infeasible
        # P must retain the admitted demand of B2, not change queue order while
        # attributing a difference to its placement-opportunity component.
        if policy_name == "P":
            baseline = make_policy("B2", max_candidates=runner.MAX_CANDIDATES).plan(snapshot)
            by_id = {candidate.candidate_id: candidate for candidate in plan.candidates}
            if plan.selected_candidate_id is None:
                assert baseline.selected_candidate_id is None
            else:
                assert (by_id[plan.selected_candidate_id].demand_id
                        == by_id[baseline.selected_candidate_id].demand_id)
        assert all(demand.arrived_at <= snapshot.now for demand in snapshot.demands)
        assert all(notice.issued_at <= snapshot.now for notice in snapshot.notices)
    summary = runner._summary(inputs, output, "independent-audit", policy_name)
    assert len(summary["works"]) == len(inputs["works"])
    assert all(row["effective_service"] == float(row["status"] == "completed")
               for row in summary["works"])


def test_p_pair_compatibility_uses_valid_kernel_mig_inventory(tmp_path):
    inputs = runner.scenario_input(ROOT, "cp2-recall-v1", 0)
    nodes = tuple(m.NodeInventory.model_validate_json(json.dumps(row)) for row in inputs["inventory"])
    kernel = Kernel(tmp_path / "mig.sqlite", inventory=nodes, scheduling_mode="external")
    workflow = WorkflowOrchestrator(kernel, kernel)
    try:
        for index in range(2):
            owner = f"owner-{index}"
            request = m.WorkIntentRegistration(
                intent_id=f"mig-{index}", owner_id=owner, execution_mode="logical_simulation",
                settlement_action="keep_lease", specification=m.WorkSpecification(
                    allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id=owner,
                        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                            gpu=m.GPURequest(count=1, model="A100", memory_mib=1024,
                                             profile="mig-2g.20gb"))),
                    job=m.JobSpecification(image="logical", command=("no-real-execution",))))
            actor = m.AuthenticatedActor(actor_id=owner, owner_id=owner, role="researcher")
            assert workflow.start(request, kernel.decision_snapshot().version,
                actor=actor, request_id=f"register-{index}").state == "waiting"
        snapshot = kernel.decision_snapshot()
        plan = make_policy("P").plan(snapshot)
        legal = [candidate for candidate in plan.candidates if candidate.feasibility == "legal"]
        assert len(legal) == 4
        for left in legal:
            for right in legal:
                if left.owner_id == right.owner_id:
                    continue
                # Both configured 20 GiB slices fit the physical 40 GiB parent;
                # reusing the same slice does not. No malformed inventory needed.
                distinct = left.placement.slices[0].device_ids != right.placement.slices[0].device_ids
                assert _can_coexist(left, right, snapshot) == distinct
            score = next(score.value for score in left.scores
                         if score.name == "observed_opportunity_loss")
            assert score == .5
    finally:
        kernel.store.close()
