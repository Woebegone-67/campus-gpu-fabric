"""CP2 B reads against real command transitions in disposable local databases."""

import time

import pytest
from fastapi.testclient import TestClient

from campus_fabric.api.queries import make_kernel_services, register_queries
from campus_fabric.app import create_app
from campus_fabric.contracts import models as m
from campus_fabric.executors.cpu_reference import CpuReferenceConfig
from campus_fabric.workflows import WorkflowOrchestrator

ACTORS = {
    name: m.AuthenticatedActor(actor_id=name, owner_id=name, role=role)
    for name, role in (("alice", "researcher"), ("bob", "researcher"),
                       ("supplier", "contributor"), ("outsider", "contributor"))
}
ACTORS["admin"] = m.AuthenticatedActor(actor_id="admin", role="administrator")
TOKENS = {"cp2-query-" + name: actor for name, actor in ACTORS.items()}


def headers(name="alice"):
    return {"Authorization": "Bearer cp2-query-" + name,
            **({"X-Owner": name} if name != "admin" else {})}


PATH = "/x/v1/work-intents/cp2-query-work"


def node():
    amount = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                               gpu_count=1, gpu_memory_mib=8192)
    return m.NodeInventory(node_id="cp2-query-node", contributor_owner_id="supplier",
        state="Ready", capacity=amount, available=amount, observed_at=0.,
        devices=(m.GPUDevice(device_id="cp2-query-gpu", physical_gpu_id="cp2-query-gpu",
                             model="reference", memory_mib=8192, state="available"),))


def app_client(tmp_path, *, mode="logical_simulation"):
    app = create_app(data_dir=tmp_path, inventory=(node(),), auth_tokens=TOKENS,
                     execution_mode=mode, simulation_duration_seconds=100)
    register_queries(app, make_kernel_services(app.state.kernel, app.state.identity,
        clock_origin="2026-01-01T00:00:00Z"), replace_existing=True)
    return app, TestClient(app)


def detail(client):
    response = client.get(PATH, headers=headers())
    assert response.status_code == 200, response.text
    return response.json()


def action(client, action_name, *, version=None, owner="alice", **extra):
    for _ in range(4):
        response = client.post(PATH + "/actions", headers=headers(owner), json={
            "action": action_name, "expected_version": version or detail(client)["version"], **extra,
        })
        if version is not None or response.status_code != 409 or response.json().get("error") != "stale_version":
            break
    return response


def submit(client, *, mode="logical_simulation", policy="checkpoint", restarts=2, stages=True):
    registration = m.WorkIntentRegistration(intent_id="cp2-query-work", owner_id="alice",
        execution_mode=mode, settlement_action="keep_lease",
        specification=m.WorkSpecification(allocation=m.AllocationCreateParameters(
            kind="allocation.create", owner_id="alice", preemption_mode=policy,
            checkpoint_timeout_seconds=3, max_restarts=restarts,
            request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                       gpu=m.GPURequest(count=1))),
            job=m.JobSpecification(image="not-executed", command=("not-executed",),
                                   timeout_seconds=250, max_retries=1)))
    response = client.post("/x/v1/work-intents", headers=headers(),
                           json=registration.model_dump(mode="json"))
    assert response.status_code == 202, response.text
    for name, stage in ((("allocate", "allocate-v1"), ("create_job", "job-v1")) if stages else ()):
        response = action(client, name, stage_id=stage)
        assert response.status_code == 200, response.text
    return detail(client)


@pytest.mark.parametrize("policy_name", ["B0", "B1", "B2", "P"])
def test_external_worker_reschedules_pending_resume_without_new_work(tmp_path, monkeypatch, policy_name):
    from campus_fabric.workflows.devhost import create_app as host_app

    monkeypatch.setenv("FABRIC_B_POLICY", policy_name)
    app = host_app(data_dir=tmp_path, inventory=(node(),), auth_tokens=TOKENS,
        scheduling_mode="external", worker_enabled=False, simulation_duration_seconds=100)
    with TestClient(app) as client:
        submit(client, stages=False)
        app.state.workflow_worker.tick()
        app.state.workflow_worker.tick()
        original = detail(client)
        assert original["item"]["state"] == "running"
        aid = original["allocations"][0]["id"]
        assert recall(client).status_code == 201
        assert client.post(f"/v1/allocations/{aid}/checkpoint", headers=headers(),
                           json={"status": "ready"}).status_code == 200
        app.state.workflow_worker.tick()
        assert detail(client)["allocations"][0]["state"] == "Preempted"
        assert action(client, "resume").status_code == 200
        app.state.workflow_worker.tick()
        assert detail(client)["allocations"][0]["state"] == "Pending"
        advance(client, 25)
        app.state.workflow_worker.tick()
        resumed = detail(client)
        assert resumed["item"]["state"] == "running"
        assert resumed["item"]["job_ids"] == original["item"]["job_ids"]
        assert resumed["item"]["allocation_ids"] == original["item"]["allocation_ids"]
        assert resumed["jobs"][0]["attempt"]["generation"] == original["jobs"][0]["attempt"]["generation"] + 1
        assert len(resumed["item"]["stages"]) == len(original["item"]["stages"])


