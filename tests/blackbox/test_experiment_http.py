"""Registered experiment API integration; all execution stays in temporary instances."""

import hashlib

from fastapi.testclient import TestClient

from campus_fabric.contracts import models as m
from campus_fabric.workflows.devhost import create_app

ADMIN = {"Authorization": "Bearer isolated-experiment-admin"}
ALICE = {"Authorization": "Bearer isolated-experiment-alice", "X-Owner": "alice"}
TOKENS = {
    "isolated-experiment-admin": m.AuthenticatedActor(actor_id="admin", role="administrator"),
    "isolated-experiment-alice": m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="researcher"),
}


def test_registered_experiments_have_real_records_and_cannot_mutate_operating_ledger(tmp_path):
    app = create_app(data_dir=tmp_path, auth_tokens=TOKENS, worker_enabled=False)
    with TestClient(app) as client:
        before = client.get("/v1/metrics", headers=ADMIN).json()
        catalog = client.get("/x/v1/experiments/catalog", headers=ADMIN)
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["items"]
        assert all(row["execution_mode"] == "logical_simulation" for row in catalog.json()["items"])
        response = client.post("/x/v1/experiments", headers=ADMIN, json={
            "scenario_id": "cp2-negative-control-v1", "policy": "B0", "seed": 4,
        })
        assert response.status_code == 202, response.text
        assert response.json()["item"]["status"] == "running"
        run_id = response.json()["item"]["run_id"]
        app.state.experiment_service.wait(run_id, timeout=15)
        result = client.get("/x/v1/experiments/" + run_id, headers=ADMIN)
        assert result.status_code == 200, result.text
        run = result.json()["item"]
        # Source changes during concurrent development may correctly mark a run
        # failed. Completion of the worker never silently changes that verdict.
        assert run["status"] in {"completed", "failed"}
        assert run["outputs"] and run["checks"] == []
        listed = client.get("/x/v1/experiments", headers=ADMIN).json()["items"]
        assert listed == [run]
        input_record = run["inputs"][0]
        artifact = client.get(f"/x/v1/experiments/{run_id}/artifacts/input", headers=ADMIN)
        assert artifact.status_code == 200, artifact.text
        assert hashlib.sha256(artifact.content).hexdigest() == input_record["sha256"]
        assert artifact.headers["Cache-Control"] == "no-store"
        assert artifact.headers["X-Content-Type-Options"] == "nosniff"
        summary = client.get(f"/x/v1/experiments/{run_id}/artifacts/summary", headers=ADMIN).json()
        assert any(row["status"] != "completed" for row in summary["works"])
        assert "not CP3 validation" in summary["scope"]
        after = client.get("/v1/metrics", headers=ADMIN).json()
        assert after["version"] == before["version"]
        assert after["counts"] == before["counts"]
        assert after["now"] == before["now"]
        for path in ("/x/v1/experiments", "/x/v1/experiments/catalog",
                     f"/x/v1/experiments/{run_id}/artifacts/summary"):
            assert client.get(path, headers={**ALICE, "X-Role": "administrator"}).status_code == 403
        assert client.get("/x/v1/experiments/" + run_id, headers=ALICE).status_code == 404
        assert client.get(f"/x/v1/experiments/{run_id}/artifacts/auth.json", headers=ADMIN).status_code == 404
        (tmp_path / "experiments" / run_id / "summary.json").write_text("{}")
        corrupted = client.get(f"/x/v1/experiments/{run_id}/artifacts/summary", headers=ADMIN)
        assert corrupted.status_code == 409 and corrupted.json()["error"] == "invalid_evidence"


def test_experiment_submit_authenticates_first_and_rejects_arbitrary_scenarios(tmp_path):
    app = create_app(data_dir=tmp_path, auth_tokens=TOKENS, worker_enabled=False)
    with TestClient(app) as client:
        assert client.post("/x/v1/experiments", content=b"not-json").status_code == 401
        assert client.post("/x/v1/experiments", headers=ALICE, json={}).status_code == 403
        for body in ([], {"scenario_id": "cp2-contention-v1", "policy": "B0", "seed": True},
                     {"scenario_id": "cp2-contention-v1", "policy": "B0", "seed": -1},
                     {"scenario_id": "cp2-contention-v1", "policy": "B0", "seed": 10000},
                     {"scenario_id": "file:///private/scenario", "policy": "B0"},
                     {"scenario_id": "cp2-contention-v1", "policy": "B0", "kernel_url": "http://localhost:18081"}):
            response = client.post("/x/v1/experiments", headers=ADMIN, json=body)
            assert response.status_code in {400, 404}, response.text
        assert client.get("/x/v1/experiments", headers=ADMIN).json()["items"] == []
