"""Recall notifications preserve owner boundaries and report actual deadline transitions."""

import copy

import pytest
from fastapi.testclient import TestClient

from campus_fabric.app import create_app
from campus_fabric.auth.sessions import hash_password
from campus_fabric.contracts import models as m
from campus_fabric.domain.lifecycle import LifecycleMixin
from campus_fabric.domain.recall_views import recall_status

TOKENS = {name: {"actor_id": name, "owner_id": name, "role": "member"}
          for name in ("alice", "bob", "supplier")}
TOKENS["admin"] = {"actor_id": "admin", "role": "administrator", "owner_id": None}
TEST = {"X-Test-Key": "recall-view-test-only"}


def headers(name):
    return {"Authorization": "Bearer " + name,
            **({"X-Owner": name} if name != "admin" else {})}


@pytest.fixture
def client(tmp_path):
    capacity = m.ResourceAmounts(cpu_millicores=8000, memory_mib=16000,
                                 gpu_count=2, gpu_memory_mib=16000)
    node = m.NodeInventory(
        node_id="shared-node", contributor_owner_id="supplier", state="Ready",
        capacity=capacity, available=capacity, observed_at=0.0,
        devices=tuple(m.GPUDevice(device_id=f"g{i}", physical_gpu_id=f"g{i}",
                                  model="A10", memory_mib=8000, state="available") for i in (0, 1)),
    )
    app = create_app(data_dir=tmp_path, inventory=(node,), auth_tokens=TOKENS,
                     test_key=TEST["X-Test-Key"], simulation_duration_seconds=100,
                     auth_accounts={"chen": {"password_hash": hash_password("test-only-password"),
                                             "actor": TOKENS["supplier"]}})
    with TestClient(app) as connection:
        yield connection


def work(client, owner="alice", mode="checkpoint"):
    ident = f"private-{owner}-work"
    response = client.post("/x/v1/work-intents", headers=headers(owner), json={
        "intent_id": ident, "owner_id": owner, "display_name": f"{owner} private experiment",
        "execution_mode": "logical_simulation", "settlement_action": "keep_lease",
        "specification": {"allocation": {
            "kind": "allocation.create", "owner_id": owner, "ttl_seconds": 300,
            "preemption_mode": mode, "checkpoint_timeout_seconds": 2,
            "request": {"cpu_millicores": 1000, "memory_mib": 1000,
                        "gpu": {"count": 1, "model": "A10"}}},
            "job": {"image": "reference", "command": ["reference"], "timeout_seconds": 200}},
    })
    assert response.status_code == 202, response.text
    for action in ("allocate", "create_job"):
        detail = client.get(f"/x/v1/work-intents/{ident}", headers=headers(owner)).json()
        response = client.post(f"/x/v1/work-intents/{ident}/actions", headers=headers(owner),
                               json={"action": action, "stage_id": action,
                                     "expected_version": detail["version"]})
        assert response.status_code == 200, response.text
    return client.get(f"/x/v1/work-intents/{ident}", headers=headers(owner)).json()["intent"]["allocation_ids"][0]


def recall(client):
    response = client.post("/x/v1/contributions/recall", headers=headers("supplier"),
                           json={"gpu_ids": ["g0"], "notice_seconds": 8, "duration_seconds": 5})
    assert response.status_code == 201, response.text


def advance(client, seconds):
    response = client.post("/_test/clock", headers=TEST, json={"advance_seconds": seconds})
    assert response.status_code == 200, response.text


def status(client, owner):
    response = client.get("/x/v1/recalls", headers=headers(owner))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_recall_view_is_read_only_owner_scoped_and_supports_normal_account_alias(client):
    aid = work(client)
    recall(client)
    before = client.app.state.kernel.store.read()
    denied = client.get("/x/v1/recalls")
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    assert client.get("/x/v1/recalls", headers={**headers("alice"), "X-Owner": "bob"}).status_code == 403
    borrower = status(client, "alice")
    assert borrower["execution_mode"] == "logical_simulation"
    assert borrower["version"] == {"epoch": before["epoch"], "revision": before["revision"]}
    assert borrower["borrowed"] == [{"allocation_id": aid, "intent_id": "private-alice-work",
        "display_name": "alice private experiment", "state": "saving", "checkpoint_due": 2.0,
        "deadline": 8.0, "checkpoint_status": None, "checkpoint_verified": False}]
    assert borrower["owned"] == []
    assert status(client, "bob")["borrowed"] == status(client, "bob")["owned"] == []
    assert status(client, "admin")["borrowed"] == status(client, "admin")["owned"] == []
    owner = status(client, "supplier")
    assert owner["owned"][0]["held_count"] == 1 and owner["owned"][0]["state"] == "saving"
    assert "alice" not in str(owner) and aid not in str(owner)
    login = client.post("/x/v1/auth/login", headers={"X-Fabric-Request": "browser"},
                        json={"username": "chen", "password": "test-only-password"})
    assert login.status_code == 200
    assert client.get("/x/v1/recalls").json() == owner
    assert client.app.state.kernel.store.read() == before


