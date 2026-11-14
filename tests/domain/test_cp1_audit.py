"""Counterexamples from the independent CP1 closeout review."""

from fastapi.testclient import TestClient

from campus_fabric.app import create_app
from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.domain.placement import choose_placement

OWNER = m.AuthenticatedActor(actor_id="owner", owner_id="owner", role="researcher")
SYSTEM = m.AuthenticatedActor(actor_id="policy", role="system")
TEST = m.AuthenticatedActor(actor_id="test", role="test_driver")


def setup(tmp_path, *, external=False):
    resources = m.ResourceAmounts(cpu_millicores=2000, memory_mib=4000)
    node = m.NodeInventory(node_id="n", contributor_owner_id="supply", state="Ready",
                           capacity=resources, available=resources, observed_at=0.0)
    kernel = Kernel(tmp_path / "audit.sqlite", inventory=(node,),
                    scheduling_mode="external" if external else "builtin")
    allocation = m.AllocationCreateParameters(kind="allocation.create", owner_id="owner",
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1000, gpu=m.GPURequest(count=0)))
    return kernel, allocation


def commit(kernel, params, actor=OWNER, *, version=None):
    return kernel.commit(m.CommandEnvelope(request_id="audit-command", actor=actor,
        operation=params.kind, parameters=params, expected_version=version))


def register(kernel, allocation):
    spec = m.WorkSpecification(allocation=allocation,
                              job=m.JobSpecification(image="sim", command=("work",)))
    registered = kernel.register_intent(m.WorkIntentRegistration(
        intent_id="intent", owner_id="owner", execution_mode="logical_simulation",
        settlement_action="release_on_success", specification=spec),
        kernel.decision_snapshot().version, actor=OWNER, request_id="register")
    assert registered.outcome == "committed"
    allocated = kernel.create_allocation_and_bind("intent", "allocate", registered.version,
                                                actor=OWNER, request_id="allocate")
    assert allocated.outcome == "committed"
    return allocated


def test_repeating_allocation_stage_with_new_id_cannot_regress_running_intent(tmp_path):
    kernel, allocation = setup(tmp_path)
    allocated = register(kernel, allocation)
    running = kernel.create_job_and_bind("intent", "job", allocated.version,
                                        actor=OWNER, request_id="job")
    assert running.outcome == "committed"
    kernel.create_allocation_and_bind("intent", "another-allocation-stage", running.version,
                                     actor=OWNER, request_id="second-allocation")
    view = kernel.read_snapshot(m.ReadQuery(actor=OWNER))
    assert view.jobs[0].state == "Running"
    assert view.intents[0].state == "running"


def test_public_job_cannot_bypass_workflow_binding_and_settlement(tmp_path):
    kernel, allocation = setup(tmp_path)
    allocated = register(kernel, allocation)
    aid = next(e.entity_id for e in allocated.objects if e.kind == "allocation")
    result = commit(kernel, m.JobCreateParameters(kind="job.create", allocation_id=aid,
                                                 image="unrelated", command=("other",)))
    assert result.outcome == "rejected"
    assert not kernel.read_snapshot(m.ReadQuery(actor=OWNER)).jobs


def test_committed_response_survives_reset_between_commit_and_response_read(tmp_path):
    kernel, allocation = setup(tmp_path)
    receipt, payload = kernel.commit_with_payload(m.CommandEnvelope(
        request_id="atomic-response", actor=OWNER, operation="allocation.create",
        parameters=allocation))
    assert receipt.outcome == "committed"
    commit(kernel, m.ResetParameters(kind="test.reset"), actor=TEST)
    assert payload["status_code"] == 201
    assert payload["body"]["owner"] == "owner"
    assert payload["body"]["state"] == "Allocated"
    assert not kernel.read_snapshot(m.ReadQuery(actor=OWNER)).allocations


def test_http_response_uses_atomic_snapshot_even_if_reset_wins_before_send(tmp_path, monkeypatch):
    source, _ = setup(tmp_path)
    app = create_app(data_dir=tmp_path / "http", inventory=source.decision_snapshot().inventory)
    kernel = app.state.kernel
    original = kernel.commit_with_payload

    def reset_after_commit(command):
        result = original(command)
        original(m.CommandEnvelope(request_id="interleaved-reset", actor=TEST,
            operation="test.reset", parameters=m.ResetParameters(kind="test.reset")))
        return result

    monkeypatch.setattr(kernel, "commit_with_payload", reset_after_commit)
    with TestClient(app) as client:
        response = client.post("/v1/allocations", headers={"X-Owner": "owner"}, json={
            "owner": "owner", "request": {
                "cpu_millicores": 1000, "memory_mib": 1000, "gpu": {"count": 0},
            },
        })
        assert response.status_code == 201
        assert response.json()["state"] == "Allocated"
        assert client.get("/v1/allocations", headers={"X-Owner": "owner"}).json()["allocations"] == []


def policy_plan(kernel, allocation, *, action="allocate", preconditions=()):
    receipt = commit(kernel, allocation)
    aid = next(e.entity_id for e in receipt.objects if e.kind == "allocation")
    snap = kernel.decision_snapshot()
    candidate = m.CandidateAction(candidate_id="c", action=action, owner_id="owner",
        demand_id=aid, allocation_id=aid, feasibility="legal", preconditions=preconditions,
        placement=choose_placement(allocation.request, (), snap.inventory))
    artifact = m.VersionedArtifact(name="audit", version="1", sha256="0" * 64)
    plan = m.DecisionPlan(decision_id="d", snapshot_version=snap.version, decision_at=snap.now,
        policy=artifact, model=artifact, candidates=(candidate,), selected_candidate_id="c",
        coverage=m.SearchCoverage(scope="complete", domain_description="one action",
                                  examined_count=1, domain_size=1),
        rationale="Independent audit", tie_break_rule="single candidate")
    return plan, aid


def test_policy_preconditions_are_not_silently_ignored(tmp_path):
    kernel, allocation = setup(tmp_path, external=True)
    plan, aid = policy_plan(kernel, allocation)
    candidate = plan.candidates[0].model_copy(update={"preconditions": (
        m.Precondition(kind="allocation_state", entity=m.EntityRef(kind="allocation", entity_id=aid),
                       expected_value="Released"),)})
    plan = plan.model_copy(update={"candidates": (candidate,)})
    result = commit(kernel, m.DecisionCommitParameters(kind="decision.commit", plan=plan),
                    actor=SYSTEM, version=plan.snapshot_version)
    assert result.outcome == "rejected"
    assert kernel.read_snapshot(m.ReadQuery(actor=OWNER)).allocations[0].state == "Pending"


def test_selected_reject_must_apply_or_return_unsupported(tmp_path):
    kernel, allocation = setup(tmp_path, external=True)
    plan, _ = policy_plan(kernel, allocation, action="reject")
    result = commit(kernel, m.DecisionCommitParameters(kind="decision.commit", plan=plan),
                    actor=SYSTEM, version=plan.snapshot_version)
    state = kernel.read_snapshot(m.ReadQuery(actor=OWNER)).allocations[0].state
    assert result.outcome == "rejected" or state == "Rejected"
