"""Real adapters over the persisted kernel, with isolated temporary state."""

import json

import pytest
from fastapi.testclient import TestClient

from campus_fabric.app import create_app
from campus_fabric.contracts.models import NodeInventory


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
def allocation():
    return {"owner": "alice", "request": {"cpu_millicores": 1000, "memory_mib": 1024,
            "gpu": {"count": 1, "model": "A10", "memory_mib": 4096}}, "ttl_seconds": 30}


@pytest.fixture
def client(tmp_path, node):
    with TestClient(create_app(data_dir=tmp_path, inventory=(node,), test_key="secret-test")) as value:
        yield value


def test_owner_body_and_role_cannot_escalate(client, allocation):
    assert client.post("/v1/allocations", json=allocation).status_code == 401
    assert client.post("/v1/allocations", json=allocation,
                       headers={"X-Owner": "bob", "X-Role": "administrator"}).status_code == 403
    assert client.post("/x/v1/contributions", json={},
                       headers={"X-Owner": "alice", "X-Role": "administrator"}).status_code == 403
    assert client.get("/v1/allocations", headers={"Authorization": "Bearer alice"}).status_code == 401
    assert client.post("/_test/reset", headers={"X-Owner": "alice"}).status_code == 403


def test_bearer_binding_and_no_test_key_as_admin(tmp_path, node, allocation):
    tokens = {"secret-alice": {"actor_id": "actor-a", "role": "researcher", "owner_id": "alice"}}
    with TestClient(create_app(data_dir=tmp_path, inventory=(node,), auth_tokens=tokens,
                              test_key="test-secret")) as client:
        assert client.get("/v1/allocations", headers={"X-Owner": "alice"}).status_code == 401
        assert client.get("/v1/allocations", headers={"Authorization": "Bearer secret-alice",
                          "X-Owner": "bob"}).status_code == 403
        assert client.get("/v1/allocations", headers={"Authorization": "Bearer test-secret",
                          "X-Owner": "alice"}).status_code == 401
        response = client.post("/v1/allocations", json=allocation,
                               headers={"Authorization": "Bearer secret-alice", "X-Owner": "alice"})
        assert response.status_code == 201
        assert response.json()["owner"] == "alice"


def test_allocation_retry_keeps_original_response_after_release(client, allocation):
    headers = {"X-Owner": "alice", "Idempotency-Key": "same"}
    first = client.post("/v1/allocations", json=allocation, headers=headers)
    assert first.status_code == 201
    allocation_id = first.json()["id"]
    assert client.delete(f"/v1/allocations/{allocation_id}", headers=headers).status_code == 202
    second = client.post("/v1/allocations", json=allocation, headers=headers)
    assert second.status_code == first.status_code
    assert second.json() == first.json()
    changed = {**allocation, "ttl_seconds": 60}
    assert client.post("/v1/allocations", json=changed, headers=headers).status_code == 409
    assert len(client.get("/v1/allocations", headers=headers).json()["allocations"]) == 1


def test_normal_completion_keeps_lease_and_expiry_returns_once(client, allocation):
    headers = {"X-Owner": "alice"}
    created = client.post("/v1/allocations", json=allocation, headers=headers)
    aid = created.json()["id"]
    job = client.post("/v1/jobs", headers=headers,
                      json={"allocation_id": aid, "image": "example", "command": ["work"]})
    assert job.status_code == 201
    jid = job.json()["id"]
    assert client.post("/_test/clock", headers={"X-Test-Key": "secret-test"},
                       json={"advance_seconds": 10}).status_code == 200
    assert client.get(f"/v1/jobs/{jid}", headers=headers).json()["state"] == "Succeeded"
    assert client.get(f"/v1/allocations/{aid}", headers=headers).json()["state"] == "Allocated"
    assert client.get("/v1/capacity", headers=headers).json()["totals"]["available"]["gpu_count"] == 0
    client.post("/_test/clock", headers={"X-Test-Key": "secret-test"}, json={"advance_seconds": 20})
    assert client.get(f"/v1/allocations/{aid}", headers=headers).json()["state"] == "Expired"
    assert client.post(f"/v1/allocations/{aid}/renew", headers=headers, json={}).status_code == 409
    assert client.get("/v1/capacity", headers=headers).json()["totals"]["available"]["gpu_count"] == 1


def test_owner_isolation_in_detail_lists_events_metrics(client, allocation):
    alice, bob = {"X-Owner": "alice"}, {"X-Owner": "bob"}
    created = client.post("/v1/allocations", json=allocation, headers=alice)
    aid = created.json()["id"]
    job = client.post("/v1/jobs", headers=alice,
                      json={"allocation_id": aid, "image": "example", "command": ["work"]})
    jid = job.json()["id"]
    assert client.get(f"/v1/allocations/{aid}", headers=bob).status_code == 404
    assert client.get(f"/v1/jobs/{jid}", headers=bob).status_code == 404
    assert client.delete(f"/v1/allocations/{aid}", headers=bob).status_code == 404
    assert client.post(f"/v1/allocations/{aid}/renew", headers=bob, json={}).status_code == 409
    assert client.get("/v1/allocations", headers=bob).json()["allocations"] == []
    assert client.get("/v1/jobs", headers=bob).json()["jobs"] == []
    assert client.get("/v1/events", headers=bob).json()["events"] == []
    assert client.get("/v1/metrics", headers=bob).json()["jobs"] == 0