def advance(client, seconds):
    response = client.post("/x/v1/admin/clock", headers=headers("admin"),
                           json={"advance_seconds": seconds})
    assert response.status_code == 200, response.text


def recall(client, owner="supplier"):
    return client.post("/x/v1/contributions/recall", headers=headers(owner), json={
        "gpu_ids": ["cp2-query-gpu"], "notice_seconds": 5, "duration_seconds": 20,
    })


def operations(value):
    return {item["operation"] for item in value["legal_actions"]}


@pytest.mark.parametrize("report", ["ready", "failed", "timeout"])
def test_checkpoint_report_failure_timeout_are_preserved_without_fake_verification(tmp_path, report):
    app, client = app_client(tmp_path)
    original = submit(client)
    allocation_id = original["allocations"][0]["id"]
    assert recall(client).status_code == 201
    active = detail(client)
    assert active["jobs"][0]["state"] == "Checkpointing"
    assert active["allocations"][0]["preemption"]["checkpoint_due"] == 3
    assert {"allocation.checkpoint", "allocation.release", "job.cancel"} <= operations(active)
    if report == "timeout":
        advance(client, 3)
    else:
        response = client.post(f"/v1/allocations/{allocation_id}/checkpoint", headers=headers(),
                               json={"status": report, "artifact_uri": "file:///untrusted.json"})
        assert response.status_code == 200, response.text
    value = detail(client)
    assert value["allocations"][0]["state"] == ("Preempted" if report == "ready" else "Evicted")
    assert "allocation.checkpoint" not in operations(value)
    assert ("allocation.resume" in operations(value)) == (report == "ready")
    for item in (value["allocations"][0], value["jobs"][0]):
        if report == "timeout":
            assert item["checkpoint"] is None
        else:
            assert item["checkpoint"]["report_status"] == report
            assert item["checkpoint"]["verification_status"] == "unverified"
            assert item["checkpoint"]["continuation_status"] == "not_attempted"
    assert value["jobs"][0]["execution_mode"] == "logical_simulation"
    assert client.get(PATH, headers=headers("bob")).status_code == 404
    assert client.get("/v1/jobs", headers=headers("supplier")).json()["items"] == []
    assert len(app.state.kernel.read_snapshot(m.ReadQuery(actor=ACTORS["alice"])).jobs) == 1


def test_resume_waits_replays_without_new_objects_and_budget_is_consumed_only_on_grant(tmp_path):
    app, client = app_client(tmp_path)
    original = submit(client, restarts=1)
    allocation_id = original["allocations"][0]["id"]
    assert recall(client).status_code == 201
    assert client.post(f"/v1/allocations/{allocation_id}/checkpoint", headers=headers(),
                       json={"status": "ready"}).status_code == 200
    saved = detail(client)
    assert action(client, "resume", version=original["version"]).status_code == 409
    assert detail(client)["version"] == saved["version"]
    assert action(client, "resume", owner="bob").status_code == 404
    assert action(client, "resume").status_code == 200
    waiting = detail(client)
    assert waiting["allocations"][0]["state"] == "Pending"
    assert waiting["allocations"][0]["restart_count"] == 0
    assert "allocation.resume" not in operations(waiting)
    assert action(client, "resume").status_code == 200
    workflow = WorkflowOrchestrator(app.state.kernel, app.state.kernel)
    progress = workflow.advance("cp2-query-work", actor=ACTORS["alice"])
    assert progress.state == "waiting" and "replacement capacity" in progress.reason
    advance(client, 25)
    resumed = detail(client)
    assert resumed["item"]["job_ids"] == original["item"]["job_ids"]
    assert resumed["item"]["allocation_ids"] == original["item"]["allocation_ids"]
    assert resumed["jobs"][0]["attempt"]["generation"] == original["jobs"][0]["attempt"]["generation"] + 1
    assert resumed["allocations"][0]["expires_at"] == original["allocations"][0]["expires_at"]
    assert resumed["allocations"][0]["restart_count"] == 1
    assert resumed["jobs"][0]["retry_count"] == 0
    assert recall(client).status_code == 201
    assert client.post(f"/v1/allocations/{allocation_id}/checkpoint", headers=headers(),
                       json={"status": "ready"}).status_code == 200
    exhausted = detail(client)
    assert "allocation.resume" not in operations(exhausted)
    assert {"allocation.release", "job.cancel"} <= operations(exhausted)
    assert action(client, "resume").json()["error"] == "budget_exhausted"
    assert workflow.advance("cp2-query-work", actor=ACTORS["alice"]).state == "blocked"
    assert action(client, "release").status_code == 200
    assert detail(client)["allocations"][0]["state"] == "Released"


