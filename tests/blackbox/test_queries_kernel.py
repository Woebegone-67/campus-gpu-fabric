"""B read adapters against A's actual CP1 SQLite authority, isolated temporary DBs."""

from fastapi.testclient import TestClient

from campus_fabric.api.queries import make_kernel_services, register_queries
from campus_fabric.app import create_app
from campus_fabric.contracts import models as m

ORIGIN = "2026-01-01T00:00:00Z"
ALICE = {"X-Owner": "alice"}
BOB = {"X-Owner": "bob"}
TEST_KEY = {"X-Test-Key": "query-module-test-only"}


def app_client(tmp_path, *, tokens=None):
    resources = m.ResourceAmounts(cpu_millicores=8000, memory_mib=16000)
    nodes = tuple(m.NodeInventory(
        node_id=f"isolated-query-node-{i}", contributor_owner_id="supply", state="Ready",
        capacity=resources, available=resources, observed_at=0.0,
    ) for i in range(2))
    app = create_app(data_dir=tmp_path, inventory=nodes, auth_tokens=tokens,
                     test_key=TEST_KEY["X-Test-Key"])
    register_queries(app, make_kernel_services(app.state.kernel, app.state.identity,
                     clock_origin=ORIGIN), replace_existing=True)
    return TestClient(app), app.state.kernel


def allocate(client, owner="alice"):
    response = client.post("/v1/allocations", headers={"X-Owner": owner}, json={
        "owner": owner,
        "request": {"cpu_millicores": 1000, "memory_mib": 1000, "gpu": {"count": 0}},
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def registration():
    return m.WorkIntentRegistration(
        intent_id="isolated-query-intent", owner_id="alice", execution_mode="logical_simulation",
        settlement_action="release_on_success",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id="alice",
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1000,
                                           gpu=m.GPURequest(count=0))),
            job=m.JobSpecification(image="isolated-simulation", command=("no-real-execution",)),
        ),
    ).model_dump(mode="json")


def test_cp1_mutations_project_real_records_dates_and_owner_isolation(tmp_path):
    client, kernel = app_client(tmp_path)
    alice_id, bob_id = allocate(client), allocate(client, "bob")
    job = client.post("/v1/jobs", headers=ALICE, json={
        "allocation_id": alice_id, "image": "sim", "command": ["not-executed"],
    })
    assert job.status_code == 201
    revision = kernel.decision_snapshot().version
    allocations = client.get("/v1/allocations", headers=ALICE).json()
    assert allocations["items"] == allocations["allocations"]
    assert [a["id"] for a in allocations["items"]] == [alice_id]
    assert client.get(f"/v1/allocations/{bob_id}", headers=ALICE).status_code == 404
    query = client.get(f"/v1/jobs/{job.json()['id']}", headers=ALICE)
    assert query.json()["created_at"] == ORIGIN
    assert query.json()["request_id"] == query.headers["X-Request-Id"]
    assert client.get("/v1/jobs", headers=BOB).json()["items"] == []
    metrics = client.get("/v1/metrics", headers=ALICE).json()
    assert metrics["counts"] == {"allocations": 1, "jobs": 1, "work_intents": 0}
    assert metrics["running_jobs"] == 1
    assert "bob" not in client.get("/v1/events", headers=ALICE).text
    assert kernel.decision_snapshot().version == revision, "GETs must not mutate A's authority"


def test_cp1_cursor_pagination_reset_and_inventory_totals(tmp_path):
    client, _ = app_client(tmp_path)
    allocate(client)
    allocate(client)
    first = client.get("/v1/allocations?limit=1", headers=ALICE).json()
    second = client.get("/v1/allocations", params={"after": first["next_cursor"]}, headers=ALICE)
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] != first["items"][0]["id"]
    allocate(client, "bob")
    assert client.get("/v1/allocations", params={"after": first["next_cursor"]}, headers=ALICE).status_code == 409
    page = client.get("/v1/capacity?limit=1", headers=ALICE).json()
    assert len(page["nodes"]) == 1
    assert page["totals"]["capacity"]["cpu_millicores"] == 16000
    assert page["totals"]["available"]["cpu_millicores"] == 13000
    empty = client.get("/v1/events", headers={"X-Owner": "nobody"}).json()
    assert empty["items"] == [] and empty["next_after"] == "0:0"
    events = client.get("/v1/events", headers=ALICE).json()
    assert events["next_cursor"]["after_sequence"] == events["items"][-1]["sequence"]
    assert client.post("/_test/reset", headers=TEST_KEY).status_code == 204
    response = client.get("/v1/events", params={"after": events["next_after"]}, headers=ALICE)
    assert response.status_code == 409 and response.json()["error"] == "stale_epoch"
    assert client.get("/v1/capacity", headers=ALICE).json()["nodes"] == []


