import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from campus_fabric.api.queries import make_kernel_services, register_queries
from campus_fabric.app import create_app
from campus_fabric.workflows import reclaim_validation as report


@pytest.fixture
def suite(tmp_path, monkeypatch):
    directory = tmp_path / "report"
    directory.mkdir()
    monkeypatch.setattr(report, "SUITE", directory)
    monkeypatch.setattr(report, "ROOT", tmp_path)
    planner = tmp_path / "src/campus_fabric/policies/reclaim.py"
    planner.parent.mkdir(parents=True)
    planner.write_text("# test planner\n")
    protocol = b'{"case":"bounded"}\n'
    (directory / "protocol.json").write_bytes(protocol)
    body = {"schema_version": 1, "summary": [], "runs": [],
            "protocol_sha256": sha256(protocol).hexdigest(),
            "planner_sha256": sha256(planner.read_bytes()).hexdigest()}
    content = (json.dumps(body) + "\n").encode()
    (directory / "report.json").write_bytes(content)
    (directory / "manifest.json").write_text(json.dumps({"schema_version": 1, "artifacts": {
        key: {"path": filename, "sha256": sha256((directory / filename).read_bytes()).hexdigest()}
        for key, filename in report.FILES.items()
    }}))
    return directory, content, planner


@pytest.fixture
def client(tmp_path, suite):
    app = create_app(data_dir=tmp_path / "data", auth_tokens={
        "admin-token": {"actor_id": "admin", "role": "administrator"},
        "member-token": {"actor_id": "alice", "role": "member", "owner_id": "alice"},
    })
    register_queries(app, make_kernel_services(app.state.kernel, app.state.identity,
        clock_origin="2026-01-01T00:00:00Z"), replace_existing=True)
    with TestClient(app) as connection:
        yield connection


ADMIN = {"Authorization": "Bearer admin-token"}
URL = "/x/v1/reclaim-validation"


def test_report_is_admin_only_and_download_preserves_evidence_bytes(client, suite):
    _, content, planner = suite
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"Authorization": "Bearer member-token"}).status_code == 403
    response = client.get(URL, headers=ADMIN)
    assert response.status_code == 200 and response.json()["planner_matches_current"]
    download = client.get(URL + "/artifacts/report", headers=ADMIN)
    assert download.content == content and download.headers["cache-control"] == "no-store"
    assert client.get(URL + "/artifacts/unregistered", headers=ADMIN).status_code == 404
    planner.write_text("# changed planner\n")
    assert client.get(URL, headers=ADMIN).json()["planner_matches_current"] is False


def test_corrupt_or_symlink_report_fails_closed(client, suite):
    directory, content, _ = suite
    path = directory / "report.json"
    path.write_bytes(content + b" ")
    assert client.get(URL, headers=ADMIN).status_code == 409
    assert client.get(URL + "/artifacts/report", headers=ADMIN).status_code == 409
    path.unlink()
    other = directory / "outside.json"
    other.write_bytes(content)
    path.symlink_to(other)
    assert client.get(URL, headers=ADMIN).status_code == 409
