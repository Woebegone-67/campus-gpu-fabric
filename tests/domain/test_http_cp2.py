"""CP2 HTTP effects over real, isolated persistent kernels; no live demo ports."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from campus_fabric.app import create_app
from campus_fabric.contracts.models import NodeInventory

ALICE = {"X-Owner": "alice"}
TEST = {"X-Test-Key": "isolated-test"}


@pytest.fixture
def node():
    return NodeInventory.model_validate_json(json.dumps({
        "node_id": "node-1", "contributor_owner_id": "supplier", "state": "Ready",
        "capacity": {"cpu_millicores": 8000, "memory_mib": 16000,
                     "ephemeral_storage_mib": 20000, "gpu_count": 1, "gpu_memory_mib": 8192},
        "available": {"cpu_millicores": 8000, "memory_mib": 16000,
                      "ephemeral_storage_mib": 20000, "gpu_count": 1, "gpu_memory_mib": 8192},
        "observed_at": 0,
        "devices": [{"device_id": "gpu-1", "physical_gpu_id": "gpu-1", "model": "A10",
                     "memory_mib": 8192, "state": "available"}],
    }))


@pytest.fixture
def client(tmp_path, node):
    with TestClient(create_app(data_dir=tmp_path, inventory=(node,),
                              test_key=TEST["X-Test-Key"],
                              simulation_duration_seconds=100)) as value:
        yield value


def allocate(client, *, headers=None, **patch):
    body = {"owner": "alice", "request": {"cpu_millicores": 1000, "memory_mib": 1024,
            "gpu": {"count": 1, "model": "A10", "memory_mib": 4096}}, "ttl_seconds": 300}
    return client.post("/v1/allocations", headers=headers or ALICE, json={**body, **patch})


def configure(client, **patch):
    body = {"operation": "POST /v1/allocations", "mode": "503"}
    return client.post("/_test/fault", headers=TEST, json={**body, **patch})


def clock(client, seconds):
    response = client.post("/_test/clock", headers=TEST, json={"advance_seconds": seconds})
    assert response.status_code == 200, response.text


def demand(client, **patch):
    return client.post("/_test/contributor-demand", headers=TEST, json={
        "gpu_ids": ["gpu-1"], "notice_seconds": 5, "duration_seconds": 10, **patch})


@pytest.mark.parametrize("path,body", [
    ("/_test/fault", {"operation": "GET /v1/capacity", "mode": "503"}),
    ("/_test/node-state", {"node_id": "node-1", "state": "NotReady"}),
    ("/_test/contributor-demand", {"gpu_ids": ["gpu-1"], "notice_seconds": 1,
                                  "duration_seconds": 1}),
])
def test_test_controls_do_not_accept_owner_role_or_bearer(path, body, client):
    for headers in ({}, ALICE, {"X-Role": "administrator", **ALICE},
                    {"Authorization": "Bearer isolated-test", **ALICE}):
        assert client.post(path, headers=headers, json=body).status_code == 403
    assert client.get("/v1/capacity", headers=ALICE).json()["nodes"][0]["state"] == "Ready"


@pytest.mark.parametrize("mode", ["429", "500", "503", 429, 500, 503])
def test_error_fault_once_has_no_allocation_or_idempotency_effect(client, mode):
    response = configure(client, mode=mode)
    assert response.status_code == 204 and response.content == b""
    headers = {**ALICE, "Idempotency-Key": "fault-retry"}
    faulted = allocate(client, headers=headers)
    assert faulted.status_code == int(mode)
    assert faulted.headers["X-Request-Id"] == faulted.json()["request_id"]
    assert client.get("/v1/allocations", headers=ALICE).json()["allocations"] == []
    success = allocate(client, headers=headers)
    assert success.status_code == 201
    replay = allocate(client, headers=headers)
    assert replay.status_code == 201 and replay.json() == success.json()
    assert len(client.get("/v1/allocations", headers=ALICE).json()["allocations"]) == 1


def test_fault_matches_template_method_and_authenticated_attempt(client):
    aid = allocate(client).json()["id"]
    target = "POST /v1/allocations/{allocation_id}/renew"
    assert configure(client, operation=target, count=1).status_code == 204
    # GET on the same resource, a different operation, and failed auth leave budget intact.
    assert client.get(f"/v1/allocations/{aid}", headers=ALICE).status_code == 200
    assert client.post(f"/v1/allocations/{aid}/renew").status_code == 401
    assert client.post(f"/v1/allocations/{aid}/renew", headers=ALICE).status_code == 503
    assert client.post(f"/v1/allocations/{aid}/renew", headers=ALICE).status_code == 200


@pytest.mark.parametrize("patch", [
    {"operation": "POST /_test/reset"}, {"operation": "POST /x/v1/contributions"},
    {"operation": "POST /v1/allocations/real-id/renew"}, {"operation": "allocation_create"},
    {"operation": []}, {"mode": True}, {"mode": "502"},
    {"count": -1}, {"count": 21}, {"count": True}, {"count": "1"}, {"count": None},
    {"delay_ms": -1}, {"delay_ms": 30001}, {"delay_ms": True}, {"delay_ms": None}, {"extra": 1},
])
def test_fault_bounds_and_targets_are_strict(client, patch):
    assert configure(client, **patch).status_code == 400
    assert allocate(client).status_code == 201


def test_fault_configuration_survives_restart_and_reset_clears_it(tmp_path, node):
    options = {"data_dir": tmp_path, "inventory": (node,), "test_key": TEST["X-Test-Key"]}
    with TestClient(create_app(**options)) as client:
        assert configure(client, operation="GET /v1/capacity", count=2).status_code == 204
        assert client.get("/v1/capacity", headers=ALICE).status_code == 503
    with TestClient(create_app(**options)) as client:
        assert client.get("/v1/capacity", headers=ALICE).status_code == 503
        assert client.get("/v1/capacity", headers=ALICE).status_code == 200
        assert configure(client, operation="GET /v1/capacity", count=2).status_code == 204
        assert client.post("/_test/reset", headers=TEST).status_code == 204
        result = client.get("/v1/capacity", headers=ALICE)
        assert result.status_code == 200 and result.json()["nodes"] == []


def test_fault_count_is_atomic_across_concurrent_http_requests(client):
    assert configure(client, operation="GET /v1/capacity", count=7).status_code == 204
    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(lambda _: client.get("/v1/capacity", headers=ALICE).status_code,
                              range(32)))
    assert codes.count(503) == 7
    assert codes.count(200) == 25


@pytest.mark.parametrize("disable", [{"mode": "none"}, {"count": 0}])
def test_fault_can_be_disabled_without_consuming_other_operations(client, disable):
    assert configure(client, count=20).status_code == 204
    assert configure(client, **disable).status_code == 204
    assert allocate(client).status_code == 201


def test_latency_is_wall_clock_only_and_command_executes_once(client):
    before = client.get("/v1/capacity", headers=ALICE).json()["logical_time"]
    assert configure(client, mode="latency", delay_ms=70).status_code == 204
    started = time.monotonic()
    response = allocate(client)
    assert time.monotonic() - started >= 0.06
    assert response.status_code == 201
    after = client.get("/v1/capacity", headers=ALICE).json()["logical_time"]
    assert before == after
    assert len(client.get("/v1/allocations", headers=ALICE).json()["allocations"]) == 1


def test_checkpoint_owner_scope_and_no_fabricated_artifact(client):
    aid = allocate(client, preemption_policy={"mode": "checkpoint"}).json()["id"]
    path = f"/v1/allocations/{aid}/checkpoint"
    assert client.post(path, headers=ALICE, json={"status": "ready"}).status_code == 409
    assert demand(client).status_code == 201
    assert client.post(path, headers={"X-Owner": "bob"},
                       json={"status": "ready"}).status_code in {403, 404, 409}
    assert client.get(f"/v1/allocations/{aid}", headers=ALICE).json()["state"] == "PreemptRequested"
    assert client.post(path, headers=ALICE, json={"status": "ready"}).status_code == 200
    assert client.get(f"/v1/allocations/{aid}", headers=ALICE).json()["state"] == "Preempted"


def test_resume_waits_then_replaces_without_new_job_identity(client):
    aid = allocate(client, preemption_policy={"mode": "restartable"}).json()["id"]
    job = client.post("/v1/jobs", headers=ALICE,
                      json={"allocation_id": aid, "image": "example", "command": ["work"]})
    assert job.status_code == 201
    jid = job.json()["id"]
    assert demand(client).status_code == 201
    clock(client, 5)
    assert client.get(f"/v1/allocations/{aid}", headers=ALICE).json()["state"] == "Preempted"
    assert client.post(f"/v1/allocations/{aid}/resume", headers=ALICE).status_code == 202
    clock(client, 10)
    assert client.get(f"/v1/allocations/{aid}", headers=ALICE).json()["state"] == "Running"
    assert client.get(f"/v1/jobs/{jid}", headers=ALICE).json()["state"] == "Running"
    assert len(client.get("/v1/jobs", headers=ALICE).json()["jobs"]) == 1


def test_new_control_payloads_reject_invalid_input_atomically(client):
    for patch in ({"gpu_ids": ["gpu-1", "missing"]}, {"gpu_ids": ["gpu-1", "gpu-1"]},
                  {"notice_seconds": True}, {"duration_seconds": 0}, {"unexpected": 1}):
        assert demand(client, **patch).status_code in {400, 404}
    assert allocate(client).status_code == 201
    aid = client.get("/v1/allocations", headers=ALICE).json()["allocations"][0]["id"]
    for suffix, body in (("checkpoint", {}), ("checkpoint", {"status": "ready", "extra": 0}),
                         ("resume", {"extra": 0}), ("checkpoint", {"status": "ready", "artifact_uri": None})):
        assert client.post(f"/v1/allocations/{aid}/{suffix}", headers=ALICE,
                           json=body).status_code == 400


def test_draining_preserves_current_lease_and_blocks_new_grants(client):
    aid = allocate(client).json()["id"]
    assert client.post("/_test/node-state", headers=TEST,
                       json={"node_id": "node-1", "state": "Draining"}).status_code == 200
    assert client.get(f"/v1/allocations/{aid}", headers=ALICE).json()["state"] == "Allocated"
    assert client.delete(f"/v1/allocations/{aid}", headers=ALICE).status_code == 202
    queued = allocate(client)
    assert queued.status_code == 202
    assert client.post("/_test/node-state", headers=TEST,
                       json={"node_id": "node-1", "state": "Ready"}).status_code == 200
    assert client.get(f"/v1/allocations/{queued.json()['id']}", headers=ALICE).json()["state"] == "Allocated"


def test_contributor_recall_uses_owner_authority_without_test_key(tmp_path, node):
    tokens = {
        "supplier-token": {"actor_id": "supplier", "role": "contributor", "owner_id": "supplier"},
        "other-token": {"actor_id": "other", "role": "contributor", "owner_id": "other"},
    }
    payload = {"gpu_ids": ["gpu-1"], "notice_seconds": 1, "duration_seconds": 10}
    with TestClient(create_app(data_dir=tmp_path, inventory=(node,), auth_tokens=tokens)) as client:
        wrong = {"Authorization": "Bearer other-token", "X-Owner": "other"}
        owner = {"Authorization": "Bearer supplier-token", "X-Owner": "supplier"}
        assert client.post("/x/v1/contributions/recall", headers=wrong,
                           json=payload).status_code in {403, 404}
        assert client.post("/x/v1/contributions/recall", headers=owner,
                           json=payload).status_code == 201
        assert client.post("/_test/contributor-demand", headers=owner,
                           json=payload).status_code == 403
