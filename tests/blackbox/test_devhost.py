"""Joint B host integration on disposable CP1 databases, not acceptance receipts."""

import time

import pytest
from fastapi.testclient import TestClient

from campus_fabric.contracts import models as m
from campus_fabric.workflows.devhost import create_app

TOKENS = {
    "isolated-alice": m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="researcher"),
    "isolated-bob": m.AuthenticatedActor(actor_id="bob", owner_id="bob", role="researcher"),
    "isolated-admin": m.AuthenticatedActor(actor_id="admin", role="administrator"),
}
ALICE = {"Authorization": "Bearer isolated-alice", "X-Owner": "alice"}
BOB = {"Authorization": "Bearer isolated-bob", "X-Owner": "bob"}
ADMIN = {"Authorization": "Bearer isolated-admin"}
PATH = "/x/v1/work-intents/isolated-host-intent"


def make_app(tmp_path, *, worker_enabled=False, scheduling_mode="builtin"):
    amounts = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8000)
    node = m.NodeInventory(node_id="isolated-host-node", contributor_owner_id="supplier",
        state="Ready", capacity=amounts, available=amounts, observed_at=0.0)
    return create_app(data_dir=tmp_path, inventory=(node,), auth_tokens=TOKENS,
                      worker_enabled=worker_enabled, scheduling_mode=scheduling_mode)


def submit(client):
    body = m.WorkIntentRegistration(intent_id="isolated-host-intent", owner_id="alice",
        execution_mode="logical_simulation", settlement_action="release_on_success",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id="alice",
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1000,
                                           gpu=m.GPURequest(count=0))),
            job=m.JobSpecification(image="sim", command=("not-physically-executed",)),
        ))
    result = client.post("/x/v1/work-intents", headers=ALICE, json=body.model_dump(mode="json"))
    assert result.status_code == 202, result.text
    return body


def test_host_workflow_clock_settlement_authorization_and_honest_acceptance(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        submit(client)
        app.state.workflow_worker.tick()
        running = client.get(PATH, headers=ALICE).json()
        assert running["item"]["state"] == "running"
        assert len(running["jobs"]) == len(running["allocations"]) == 1
        assert running["jobs"][0]["state"] == "Running"
        assert client.get(PATH, headers=BOB).status_code == 404
        assert client.get("/v1/jobs", headers=BOB).json()["items"] == []
        assert client.post("/x/v1/admin/clock", headers=ALICE,
                           json={"advance_seconds": 10}).status_code == 403
        assert client.post("/x/v1/admin/clock", headers=ADMIN,
                           json={"advance_seconds": 10}).status_code == 200
        completed = client.get(PATH, headers=ALICE).json()
        assert completed["item"]["state"] == "completed"
        assert completed["jobs"][0]["state"] == "Succeeded"
        assert completed["allocations"][0]["state"] == "Released"
        aid = completed["allocations"][0]["id"]
        assert client.post(f"/v1/allocations/{aid}/resume", headers=ALICE).status_code == 409
        acceptance = client.get("/x/v1/acceptance", headers=ADMIN)
        assert acceptance.status_code == 200, acceptance.text
        assert acceptance.json()["status"] == "NOT_RUN"
        assert acceptance.json()["items"]
        assert all(check["status"] == "NOT_RUN" for check in acceptance.json()["items"])
        assert acceptance.json()["build_matches"] is None


@pytest.mark.parametrize("policy_name", ["B0", "B2"])
def test_external_policy_commits_real_decision_and_worker_binds_job(tmp_path, monkeypatch, policy_name):
    monkeypatch.setenv("FABRIC_B_POLICY", policy_name)
    app = make_app(tmp_path, scheduling_mode="external")
    with TestClient(app) as client:
        submit(client)
        app.state.workflow_worker.tick()
        granted = client.get(PATH, headers=ALICE).json()
        assert granted["allocations"][0]["state"] == "Allocated"
        assert granted["jobs"] == []
        app.state.workflow_worker.tick()
        running = client.get(PATH, headers=ALICE).json()
        assert running["item"]["state"] == "running" and len(running["jobs"]) == 1
        decisions = {event["decision_id"] for event in client.get("/v1/events", headers=ALICE).json()["items"]
                     if event["decision_id"] is not None}
        assert decisions
        for decision_id in decisions:
            response = client.get(f"/x/v1/decisions/{decision_id}", headers=ALICE)
            assert response.status_code == 200, response.text
            assert response.json()["commit_status"] == "committed"
            assert response.json()["actual_events"]


def test_host_restart_recovers_durable_bindings_without_new_objects(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        submit(client)
        app.state.workflow_worker.tick()
        original = client.get(PATH, headers=ALICE).json()
    restarted = make_app(tmp_path)
    with TestClient(restarted) as client:
        restarted.state.workflow_worker.tick()
        recovered = client.get(PATH, headers=ALICE).json()
        assert recovered["item"]["allocation_ids"] == original["item"]["allocation_ids"]
        assert recovered["item"]["job_ids"] == original["item"]["job_ids"]
        assert recovered["item"]["stages"] == original["item"]["stages"]
        assert recovered["version"] == original["version"]
        assert client.get("/v1/metrics", headers=ALICE).json()["counts"] == {
            "allocations": 1, "jobs": 1, "work_intents": 1,
        }


def test_enabled_lifespan_worker_advances_actual_persisted_intent(tmp_path):
    app = make_app(tmp_path, worker_enabled=True)
    with TestClient(app) as client:
        submit(client)
        deadline = time.monotonic() + 5
        while True:
            state = client.get(PATH, headers=ALICE).json()
            if state["item"]["state"] == "running" or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        assert state["item"]["state"] == "running", state
        assert len(state["jobs"]) == 1
        assert state["now"] == 0.0, "Background workflow must not secretly advance the clock"


def test_worker_discovery_does_not_cross_reset_into_reused_intent(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        submit(client)
        worker = app.state.workflow_worker
        original_advance = worker.workflow.advance

        def reset_between_discovery_and_advance(*args, **kwargs):
            app.state.kernel.commit(m.CommandEnvelope(
                request_id="isolated-reset-race", actor=m.AuthenticatedActor(
                    actor_id="isolated-reset-driver", role="test_driver"),
                operation="test.reset", parameters=m.ResetParameters(kind="test.reset"),
            ))
            submit(client)  # Same intent identifier in a different authoritative epoch.
            return original_advance(*args, **kwargs)

        monkeypatch.setattr(worker.workflow, "advance", reset_between_discovery_and_advance)
        worker.tick()
        current = client.get(PATH, headers=ALICE).json()
        assert current["version"]["epoch"] == 1
        assert current["item"]["state"] == "pending"
        assert current["item"]["allocation_ids"] == current["item"]["job_ids"] == []
