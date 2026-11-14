import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from campus_fabric.app import create_app

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = json.loads((ROOT / "contracts/api_ownership.json").read_text())["operations"]


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda x: x["operation_id"])
def test_registered_route_does_not_masquerade_as_business_implementation(operation):
    path = operation["path"].replace("{allocation_id}", "cp0-a").replace("{job_id}", "cp0-j")
    with TestClient(create_app()) as client:
        response = client.request(operation["method"], path)
    if path in {"/healthz", "/v1/version"}:
        assert response.status_code == 200
        assert response.json()["checkpoint"] == "CP0"
    else:
        assert response.status_code == 501
        assert response.json()["error"] == "not_implemented"


def test_health_is_not_a_claim_of_a_ready_control_plane():
    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["control_plane_ready"] is False
        assert client.get("/v1/version").json()["capabilities"]["scheduler"] is False
