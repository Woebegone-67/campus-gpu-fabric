"""Independent CP1 query/auth audit against the actual B-over-A TCP host."""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
QUERY_PATHS = (
    "/v1/capacity", "/v1/allocations", "/v1/allocations/unknown", "/v1/jobs",
    "/v1/jobs/unknown", "/v1/events", "/v1/metrics", "/x/v1/work-intents",
    "/x/v1/work-intents/unknown", "/x/v1/work-intents/unknown/artifacts/unknown",
    "/x/v1/decisions/unknown", "/x/v1/contributions", "/x/v1/admin/overview",
    "/x/v1/experiments/unknown", "/x/v1/acceptance",
)


@pytest.fixture
def live(tmp_path):
    tokens = {owner: secrets.token_urlsafe(24) for owner in ("alice", "bob", "supplier", "admin")}
    identities = {
        token: {"actor_id": owner, "owner_id": None if owner == "admin" else owner,
                "role": "administrator" if owner == "admin" else
                "contributor" if owner == "supplier" else "researcher"}
        for owner, token in tokens.items()
    }
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(identities))
    auth.chmod(0o600)
    capacity = {"cpu_millicores": 100000, "memory_mib": 100000}
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([{
        "node_id": "security-node", "contributor_owner_id": "supplier", "state": "Ready",
        "capacity": capacity, "available": capacity, "observed_at": 0.0,
    }]))
    # Keep the actual TCP host and receipt reader, but give this no-evidence
    # fixture its own registry root. Developer acceptance files must not affect it.
    evidence_root = tmp_path / "receipt-root"
    (evidence_root / "configs").mkdir(parents=True)
    (evidence_root / "configs/checkpoints.json").write_bytes(
        (ROOT / "configs/checkpoints.json").read_bytes())
    (tmp_path / "isolated_security_host.py").write_text(
        "from pathlib import Path\n"
        "from campus_fabric.workflows import devhost\n"
        "from campus_fabric.workflows.acceptance_evidence import checkpoint_checks\n"
        "\n"
        "def create_app():\n"
        "    evidence_root = Path(__file__).parent / 'receipt-root'\n"
        "    devhost.checkpoint_checks = lambda _root: checkpoint_checks(evidence_root)\n"
        "    return devhost.create_app()\n"
    )
    test_key = secrets.token_urlsafe(24)
    env = {key: value for key, value in os.environ.items() if not key.startswith("FABRIC_")}
    env.update(FABRIC_AUTH_FILE=str(auth), FABRIC_INVENTORY_FILE=str(inventory),
               FABRIC_DATA_DIR=str(tmp_path / "db"), FABRIC_TEST_KEY=test_key,
               FABRIC_MODE="logical_simulation", FABRIC_SCHEDULING_MODE="builtin")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen([
            sys.executable, "-m", "uvicorn", "isolated_security_host:create_app",
            "--app-dir", str(tmp_path), "--factory", "--host", "127.0.0.1",
            "--port", str(port), "--log-level", "error",
        ], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    assert process.poll() is None, (tmp_path / "server.log").read_text()
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("Joint devhost did not become ready")
                headers = {owner: {"Authorization": "Bearer " + token,
                           **({"X-Owner": owner} if owner != "admin" else {})}
                           for owner, token in tokens.items()}
                headers["test"] = {"X-Test-Key": test_key}
                yield client, headers
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def allocate(client, headers, owner="alice"):
    response = client.post("/v1/allocations", headers=headers[owner], json={
        "owner": owner, "request": {"cpu_millicores": 100, "memory_mib": 100,
                                     "gpu": {"count": 0}},
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_all_joint_query_routes_bind_trusted_identity(live):
    client, headers = live
    for path in QUERY_PATHS:
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"X-Owner": "alice", "X-Role": "administrator"}).status_code == 401, path
        assert client.get(path, headers={**headers["alice"], "X-Owner": "bob"}).status_code == 403, path
        assert client.get(path, headers=headers["test"]).status_code == 401, path
    forged = {**headers["alice"], "X-Role": "administrator"}
    for path in ("/x/v1/admin/overview", "/x/v1/acceptance", "/x/v1/contributions"):
        assert client.get(path, headers=forged).status_code == 403
    assert client.post("/x/v1/admin/clock", headers=forged,
                       json={"advance_seconds": 10}).status_code == 403


def test_joint_tenant_lists_details_events_metrics_and_queries_are_read_only(live):
    client, headers = live
    alice_id, bob_id = allocate(client, headers), allocate(client, headers, "bob")
    job = client.post("/v1/jobs", headers=headers["alice"], json={
        "allocation_id": alice_id, "image": "private-alice-image", "command": ["private-alice-command"],
    })
    assert job.status_code == 201, job.text
    initial = client.get("/v1/metrics", headers=headers["admin"]).json()
    for owner, other_id in (("alice", bob_id), ("bob", alice_id)):
        for path in ("/v1/allocations", "/v1/jobs", "/v1/events", "/x/v1/work-intents"):
            data = client.get(path + "?global_scope=true&owner=other", headers=headers[owner]).json()
            assert data["scope"] == "owner"
            assert all(item["owner_id"] == owner for item in data["items"])
        assert client.get(f"/v1/allocations/{other_id}", headers=headers[owner]).status_code == 404
    assert client.get(f"/v1/jobs/{job.json()['id']}", headers=headers["bob"]).status_code == 404
    assert client.get("/v1/metrics", headers=headers["bob"]).json()["counts"] == {
        "allocations": 1, "jobs": 0, "work_intents": 0,
    }
    for path in QUERY_PATHS:
        response = client.get(path, headers=headers["admin"])
        assert response.status_code in (200, 404, 503), (path, response.text)
    final = client.get("/v1/metrics", headers=headers["admin"]).json()
    assert final["version"] == initial["version"]
    assert final["counts"] == initial["counts"]
    assert "contributor_owner_id" not in client.get("/v1/capacity", headers=headers["alice"]).text
    supply = client.get("/x/v1/contributions", headers=headers["supplier"]).json()
    assert {item["contributor_owner_id"] for item in supply["items"]} == {"supplier"}


def test_joint_object_pagination_counts_and_stale_version(live):
    client, headers = live
    expected = {allocate(client, headers) for _ in range(55)}
    first = client.get("/v1/allocations", headers=headers["alice"]).json()
    assert len(first["items"]) == 50 and first["page"]["total"] == 55
    second = client.get("/v1/allocations", headers=headers["alice"],
                        params={"after": first["next_cursor"]}).json()
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    assert {item["id"] for item in first["items"] + second["items"]} == expected
    assert client.get("/v1/metrics", headers=headers["alice"]).json()["counts"]["allocations"] == 55
    allocate(client, headers, "bob")
    assert client.get("/v1/allocations", headers=headers["alice"],
                      params={"after": first["next_cursor"]}).status_code == 409
    for params in ({"limit": 0}, {"limit": 501}, {"offset": 1}, {"after": "1:2"}):
        assert client.get("/v1/allocations", headers=headers["alice"], params=params).status_code == 400


def test_joint_event_cursor_never_skips_tenant_events_and_reset_invalidates(live):
    client, headers = live
    for _ in range(3):
        allocate(client, headers)
        allocate(client, headers, "bob")
    all_events = client.get("/v1/events?limit=500", headers=headers["alice"]).json()["items"]
    observed = []
    after = None
    while True:
        data = client.get("/v1/events", headers=headers["alice"],
                          params={"limit": 1, **({"after": after} if after else {})}).json()
        observed.extend(data["items"])
        after = data["next_after"]
        if not data["has_more"]:
            break
    assert observed == all_events
    assert int(after.split(":")[1]) == observed[-1]["sequence"]
    assert client.get("/v1/events?after=0:999999", headers=headers["alice"]).status_code == 409
    assert client.post("/_test/reset", headers=headers["test"]).status_code == 204
    stale = client.get("/v1/events", params={"after": after}, headers=headers["alice"])
    assert stale.status_code == 409 and stale.json()["error"] == "stale_epoch"


def test_joint_work_result_access_and_unregistered_evidence_are_honest(live):
    client, headers = live
    body = {
        "intent_id": "audit-private-intent", "owner_id": "alice",
        "execution_mode": "logical_simulation", "settlement_action": "release_on_success",
        "specification": {"allocation": {"kind": "allocation.create", "owner_id": "alice",
            "request": {"cpu_millicores": 100, "memory_mib": 100, "gpu": {"count": 0}}},
            "job": {"image": "not-executed", "command": ["not-executed"]}},
    }
    response = client.post("/x/v1/work-intents", headers=headers["alice"], json=body)
    assert response.status_code == 202, response.text
    path = "/x/v1/work-intents/audit-private-intent"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        actual = client.get(path, headers=headers["alice"]).json()
        if actual["item"]["state"] == "running":
            break
        time.sleep(0.05)
    assert actual["item"]["state"] == "running"
    assert actual["now"] == 0.0
    assert client.get(path, headers=headers["bob"]).status_code == 404
    assert client.get(path + "/artifacts/unknown", headers=headers["bob"]).status_code == 404
    assert client.get(path + "/artifacts/unknown", headers=headers["alice"]).status_code == 503
    acceptance = client.get("/x/v1/acceptance", headers=headers["admin"]).json()
    assert acceptance["status"] == "NOT_RUN" and acceptance["build_matches"] is None
    assert acceptance["items"] and all(item["status"] == "NOT_RUN" for item in acceptance["items"])
    assert client.get("/x/v1/experiments/unregistered-run", headers=headers["alice"]).status_code == 404
    assert client.post("/x/v1/admin/clock", headers=headers["admin"],
                       json={"advance_seconds": 10}).status_code == 200
    complete = client.get(path, headers=headers["alice"]).json()
    assert complete["item"]["state"] == "completed"
    assert complete["jobs"][0]["state"] == "Succeeded"
    assert complete["allocations"][0]["state"] == "Released"