@pytest.mark.parametrize("patch", [{"priority": True}, {"ttl_seconds": "30"},
                                    {"ttl_seconds": 0}, {"unexpected": 1}])
def test_strict_http_input(client, allocation, patch):
    assert client.post("/v1/allocations", headers={"X-Owner": "alice"},
                       json={**allocation, **patch}).status_code == 400


def test_explicit_configuration_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRIC_MODE", "cpu_reference")
    with pytest.raises(ValueError, match="logical_simulation"):
        create_app(data_dir=tmp_path)
    monkeypatch.setenv("FABRIC_MODE", "logical_simulation")
    with pytest.raises(ValueError, match="Internal roles"):
        create_app(data_dir=tmp_path, auth_tokens={"secret": {"actor_id": "x", "role": "system"}})


def test_bad_json_and_identifier(client):
    assert client.post("/v1/allocations", headers={"X-Owner": "alice"},
                       content=b"not-json").status_code == 400
    assert client.get("/v1/capacity", headers={"X-Owner": "bad owner"}).status_code == 400
    assert client.get("/healthz", headers={"X-Request-Id": "bad id"}).status_code == 400


def test_workflow_stages_retry_and_terminal_settlement(client, allocation):
    headers = {"X-Owner": "alice"}
    registration = {
        "intent_id": "work-1", "owner_id": "alice", "execution_mode": "logical_simulation",
        "settlement_action": "release_on_success",
        "specification": {
            "allocation": {"kind": "allocation.create", "owner_id": "alice",
                           "request": allocation["request"], "ttl_seconds": 30},
            "job": {"image": "example", "command": ["work"]},
        },
    }
    registered = client.post("/x/v1/work-intents", json=registration, headers=headers)
    assert registered.status_code == 202
    version = registered.json()["version"]
    action = {"action": "allocate", "stage_id": "allocate-once", "expected_version": version}
    allocated = client.post("/x/v1/work-intents/work-1/actions", json=action, headers=headers)
    assert allocated.status_code == 200
    retried = client.post("/x/v1/work-intents/work-1/actions", json=action, headers=headers)
    assert retried.status_code == 200
    assert retried.json()["objects"] == allocated.json()["objects"]
    action = {"action": "create_job", "stage_id": "job-once",
              "expected_version": allocated.json()["version"]}
    created = client.post("/x/v1/work-intents/work-1/actions", json=action, headers=headers)
    assert created.status_code == 200
    retried = client.post("/x/v1/work-intents/work-1/actions", json=action, headers=headers)
    assert retried.status_code == 200
    assert retried.json()["objects"] == created.json()["objects"]
    assert len(client.get("/v1/allocations", headers=headers).json()["allocations"]) == 1
    assert len(client.get("/v1/jobs", headers=headers).json()["jobs"]) == 1
    client.post("/_test/clock", headers={"X-Test-Key": "secret-test"}, json={"advance_seconds": 10})
    intent = client.get("/x/v1/work-intents/work-1", headers=headers).json()["intent"]
    assert intent["state"] == "completed"
    assert intent["result"]["kind"] == "success"
    assert client.get("/v1/allocations", headers=headers).json()["allocations"][0]["state"] == "Released"
    assert client.get("/x/v1/work-intents/work-1", headers={"X-Owner": "bob"}).status_code == 404


def test_persistence_and_original_http_receipt(tmp_path, node, allocation):
    headers = {"X-Owner": "alice", "Idempotency-Key": "persisted-key"}
    with TestClient(create_app(data_dir=tmp_path, inventory=(node,))) as client:
        first = client.post("/v1/allocations", json=allocation, headers=headers)
        assert first.status_code == 201
    # Explicit startup inventory is initialization only, not a repeated reset.
    with TestClient(create_app(data_dir=tmp_path, inventory=(node,))) as client:
        replay = client.post("/v1/allocations", json=allocation, headers=headers)
        assert replay.status_code == first.status_code
        assert replay.json() == first.json()
        assert client.get("/v1/capacity", headers=headers).json()["totals"]["available"]["gpu_count"] == 0


def test_contribution_requires_server_granted_identity(tmp_path, node):
    tokens = {"supplier-secret": {"actor_id": "supplier", "role": "contributor", "owner_id": "supplier"}}
    headers = {"X-Owner": "supplier", "Authorization": "Bearer supplier-secret"}
    with TestClient(create_app(data_dir=tmp_path, auth_tokens=tokens)) as client:
        response = client.post("/x/v1/contributions", headers=headers, json=node.model_dump(mode="json"))
        assert response.status_code == 201
        assert client.get("/v1/capacity", headers=headers).json()["totals"]["capacity"]["gpu_count"] == 1
        wrong = node.model_dump(mode="json")
        wrong["contributor_owner_id"] = "another-supplier"
        assert client.post("/x/v1/contributions", headers=headers, json=wrong).status_code == 403


