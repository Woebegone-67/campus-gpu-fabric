"""A validates plans from an external policy without borrowing its feasibility claims."""

import hashlib

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.domain.placement import choose_placement


def fixture(tmp_path):
    cap = m.ResourceAmounts(cpu_millicores=2000, memory_mib=4096, gpu_count=1, gpu_memory_mib=8192)
    node = m.NodeInventory(
        node_id="node",
        contributor_owner_id="contributor",
        state="Ready",
        capacity=cap,
        available=cap,
        observed_at=0.0,
        devices=(
            m.GPUDevice(
                device_id="gpu",
                physical_gpu_id="gpu",
                model="A10",
                memory_mib=8192,
                state="available",
            ),
        ),
    )
    kernel = Kernel(tmp_path / "external.sqlite", inventory=(node,), scheduling_mode="external")
    actor = m.AuthenticatedActor(actor_id="owner", owner_id="owner", role="researcher")
    request = m.ResourceRequest(cpu_millicores=1000, memory_mib=1024, gpu=m.GPURequest(count=1))
    receipt = kernel.commit(
        m.CommandEnvelope(
            request_id="new",
            actor=actor,
            operation="allocation.create",
            parameters=m.AllocationCreateParameters(
                kind="allocation.create", owner_id="owner", request=request
            ),
        )
    )
    aid = receipt.objects[0].entity_id
    snapshot = kernel.decision_snapshot()
    placement = choose_placement(request, (), snapshot.inventory)
    candidate = m.CandidateAction(
        candidate_id="candidate",
        action="allocate",
        owner_id="owner",
        demand_id=aid,
        allocation_id=aid,
        placement=placement,
        feasibility="legal",
    )
    return kernel, actor, snapshot, candidate


def plan(snapshot, candidate, ident="decision"):
    return m.DecisionPlan(
        decision_id=ident,
        snapshot_version=snapshot.version,
        decision_at=snapshot.now,
        policy=m.VersionedArtifact(
            name="example", version="1", sha256=hashlib.sha256(b"test-only-policy").hexdigest()
        ),
        model=m.VersionedArtifact(
            name="duration-free",
            version="1",
            sha256=hashlib.sha256(b"test-only-policy").hexdigest(),
        ),
        candidates=(candidate,),
        coverage=m.SearchCoverage(
            scope="complete",
            domain_description="One proposed action",
            examined_count=1,
            domain_size=1,
        ),
        selected_candidate_id=candidate.candidate_id,
        rationale="Place a current demand",
        tie_break_rule="Input order",
    )


def submit(kernel, value):
    return kernel.commit(
        m.CommandEnvelope(
            request_id="commit",
            operation="decision.commit",
            actor=m.AuthenticatedActor(actor_id="policy-service", role="system"),
            parameters=m.DecisionCommitParameters(kind="decision.commit", plan=value),
            expected_version=value.snapshot_version,
        )
    )


def test_external_policy_gets_feasible_pending_work_and_commits(tmp_path):
    kernel, actor, snapshot, candidate = fixture(tmp_path)
    assert len(snapshot.demands) == 1
    assert snapshot.inventory[0].available.gpu_count == 1
    receipt = submit(kernel, plan(snapshot, candidate))
    assert receipt.outcome == "committed"
    view = kernel.read_snapshot(m.ReadQuery(actor=actor))
    assert view.allocations[0].state == "Allocated"
    assert view.decisions[0].decision_id == "decision"
    assert any(e.decision_id == "decision" and e.resource_deltas for e in view.events)
    assert submit(kernel, plan(snapshot, candidate, "stale")).error.code == "stale_version"


def test_external_policy_cannot_underdeclare_resources(tmp_path):
    kernel, actor, snapshot, candidate = fixture(tmp_path)
    piece = candidate.placement.slices[0]
    forged = piece.model_copy(
        update={"resources": piece.resources.model_copy(update={"cpu_millicores": 0})}
    )
    candidate = candidate.model_copy(update={"placement": m.Placement(slices=(forged,))})
    receipt = submit(kernel, plan(snapshot, candidate))
    assert receipt.error.code == "insufficient_resources"
    assert kernel.decision_snapshot() == snapshot
    assert not kernel.read_snapshot(m.ReadQuery(actor=actor)).decisions


def test_restart_cannot_silently_change_scheduler_mode(tmp_path):
    kernel, _, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="scheduling mode"):
        Kernel(tmp_path / "external.sqlite")
    reopened = Kernel(tmp_path / "external.sqlite", scheduling_mode="external")
    assert reopened.decision_snapshot() == kernel.decision_snapshot()
