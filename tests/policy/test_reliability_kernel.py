"""Opt-in placement through A's real SQLite authority; no physical GPU claims."""
from __future__ import annotations

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.policies import make_policy
from campus_fabric.policies.reliability import ReliabilityPolicy
from campus_fabric.policies.renewal import RenewalReliabilityPolicy
from campus_fabric.workflows import WorkflowOrchestrator
from tests.policy.test_baselines import device, node, selected
from tests.policy.test_kernel_integration import (
    OWNER,
    advance_clock,
    commit_plan,
    read,
    registration,
)


def enrich(snapshot):
    """Research adapter; profiles are explicit, not inferred from lease TTL."""
    demands = []
    for demand in snapshot.demands:
        short = demand.intent_id == "work-a"
        facts = tuple(m.ObservedDatum(
            name=name, value=value, source="declared_profile", source_id=demand.demand_id,
            observed_at=999., available_at=999.,
        ) for name, value in (("estimated_compute_seconds", 16. if short else 110.),
                              ("checkpoint_seconds", .1 if short else 8.)))
        facts += (m.ObservedDatum(
            name="remaining_recoveries", value=2, source="public_metadata",
            source_id=demand.demand_id, observed_at=999., available_at=999.,
        ), m.ObservedDatum(
            name="execution_window_seconds", value=900, source="public_metadata",
            source_id=demand.demand_id, observed_at=999., available_at=999.,
        ),)
        demands.append(demand.model_copy(update={"known_data": facts}))
    history = tuple(m.ObservedDatum(
        name="availability_window_seconds", value=duration, source="history",
        source_id=ident, observed_at=float(when), available_at=float(when),
    ) for ident, duration in (("gpu-a", 200.), ("gpu-b", 10.))
        for when in (200, 400, 600, 800))
    metadata = tuple(m.ObservedDatum(
        name=name, value=value, source="declared_profile", source_id=ident,
        observed_at=999., available_at=999.,
    ) for ident in ("gpu-a", "gpu-b")
        for name, value in (("notice_seconds", 12.), ("owner_use_seconds", 10.)))
    return m.DecisionSnapshot.model_validate({
        **snapshot.model_dump(), "demands": tuple(demands), "history": history, "metadata": metadata,
    })


def setup_kernel(tmp_path):
    kernel = Kernel(tmp_path / "matching.sqlite", scheduling_mode="external",
                    inventory=(node("node-a", (device("gpu-a"),)),
                               node("node-b", (device("gpu-b"),))))
    assert advance_clock(kernel, 1000).outcome == "committed"
    for ident in ("work-a", "work-b"):
        work = registration(intent_id=ident, settlement="release_on_success")
        spec = work.specification.model_copy(update={
            "allocation": work.specification.allocation.model_copy(update={"ttl_seconds": 900}),
            "job": work.specification.job.model_copy(update={"timeout_seconds": 900}),
        })
        result = WorkflowOrchestrator(kernel, kernel).start(
            work.model_copy(update={"specification": spec}),
            kernel.decision_snapshot().version, actor=OWNER, request_id="register-request",
        )
        assert result.state == "waiting"
    return kernel


@pytest.mark.parametrize("policy_type", [ReliabilityPolicy, RenewalReliabilityPolicy])
def test_matching_commits_sequentially_without_reservations_then_settles(tmp_path, policy_type):
    kernel = setup_kernel(tmp_path)
    snapshot = enrich(kernel.decision_snapshot())
    base = selected(make_policy("B2").plan(snapshot))
    plan = policy_type().plan(snapshot)
    choice = selected(plan)
    assert "inactive" not in plan.rationale
    assert choice.demand_id == base.demand_id  # unchanged first admission
    assert choice.placement.slices[0].node_id == "node-b"
    assert commit_plan(kernel, plan).outcome == "committed"
    assert sum(a.state == "Allocated" for a in read(kernel).allocations) == 1
    assert sum(a.state == "Pending" for a in read(kernel).allocations) == 1
    assert WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=OWNER).state == "running"

    second = policy_type().plan(enrich(kernel.decision_snapshot()))
    assert selected(second).placement.slices[0].node_id == "node-a"
    assert commit_plan(kernel, second).outcome == "committed"
    assert WorkflowOrchestrator(kernel, kernel).advance("work-b", actor=OWNER).state == "running"
    assert len({a.placement.slices[0].device_ids[0] for a in read(kernel).allocations}) == 2
    assert advance_clock(kernel, 10).outcome == "committed"
    for name in ("work-a", "work-b"):
        result = WorkflowOrchestrator(kernel, kernel).advance(name, actor=OWNER)
        assert result.intent.state == "completed"
    assert all(a.state == "Released" for a in read(kernel).allocations)


@pytest.mark.parametrize("policy_type", [ReliabilityPolicy, RenewalReliabilityPolicy])
def test_profiles_cannot_bypass_version_or_contributor_recall(tmp_path, policy_type):
    kernel = setup_kernel(tmp_path)
    policy = policy_type()
    stale = policy.plan(enrich(kernel.decision_snapshot()))
    assert advance_clock(kernel, 1).outcome == "committed"
    result = commit_plan(kernel, stale)
    assert result.outcome == "rejected" and result.error.code == "stale_version"
    plan = policy.plan(enrich(kernel.decision_snapshot()))
    assert commit_plan(kernel, plan).outcome == "committed"
    assert WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=OWNER).state == "running"
    contributor = m.AuthenticatedActor(actor_id="contributor-a", owner_id="contributor-a", role="contributor")
    notice = kernel.commit(m.CommandEnvelope(
        request_id="owner-recall", actor=contributor, operation="contribution.recall",
        parameters=m.RecallParameters(kind="contribution.recall", device_ids=("gpu-b",),
                                      notice_seconds=1, duration_seconds=20),
    ))
    assert notice.outcome == "committed", notice.error
    assert advance_clock(kernel, 1).outcome == "committed"
    lease = next(a for a in read(kernel).allocations if a.allocation_id == selected(plan).demand_id)
    assert lease.state == "Preempted"
    assert lease.placement is None
    # Physical ownership stays authoritative even when the research profile said notice=12.
    assert kernel.decision_snapshot().notices[0].deadline == 1002
    assert all(c.placement is None or all("gpu-b" not in s.device_ids for s in c.placement.slices)
               for c in policy.plan(enrich(kernel.decision_snapshot())).candidates if c.feasibility == "legal")


@pytest.mark.parametrize("policy_type", [ReliabilityPolicy, RenewalReliabilityPolicy])
def test_missing_live_profile_keeps_b2_choice(tmp_path, policy_type):
    kernel = setup_kernel(tmp_path)
    snapshot = kernel.decision_snapshot()
    base, extended = make_policy("B2").plan(snapshot), policy_type().plan(snapshot)
    assert extended.selected_candidate_id == base.selected_candidate_id
    assert "fallback" in extended.rationale
    assert commit_plan(kernel, extended).outcome == "committed"
