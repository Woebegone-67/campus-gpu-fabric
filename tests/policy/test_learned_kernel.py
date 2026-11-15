"""Opt-in neural plans through the authoritative kernel; synthetic metadata only.

The test adapter supplies a declared newly-opened-window feature fixture. This
checks executable allocation/authority, not an installed live feature producer
or the accuracy of the hand-authored neural test weights.
"""
from __future__ import annotations

from campus_fabric.contracts import models as m
from campus_fabric.policies.availability_model import FEATURE_NAMES
from campus_fabric.policies.baselines import make_policy
from campus_fabric.policies.candidate import CandidatePolicy
from campus_fabric.policies.learned import LearnedAvailabilityPolicy
from campus_fabric.workflows import WorkflowOrchestrator
from tests.policy.test_baselines import selected
from tests.policy.test_kernel_integration import OWNER, advance_clock, commit_plan, read
from tests.policy.test_learned import artifact
from tests.policy.test_reliability import datum
from tests.policy.test_reliability_kernel import enrich, setup_kernel


def learned_snapshot(snapshot):
    enriched = enrich(snapshot)
    metadata = list(enriched.metadata)
    for physical, phase in (("gpu-a", 1.), ("gpu-b", -1.)):
        values = (phase, 0., 0., 0., .5, .1, .5, .5)
        metadata.extend(datum(name, value, physical, when=snapshot.now, source="history")
                        for name, value in zip(FEATURE_NAMES, values, strict=True))
        metadata.append(datum("availability_window_age_seconds", 0., physical,
                              when=snapshot.now, source="observed_event"))
    return m.DecisionSnapshot.model_validate({**enriched.model_dump(), "metadata": tuple(metadata)})


def test_p_factory_neural_assignment_commits_real_lease_without_reservation(tmp_path, monkeypatch):
    kernel = setup_kernel(tmp_path)
    path, sha = artifact(tmp_path)
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_PATH", str(path))
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_SHA256", sha)
    policy = make_policy("P")
    assert isinstance(policy, LearnedAvailabilityPolicy)
    snapshot = learned_snapshot(kernel.decision_snapshot())
    base = selected(make_policy("B2").plan(snapshot))
    plan = policy.plan(snapshot)
    assert "inactive" not in plan.rationale
    assert selected(plan).demand_id == base.demand_id
    assert selected(plan).placement.slices[0].node_id == "node-b"
    assert commit_plan(kernel, plan).outcome == "committed"
    assert sum(lease.state == "Allocated" for lease in read(kernel).allocations) == 1
    assert sum(lease.state == "Pending" for lease in read(kernel).allocations) == 1
    assert WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=OWNER).state == "running"
    second = policy.plan(learned_snapshot(kernel.decision_snapshot()))
    assert selected(second).placement.slices[0].node_id == "node-a"
    assert commit_plan(kernel, second).outcome == "committed"
    assert WorkflowOrchestrator(kernel, kernel).advance("work-b", actor=OWNER).state == "running"
    assert len({lease.placement.slices[0].device_ids[0] for lease in read(kernel).allocations}) == 2
    assert advance_clock(kernel, 10).outcome == "committed"
    for ident in ("work-a", "work-b"):
        assert WorkflowOrchestrator(kernel, kernel).advance(ident, actor=OWNER).intent.state == "completed"
    assert all(lease.state == "Released" for lease in read(kernel).allocations)


def test_prediction_never_overrides_stale_version_or_contributor_return_deadline(tmp_path):
    kernel = setup_kernel(tmp_path)
    path, sha = artifact(tmp_path)
    policy = LearnedAvailabilityPolicy(path, expected_sha256=sha)
    stale = policy.plan(learned_snapshot(kernel.decision_snapshot()))
    assert advance_clock(kernel, 1).outcome == "committed"
    receipt = commit_plan(kernel, stale)
    assert receipt.outcome == "rejected" and receipt.error.code == "stale_version"
    plan = policy.plan(learned_snapshot(kernel.decision_snapshot()))
    assert commit_plan(kernel, plan).outcome == "committed"
    assert WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=OWNER).state == "running"
    contributor = m.AuthenticatedActor(actor_id="contributor-a", owner_id="contributor-a", role="contributor")
    recall = kernel.commit(m.CommandEnvelope(
        request_id="learned-owner-recall", actor=contributor, operation="contribution.recall",
        parameters=m.RecallParameters(kind="contribution.recall", device_ids=("gpu-b",),
                                      notice_seconds=1, duration_seconds=20),
    ))
    assert recall.outcome == "committed", recall.error
    assert advance_clock(kernel, 1).outcome == "committed"
    lease = next(lease for lease in read(kernel).allocations if lease.allocation_id == selected(plan).demand_id)
    assert lease.state == "Preempted" and lease.placement is None
    assert kernel.decision_snapshot().notices[0].deadline == 1002
    after = policy.plan(learned_snapshot(kernel.decision_snapshot()))
    assert all(candidate.placement is None or all("gpu-b" not in piece.device_ids
               for piece in candidate.placement.slices) for candidate in after.candidates
               if candidate.feasibility == "legal")


def test_live_missing_metadata_falls_back_and_disabling_env_restores_previous_p(tmp_path, monkeypatch):
    kernel = setup_kernel(tmp_path)
    path, sha = artifact(tmp_path)
    policy = LearnedAvailabilityPolicy(path, expected_sha256=sha)
    snapshot = kernel.decision_snapshot()
    plan = policy.plan(snapshot)
    assert "history/B2 fallback" in plan.rationale
    assert plan.selected_candidate_id == make_policy("B2").plan(snapshot).selected_candidate_id
    assert commit_plan(kernel, plan).outcome == "committed"
    monkeypatch.delenv("FABRIC_AVAILABILITY_MODEL_PATH", raising=False)
    monkeypatch.delenv("FABRIC_AVAILABILITY_MODEL_SHA256", raising=False)
    assert isinstance(make_policy("P"), CandidatePolicy)