def test_queued_response_replay_does_not_change_after_later_grant(client, allocation):
    alice = {"X-Owner": "alice"}
    blocker = client.post("/v1/allocations", json=allocation, headers=alice).json()["id"]
    headers = {"X-Owner": "bob", "Idempotency-Key": "queued"}
    request = {**allocation, "owner": "bob"}
    pending = client.post("/v1/allocations", json=request, headers=headers)
    assert pending.status_code == 202
    assert pending.json()["state"] == "Pending"
    client.delete(f"/v1/allocations/{blocker}", headers=alice)
    assert client.get(f"/v1/allocations/{pending.json()['id']}", headers=headers).json()["state"] == "Allocated"
    replay = client.post("/v1/allocations", json=request, headers=headers)
    assert replay.status_code == 202
    assert replay.json() == pending.json()


def test_event_pages_do_not_skip_undelivered_and_reset_cursor_is_conflict(client, allocation):
    headers = {"X-Owner": "alice"}
    client.post("/v1/allocations", json=allocation, headers=headers)
    expected = client.get("/v1/events", headers=headers).json()["events"]
    assert len(expected) >= 2
    collected = []
    cursor = None
    while True:
        params = {"limit": 1}
        if cursor:
            params["after"] = cursor
        response = client.get("/v1/events", headers=headers, params=params)
        assert response.status_code == 200
        page = response.json()
        assert len(page["events"]) <= 1
        collected.extend(page["events"])
        cursor = page["next_after"]
        if not page["has_more"]:
            break
    assert collected == expected
    client.post("/_test/reset", headers={"X-Test-Key": "secret-test"})
    assert client.get("/v1/events", headers=headers, params={"after": cursor}).status_code == 409


def test_object_pages_are_revision_bound_and_limits_checked(client, allocation):
    headers = {"X-Owner": "alice"}
    for _ in range(3):
        client.post("/v1/allocations", json=allocation, headers=headers)
    all_rows = client.get("/v1/allocations", headers=headers).json()["allocations"]
    first = client.get("/v1/allocations", headers=headers, params={"limit": 1}).json()
    assert len(first["allocations"]) == 1
    second = client.get("/v1/allocations", headers=headers,
                        params={"limit": 2, "after": first["next_cursor"]}).json()
    assert first["allocations"] + second["allocations"] == all_rows
    assert second["next_cursor"] is None
    client.post("/v1/allocations", json=allocation, headers=headers)
    assert client.get("/v1/allocations", headers=headers,
                      params={"after": first["next_cursor"]}).status_code == 409
    for limit in (0, 501, "one"):
        assert client.get("/v1/events", headers=headers, params={"limit": limit}).status_code == 400
        assert client.get("/v1/allocations", headers=headers, params={"limit": limit}).status_code == 400


def test_release_action_needs_version_but_not_creation_stage(client, allocation):
    headers = {"X-Owner": "alice"}
    registration = {
        "intent_id": "release-work", "owner_id": "alice", "execution_mode": "logical_simulation",
        "settlement_action": "keep_lease", "specification": {
            "allocation": {"kind": "allocation.create", "owner_id": "alice",
                           "request": allocation["request"]},
            "job": {"image": "example", "command": ["work"]},
        },
    }
    response = client.post("/x/v1/work-intents", headers=headers, json=registration)
    version = response.json()["version"]
    path = "/x/v1/work-intents/release-work/actions"
    assert client.post(path, headers=headers,
                       json={"action": "allocate", "expected_version": version}).status_code == 400
    response = client.post(path, headers=headers, json={"action": "allocate", "stage_id": "allocate",
                           "expected_version": version})
    version = response.json()["version"]
    assert client.post(path, headers=headers, json={"action": "release"}).status_code == 400
    response = client.post(path, headers=headers,
                           json={"action": "release", "expected_version": version})
    assert response.status_code == 200
    assert client.get("/v1/allocations", headers=headers).json()["allocations"][0]["state"] == "Released"


def test_product_admin_clock_is_not_a_test_backdoor(tmp_path):
    tokens = {"admin-key": {"actor_id": "admin", "role": "administrator"},
              "user-key": {"actor_id": "alice", "role": "researcher", "owner_id": "alice"}}
    with TestClient(create_app(data_dir=tmp_path, auth_tokens=tokens)) as client:
        response = client.post('/x/v1/admin/clock', json={'advance_seconds': 10},
                               headers={'Authorization': 'Bearer admin-key'})
        assert response.status_code == 200
        assert response.json()['now'] == 10
        assert client.post('/x/v1/admin/clock', json={'advance_seconds': 10},
                           headers={'Authorization': 'Bearer user-key', 'X-Owner': 'alice'}).status_code == 403
        assert client.post('/_test/clock', json={'advance_seconds': 10},
                           headers={'Authorization': 'Bearer admin-key'}).status_code == 403
