"""Startup coverage remains distinct from joint product acceptance."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from campus_fabric.app import create_app

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = json.loads((ROOT / "contracts/api_ownership.json").read_text())["operations"]
CP2 = {"/v1/allocations/{allocation_id}/checkpoint", "/v1/allocations/{allocation_id}/resume",
       "/_test/contributor-demand", "/_test/node-state", "/_test/fault"}


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda item: item["operation_id"])
def test_all_official_routes_remain_registered_and_scope_truthful(operation, tmp_path):
    path = operation["path"].replace("{allocation_id}", "missing-a").replace("{job_id}", "missing-j")
    with TestClient(create_app(data_dir=tmp_path, test_key="test-only")) as client:
        headers = {"X-Owner": "alice", "X-Test-Key": "test-only"}
        response = client.request(operation["method"], path, headers=headers)
    if path in {"/healthz", "/v1/version"}:
        assert response.status_code == 200
        assert response.json()["checkpoint"] == "CP2"
    else:
        assert response.status_code != 501
        assert response.status_code < 500


def test_health_does_not_claim_joint_checkpoint_or_real_execution(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        assert client.get("/healthz").json()["control_plane_ready"] is True
        assert client.get("/healthz").json()["checkpoint_passed"] is False
        capabilities = client.get("/v1/version").json()["capabilities"]
        assert capabilities["resource_ledger"] is True
        assert capabilities["checkpoint_recovery"] is True
        assert capabilities["real_gpu_execution"] is False
        assert capabilities["real_cpu_execution"] is False
