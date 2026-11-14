"""One ordinary identity can borrow and contribute without gaining wider ownership."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from campus_fabric.api.queries import make_kernel_services, register_queries
from campus_fabric.app import create_app
from campus_fabric.contracts import models as m

TOKENS = {
    f"test-only-{owner}": {"actor_id": owner, "owner_id": owner, "role": "member"}
    for owner in ("alice", "bob")
}
TOKENS.update({
    "test-only-admin": {"actor_id": "admin", "role": "administrator"},
    "test-only-legacy": {"actor_id": "legacy", "owner_id": "legacy", "role": "researcher"},
})


def headers(owner):
    result = {"Authorization": f"Bearer test-only-{owner}"}
    if owner != "admin":
        result["X-Owner"] = owner
    return result


def node(owner):
    resources = m.ResourceAmounts(cpu_millicores=8000, memory_mib=16000,
                                 gpu_count=1, gpu_memory_mib=8192)
    return m.NodeInventory(
        node_id=f"{owner}-node", contributor_owner_id=owner, state="Ready",
        capacity=resources, available=resources, observed_at=0.0,
        devices=(m.GPUDevice(device_id=f"{owner}-gpu", physical_gpu_id=f"{owner}-gpu",
                             model="A10", memory_mib=8192, state="available"),),
    ).model_dump(mode="json")


def registration(owner):
    return m.WorkIntentRegistration(
        intent_id=f"{owner}-work", owner_id=owner, execution_mode="logical_simulation",
        settlement_action="release_on_success",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(
                kind="allocation.create", owner_id=owner, ttl_seconds=300,
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1000,
                                          gpu=m.GPURequest(count=1)),
            ),
            job=m.JobSpecification(image="simulation", command=("reference",)),
        ),
    ).model_dump(mode="json")


@pytest.mark.parametrize("entrypoint", ["core", "devhost"])
def test_session_is_trusted_read_only_and_uncached_in_both_hosts(tmp_path, monkeypatch, entrypoint):
    if entrypoint == "core":
        app = create_app(data_dir=tmp_path, auth_tokens=TOKENS)
    else:
        from campus_fabric.workflows.devhost import create_app as create_devhost
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps(TOKENS))
        monkeypatch.setenv("FABRIC_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("FABRIC_AUTH_FILE", str(auth))
        monkeypatch.setenv("FABRIC_MODE", "logical_simulation")
        app = create_devhost()
    with TestClient(app) as client:
        before = app.state.kernel.store.read()
        response = client.get("/x/v1/session", headers=headers("alice"))
        assert response.status_code == 200
        assert response.json() == {
            "actor_id": "alice", "owner_id": "alice",
            "capabilities": ["use_compute", "contribute_resources"], "is_administrator": False,
        }
        Draft202012Validator(m.AccountSession.model_json_schema()).validate(response.json())
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Request-Id"]
        assert "Authorization" in response.headers["Vary"]
        assert client.get("/x/v1/session", headers={
            **headers("alice"), "X-Role": "administrator", "X-Capabilities": "administrator",
        }).json() == response.json()
        for bad, status in (({}, 401), ({"Authorization": "Bearer invalid"}, 401),
                            ({**headers("alice"), "X-Owner": "bob"}, 403)):
            denied = client.get("/x/v1/session", headers=bad)
            assert denied.status_code == status
            assert denied.headers["Cache-Control"] == "no-store"
            assert "capabilities" not in denied.json()
        admin = client.get("/x/v1/session", headers=headers("admin")).json()
        assert admin["is_administrator"] is True and admin["owner_id"] is None
        legacy = client.get("/x/v1/session", headers=headers("legacy")).json()
        assert legacy["capabilities"] == ["use_compute"]
        assert app.state.kernel.store.read() == before


def test_member_work_and_contribution_share_owner_without_sharing_authority(tmp_path):
    app = create_app(data_dir=tmp_path, auth_tokens=TOKENS, simulation_duration_seconds=100)
    register_queries(app, make_kernel_services(app.state.kernel, app.state.identity,
                     clock_origin="2026-01-01T00:00:00Z"), replace_existing=True)
    with TestClient(app) as client:
        for owner in ("alice", "bob"):
            contributed = client.post("/x/v1/contributions", json=node(owner), headers=headers(owner))
            assert contributed.status_code == 201, contributed.text
            submitted = client.post("/x/v1/work-intents", json=registration(owner),
                                    headers=headers(owner))
            assert submitted.status_code == 202, submitted.text
            for action in ("allocate", "create_job"):
                detail = client.get(f"/x/v1/work-intents/{owner}-work", headers=headers(owner)).json()
                result = client.post(f"/x/v1/work-intents/{owner}-work/actions",
                    headers=headers(owner), json={"action": action, "stage_id": action,
                                                 "expected_version": detail["version"]})
                assert result.status_code < 300, result.text
            detail = client.get(f"/x/v1/work-intents/{owner}-work", headers=headers(owner)).json()
            assert detail["jobs"][0]["state"] == "Running"
            assert "allocation.release" in {x["operation"] for x in detail["legal_actions"]}
            supplied = client.get("/x/v1/contributions", headers=headers(owner)).json()["items"]
            assert [x["node_id"] for x in supplied] == [f"{owner}-node"]

        before = app.state.kernel.store.read()
        alice = headers("alice")
        bob_detail = client.get("/x/v1/work-intents/bob-work", headers=headers("bob")).json()
        bob_job = bob_detail["jobs"][0]["id"]
        assert client.get("/x/v1/work-intents/bob-work", headers=alice).status_code == 404
        assert client.delete(f"/v1/jobs/{bob_job}", headers=alice).status_code == 404
        assert client.post("/x/v1/contributions", headers=alice, json=node("bob")).status_code == 403
        recall = {"gpu_ids": ["alice-gpu", "bob-gpu"], "notice_seconds": 1, "duration_seconds": 5}
        assert client.post("/x/v1/contributions/recall", headers=alice, json=recall).status_code == 403
        for path in ("/x/v1/admin/overview", "/x/v1/acceptance"):
            assert client.get(path, headers={**alice, "X-Role": "administrator"}).status_code == 403
        assert client.post("/x/v1/admin/clock", headers=alice,
                           json={"advance_seconds": 1}).status_code == 403
        assert client.post("/_test/reset", headers=alice).status_code == 403
        assert app.state.kernel.store.read() == before, "Denied operations must be atomic no-ops"

        recall["gpu_ids"] = ["alice-gpu"]
        accepted = client.post("/x/v1/contributions/recall", headers=alice, json=recall)
        assert accepted.status_code == 201, accepted.text
        after = app.state.kernel.store.read()
        assert len(after["recalls"]) == 1
        assert next(iter(after["recalls"].values()))["contributor_owner_id"] == "alice"
        assert after["intents"]["bob-work"]["owner_id"] == "bob"
        assert client.get("/x/v1/session", headers=alice).json()["is_administrator"] is False

    # Both kinds of ownership survive restart; session reads do not create another identity.
    reopened = create_app(data_dir=tmp_path, auth_tokens=TOKENS, simulation_duration_seconds=100)
    assert reopened.state.kernel.store.read() == after


def test_local_owner_mode_remains_legacy_restricted(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        session = client.get("/x/v1/session", headers={"X-Owner": "alice"}).json()
        assert session["capabilities"] == ["use_compute"]
        assert session["is_administrator"] is False
        assert client.post("/x/v1/contributions", json=node("alice"),
                           headers={"X-Owner": "alice", "X-Role": "member"}).status_code == 403
