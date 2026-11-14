"""Persistent product names over both HTTP read adapters and the real SQLite kernel."""

import json

import pytest
from fastapi.testclient import TestClient

from campus_fabric.api.queries import make_kernel_services, register_queries
from campus_fabric.app import create_app
from campus_fabric.contracts import models as m

ALICE = {"X-Owner": "alice"}
BOB = {"X-Owner": "bob"}
TEST = {"X-Test-Key": "name-test-only"}
PATH = "/x/v1/work-intents"


def registration(ident="work-name", owner="alice"):
    return {
        "intent_id": ident, "owner_id": owner, "execution_mode": "logical_simulation",
        "settlement_action": "release_on_success",
        "specification": {
            "allocation": {
                "kind": "allocation.create", "owner_id": owner,
                "request": {"cpu_millicores": 1000, "memory_mib": 1024,
                            "gpu": {"count": 1, "model": "A10", "memory_mib": 1024}},
                "ttl_seconds": 100, "preemption_mode": "checkpoint",
                "checkpoint_timeout_seconds": 3, "max_restarts": 2,
            },
            "job": {"image": "name-test", "command": ["logical-only"], "timeout_seconds": 80},
        },
    }


@pytest.fixture(params=[False, True], ids=["kernel-queries", "joint-queries"])
def make_app(tmp_path, request):
    def build():
        resources = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                                      gpu_count=1, gpu_memory_mib=8192)
        node = m.NodeInventory(
            node_id="name-node", contributor_owner_id="supplier", state="Ready",
            capacity=resources, available=resources, observed_at=0.0,
            devices=(m.GPUDevice(device_id="name-gpu", physical_gpu_id="name-gpu", model="A10",
                                 memory_mib=8192, state="available"),),
        )
        app = create_app(data_dir=tmp_path, inventory=(node,), test_key=TEST["X-Test-Key"])
        if request.param:
            register_queries(app, make_kernel_services(app.state.kernel, app.state.identity,
                             clock_origin="2026-01-01T00:00:00Z"), replace_existing=True)
        return app
    return build


def detail(client, ident="work-name"):
    response = client.get(f"{PATH}/{ident}", headers=ALICE)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("name", ["", " \t\n　", "名" * 81, 123, True, [], {}])
def test_invalid_name_does_not_register_work(make_app, name):
    with TestClient(make_app()) as client:
        response = client.post(PATH, headers=ALICE,
                               json={**registration(), "display_name": name})
        assert response.status_code == 400, response.text
        assert client.get(PATH, headers=ALICE).json()["intents"] == []


@pytest.mark.parametrize("name", ["实验一", "名" * 80, "🧵" * 80])
def test_trimmed_names_survive_restart_retry_and_owner_isolation(make_app, name):
    original = {**registration(), "display_name": f"　 {name} \t"}
    with TestClient(make_app()) as client:
        first = client.post(PATH, headers=ALICE, json=original)
        assert first.status_code == 202, first.text
        assert detail(client)["intent"]["display_name"] == name
        # Names need not be unique, and never replace the stable work ID.
        duplicate = {**original, "intent_id": "another-work"}
        assert client.post(PATH, headers=ALICE, json=duplicate).status_code == 202
        rows = client.get(PATH, headers=ALICE).json()["intents"]
        assert {row["intent_id"] for row in rows} == {"work-name", "another-work"}
        assert [row["display_name"] for row in rows] == [name, name]
        assert client.get(f"{PATH}/work-name", headers=BOB).status_code == 404
        assert client.get(PATH, headers=BOB).json()["intents"] == []
        assert client.post(PATH, headers=BOB, json=original).status_code == 403

    with TestClient(make_app()) as client:
        normalized = {**registration(), "display_name": name}
        replay = client.post(PATH, headers=ALICE, json=normalized)
        assert replay.status_code == 202 and replay.json() == first.json()
        assert detail(client)["intent"]["display_name"] == name
        conflict = client.post(PATH, headers=ALICE,
                               json={**registration(), "display_name": "different-name"})
        assert conflict.status_code == 409 and conflict.json()["error"] == "stage_conflict"
        assert detail(client)["intent"]["display_name"] == name
        assert len(client.get(PATH, headers=ALICE).json()["intents"]) == 2


def test_legacy_missing_name_and_registration_receipt_remain_compatible(make_app):
    app = make_app()
    with TestClient(app) as client:
        first = client.post(PATH, headers=ALICE, json=registration())
        assert first.status_code == 202, first.text
        assert detail(client)["intent"]["display_name"] is None

    # Reproduce a pre-name persisted record and verify the old fingerprint shape.
    kernel = app.state.kernel
    with kernel.store.transaction() as conn:
        state = kernel.store.load(conn)
        state["intents"]["work-name"].pop("display_name")
        key = json.dumps(["alice", "workflow.register_intent", "work-name"])
        historical = json.loads(state["dedup"][key]["fingerprint"])
        assert "display_name" not in historical["registration"]
        kernel.store.save(conn, state)

    with TestClient(make_app()) as client:
        assert detail(client)["intent"]["display_name"] is None
        assert client.get(PATH, headers=ALICE).json()["intents"][0]["display_name"] is None
        for body in (registration(), {**registration(), "display_name": None}):
            retry = client.post(PATH, headers=ALICE, json=body)
            assert retry.status_code == 202 and retry.json() == first.json()
        assert client.post(PATH, headers=ALICE,
                           json={**registration(), "display_name": "not-a-rename"}).status_code == 409


def test_name_is_preserved_through_recall_resume_completion_and_release(make_app):
    with TestClient(make_app()) as client:
        response = client.post(PATH, headers=ALICE,
                               json={**registration(), "display_name": "校园  实验 A"})
        assert response.status_code == 202
        for action in ("allocate", "create_job"):
            response = client.post(f"{PATH}/work-name/actions", headers=ALICE, json={
                "action": action, "stage_id": action,
                "expected_version": detail(client)["version"],
            })
            assert response.status_code == 200, response.text
        initial = detail(client)["intent"]
        aid = initial["allocation_ids"][0]
        assert client.post("/_test/clock", headers=TEST,
                           json={"advance_seconds": 2}).status_code == 200
        assert client.post("/_test/contributor-demand", headers=TEST, json={
            "gpu_ids": ["name-gpu"], "notice_seconds": 3, "duration_seconds": 4,
        }).status_code == 201
        assert client.post(f"/v1/allocations/{aid}/checkpoint", headers=ALICE,
                           json={"status": "ready"}).status_code == 200
        assert client.post(f"/v1/allocations/{aid}/resume", headers=ALICE).status_code == 202
        assert detail(client)["intent"]["display_name"] == "校园  实验 A"
        assert client.post("/_test/clock", headers=TEST,
                           json={"advance_seconds": 15}).status_code == 200
        final = detail(client)["intent"]
        assert final["display_name"] == "校园  实验 A"
        assert final["state"] == "completed"
        assert final["allocation_ids"] == initial["allocation_ids"]
        assert final["job_ids"] == initial["job_ids"]
        assert len(final["attempts"]) == 2
        assert client.get(f"/v1/allocations/{aid}", headers=ALICE).json()["state"] == "Released"