def test_countdown_uses_save_window_and_timeout_returns_without_borrower_action(client):
    aid = work(client)
    recall(client)
    assert status(client, "alice")["borrowed"][0]["checkpoint_due"] == 2
    advance(client, 2)
    assert client.get(f"/v1/allocations/{aid}", headers=headers("alice")).json()["state"] == "Evicted"
    assert status(client, "alice")["borrowed"] == []
    owned = status(client, "supplier")["owned"][0]
    assert owned["state"] == "ready" and owned["held_count"] == 0
    assert owned["unavailable_until"] == 13
    advance(client, 11)
    assert status(client, "supplier")["owned"][0]["state"] == "ended"


def test_member_receives_borrower_and_device_owner_status_in_the_same_account(client):
    aid = work(client, owner="supplier")
    recall(client)
    result = status(client, "supplier")
    assert result["borrowed"][0]["allocation_id"] == aid
    assert result["owned"][0]["held_count"] == 1


def test_private_reclaim_audit_survives_completion_and_hides_other_work(client):
    aid = work(client)
    kernel = client.app.state.kernel
    # Read-projection fixture; actual CPU audit production is tested by test_reclaim_driver.
    with kernel.store.transaction() as connection:
        state = kernel.store.load(connection)
        allocation = state["allocations"][aid]
        allocation["reclaim_history"] = [{"planned_at": 1., "action": "save_now", "phase": "complete"}]
        allocation["reclaim_plan"] = {"planned_at": 2., "action": "finish_first", "phase": "complete"}
        kernel.store.save(connection, state)
    url = "/x/v1/work-intents/private-alice-work/reclaim-decisions"
    assert client.get(url).status_code == 401
    for other in ("bob", "supplier"):
        assert client.get(url, headers=headers(other)).status_code == 404
    own = client.get(url, headers=headers("alice"))
    assert own.status_code == 200 and own.headers["cache-control"] == "no-store"
    assert [item["action"] for item in own.json()["items"]] == ["save_now", "finish_first"]
    assert client.get(url, headers=headers("admin")).json() == own.json()
    assert client.post("/_test/contributor-demand", headers=TEST, json={
        "gpu_ids": ["g0"], "notice_seconds": 8, "duration_seconds": 5,
        "return_policy": "return_first"}).status_code == 400


def test_early_save_returns_device_and_new_lease_does_not_reopen_old_notice(client):
    aid = work(client, mode="restartable")
    recall(client)
    advance(client, 1)
    assert client.post(f"/v1/allocations/{aid}/checkpoint", headers=headers("alice"),
                       json={"status": "ready"}).status_code == 200
    row = status(client, "alice")["borrowed"][0]
    assert row["state"] == "interrupted" and row["checkpoint_status"] == "ready"
    assert not row["checkpoint_verified"], "A logical ready report is not a verified CPU artifact"
    assert status(client, "supplier")["owned"][0]["state"] == "ready"
    advance(client, 12)
    work(client, owner="bob")
    row = status(client, "supplier")["owned"][0]
    assert row["state"] == "ended" and row["held_count"] == 0


def test_projection_counts_physical_overlap_and_preserves_unconfirmed_stops_past_window():
    # Synthetic read fixture: verifies projection semantics, not physical execution.
    actor = m.AuthenticatedActor(actor_id="supplier", owner_id="supplier", role="member")
    state = {"epoch": 0, "revision": 12, "now": 20.0, "model": {"execution_mode": "cpu_reference"},
        "nodes": {"node": {"devices": [
            {"device_id": "whole", "physical_gpu_id": "physical", "profile": None},
            {"device_id": "slice", "physical_gpu_id": "physical", "profile": "half"}]}},
        "allocations": {"lease": {"allocation_id": "lease", "owner_id": "alice",
            "intent_id": "work", "created_at": 0.0, "state": "PreemptRequested", "stop_requested": True,
            "placement": {"slices": [{"device_ids": ["slice"]}]},
            "preemption": {"requested_at": 1.0, "deadline": 3.0, "checkpoint_due": 2.0,
                           "reason": "contributor_recall"}}},
        "jobs": {"job": {"allocation_id": "lease", "state": "Checkpointing",
                         "attempt": {"attempt_id": "attempt"}}},
        "intents": {"work": {"state": "running", "attempts": [
            {"ref": {"attempt_id": "attempt"}, "state": "quarantined"}]}},
        "recalls": {"notice": {"notice_id": "notice", "contributor_owner_id": "supplier",
            "device_ids": ["whole"], "issued_at": 1.0, "deadline": 3.0, "unavailable_until": 5.0}}}
    original = copy.deepcopy(state)
    result = recall_status(state, actor, LifecycleMixin._device_overlap)
    assert result["owned"][0]["state"] == "attention" and result["owned"][0]["held_count"] == 1
    assert "alice" not in str(result) and "lease" not in str(result)
    assert state == original
    borrower = m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="member")
    assert recall_status(state, borrower, LifecycleMixin._device_overlap)["borrowed"][0]["state"] == "attention"
    state["intents"]["work"]["attempts"][0]["state"] = "stopping"
    assert recall_status(state, actor, LifecycleMixin._device_overlap)["owned"][0]["state"] == "stopping"
    state["allocations"]["lease"].update(state="Preempted", placement=None)
    assert recall_status(state, actor, LifecycleMixin._device_overlap)["owned"][0]["state"] == "ended"
    state["intents"]["work"]["state"] = "cancelled"
    assert recall_status(state, borrower, LifecycleMixin._device_overlap)["borrowed"] == []