def test_contributor_scope_and_cpu_pending_stop_never_claim_released_or_ready(tmp_path):
    app, client = app_client(tmp_path, mode="cpu_reference")
    # Do not enter lifespan: this deliberately exercises a queued CPU dispatch.
    # A stop request is still not confirmation, even before a driver starts it.
    original = submit(client, mode="cpu_reference")
    assert recall(client, "outsider").status_code == 403
    assert client.get("/x/v1/contributions", headers=headers("outsider")).json()["items"] == []
    assert action(client, "release").status_code == 200
    pending = detail(client)
    assert pending["allocations"][0]["pending_terminal"] == "Released"
    assert pending["allocations"][0]["state"] == "PreemptRequested"
    assert pending["allocations"][0]["placement"] == original["allocations"][0]["placement"]
    assert pending["item"]["attempts"][-1]["state"] == "stopping"
    assert pending["jobs"][0]["finished_at"] is None
    assert not {"allocation.resume", "allocation.checkpoint", "job.cancel"} & operations(pending)
    assert client.get("/v1/capacity", headers=headers()).json()["totals"]["available"]["gpu_count"] == 0
    progress = WorkflowOrchestrator(app.state.kernel, app.state.kernel).advance(
        "cp2-query-work", actor=ACTORS["alice"])
    assert progress.state == "waiting" and "stop confirmation" in progress.reason


def test_cpu_host_preserves_real_driver_and_registered_artifact_owner_scope(tmp_path):
    from campus_fabric.workflows.devhost import create_app as host_app

    app = host_app(data_dir=tmp_path, inventory=(node(),), auth_tokens=TOKENS,
        execution_mode="cpu_reference", worker_enabled=False,
        cpu_config=CpuReferenceConfig(total_steps=10000, batch_steps=1000,
                                      batch_pause_seconds=.005))
    with TestClient(app) as client:
        version = client.get("/v1/version", headers=headers()).json()
        assert version["execution_mode"] == "cpu_reference"
        assert version["capabilities"]["real_cpu_execution"] is True
        submit(client, mode="cpu_reference")
        try:
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                value = detail(client)
                if value["item"]["state"] == "completed":
                    break
                time.sleep(.03)
            assert value["item"]["state"] == "completed", value
            assert value["now"] > 0
            job = value["jobs"][0]
            assert job["execution_mode"] == "cpu_reference" and job["finished_at_logical"] is not None
            uri = job["result_uri"]
            assert uri.startswith("fabric-cpu://")
            result = client.get("/x/v1/artifacts", headers=headers(), params={"uri": uri})
            assert result.status_code == 200, result.text
            assert result.headers["Cache-Control"] == "no-store"
            assert result.headers["X-Content-Type-Options"] == "nosniff"
            assert result.json()["state"] == app.state.cpu_driver.executor.reference_result()
            assert "token" not in result.json()
            for actor in ("bob", "supplier", "admin"):
                assert client.get("/x/v1/artifacts", headers=headers(actor),
                                  params={"uri": uri}).status_code == 404
            for invalid in ("file:///etc/passwd", "https://example.com/private", uri + "../"):
                assert client.get("/x/v1/artifacts", headers=headers(),
                                  params={"uri": invalid}).status_code == 404
        finally:
            # This test owns only this temporary instance. Let A confirm cleanup.
            response = action(client, "release")
            assert response.status_code in {200, 409}
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and detail(client)["allocations"][0]["state"] != "Released":
                time.sleep(.03)
            assert detail(client)["allocations"][0]["state"] == "Released"