def test_cp1_workflow_actions_and_completion_are_observed_from_commits(tmp_path):
    client, _ = app_client(tmp_path)
    submitted = client.post("/x/v1/work-intents", headers=ALICE, json=registration())
    assert submitted.status_code == 202, submitted.text
    path = "/x/v1/work-intents/isolated-query-intent"
    state = client.get(path, headers=ALICE).json()
    assert state["item"] == state["intent"]
    assert any(x["operation"] == "workflow.create_allocation_and_bind" and x["advisory"]
               for x in state["legal_actions"])
    for action, stage in (("allocate", "allocate-v1"), ("create_job", "job-v1")):
        state = client.get(path, headers=ALICE).json()
        result = client.post(path + "/actions", headers=ALICE, json={
            "action": action, "stage_id": stage, "expected_version": state["version"],
        })
        assert result.status_code == 200, result.text
    running = client.get(path, headers=ALICE).json()
    assert running["item"]["state"] == "running"
    assert len(running["allocations"]) == len(running["jobs"]) == 1
    assert any(x["operation"] == "job.cancel" for x in running["legal_actions"])
    assert client.post("/_test/clock", headers=TEST_KEY, json={"advance_seconds": 10}).status_code == 200
    complete = client.get(path, headers=ALICE).json()
    assert complete["item"]["state"] == "completed"
    assert complete["allocations"][0]["state"] == "Released"
    assert complete["jobs"][0]["state"] == "Succeeded"
    assert complete["legal_actions"] == []
    assert client.get(path, headers=BOB).status_code == 404


def test_cp1_trusted_roles_global_overview_and_evidence_remain_honest(tmp_path):
    tokens = {
        "isolated-admin": m.AuthenticatedActor(actor_id="admin", role="administrator"),
        "isolated-alice": m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="researcher"),
    }
    client, _ = app_client(tmp_path, tokens=tokens)
    alice = {**ALICE, "Authorization": "Bearer isolated-alice", "X-Role": "administrator"}
    admin = {"Authorization": "Bearer isolated-admin"}
    assert client.get("/v1/jobs", headers=ALICE).status_code == 401
    assert client.get("/x/v1/admin/overview", headers=alice).status_code == 403
    overview = client.get("/x/v1/admin/overview", headers=admin)
    assert overview.status_code == 200 and overview.json()["scope"] == "global"
    metrics = client.get("/v1/metrics", headers=admin)
    assert metrics.status_code == 200 and metrics.json()["scope"] == "global"
    assert metrics.json()["counts"] == overview.json()["counts"]
    assert client.get("/x/v1/acceptance", headers=admin).status_code == 503


def test_cp1_global_admin_queries_are_consistent_while_owner_defaults_remain_isolated(tmp_path):
    tokens = {
        "global-admin": m.AuthenticatedActor(actor_id="global-admin", role="administrator"),
        "tenant-admin": m.AuthenticatedActor(actor_id="tenant-admin", owner_id="alice", role="administrator"),
        "alice-token": m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="researcher"),
        "bob-token": m.AuthenticatedActor(actor_id="bob", owner_id="bob", role="researcher"),
    }
    client, _ = app_client(tmp_path, tokens=tokens)
    global_admin = {"Authorization": "Bearer global-admin"}
    tenant_admin = {"Authorization": "Bearer tenant-admin", "X-Owner": "alice"}
    owner_headers = {
        owner: {"Authorization": f"Bearer {owner}-token", "X-Owner": owner,
                "X-Role": "administrator"} for owner in ("alice", "bob")
    }
    for owner, headers in owner_headers.items():
        body = registration()
        body["intent_id"] = f"{owner}-intent"
        body["owner_id"] = body["specification"]["allocation"]["owner_id"] = owner
        assert client.post("/x/v1/work-intents", headers=headers, json=body).status_code == 202
        path = f"/x/v1/work-intents/{owner}-intent"
        for action, stage in (("allocate", "allocate-v1"), ("create_job", "job-v1")):
            current = client.get(path, headers=headers).json()
            response = client.post(path + "/actions", headers=headers, json={
                "action": action, "stage_id": stage, "expected_version": current["version"],
            })
            assert response.status_code == 200, response.text

    overview = client.get("/x/v1/admin/overview", headers=global_admin).json()
    metrics = client.get("/v1/metrics", headers=global_admin).json()
    assert metrics["counts"] == overview["counts"] == {
        "allocations": 2, "jobs": 2, "work_intents": 2,
    }
    assert metrics["version"] == overview["version"]
    for path in ("/v1/allocations", "/v1/jobs", "/x/v1/work-intents", "/v1/events"):
        global_read = client.get(path, headers=global_admin)
        assert global_read.status_code == 200, global_read.text
        assert global_read.json()["scope"] == "global"
        assert {item["owner_id"] for item in global_read.json()["items"]} == {"alice", "bob"}
        for headers in (owner_headers["alice"], tenant_admin):
            scoped = client.get(path + "?global_scope=true", headers=headers).json()
            assert scoped["scope"] == "owner"
            assert {item["owner_id"] for item in scoped["items"]} == {"alice"}
    assert client.get("/x/v1/work-intents/bob-intent", headers=global_admin).status_code == 200
    assert client.get("/x/v1/work-intents/bob-intent", headers=tenant_admin).status_code == 404
    assert client.get("/v1/jobs", headers={**global_admin, "X-Owner": "alice"}).status_code == 403
