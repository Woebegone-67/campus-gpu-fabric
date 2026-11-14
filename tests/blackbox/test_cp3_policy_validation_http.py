"""CP3 presentation isolation and exact artifact bytes; no formal suite claims."""

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from campus_fabric.contracts import models as m
from campus_fabric.workflows import devhost
from campus_fabric.workflows.policy_validation import SealedPolicyValidation

ADMIN = {"Authorization": "Bearer cp3-reader-admin"}
OWNER = {"Authorization": "Bearer cp3-reader-owner", "X-Owner": "alice"}
TOKENS = {
    "cp3-reader-admin": m.AuthenticatedActor(actor_id="admin", role="administrator"),
    "cp3-reader-owner": m.AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice"),
}


def test_cp3_missing_report_is_not_run_and_cannot_mutate_instance(tmp_path, monkeypatch):
    reader = SealedPolicyValidation(tmp_path, tmp_path / "no-suite")
    monkeypatch.setattr(devhost, "SealedPolicyValidation", lambda root: reader)
    app = devhost.create_app(data_dir=tmp_path / "instance", auth_tokens=TOKENS,
                             worker_enabled=False)
    with TestClient(app) as client:
        before = client.get("/v1/metrics", headers=ADMIN).json()
        response = client.get("/x/v1/policy-validation", headers=ADMIN)
        assert response.status_code == 200
        assert response.json()["status"] == "NOT_RUN"
        assert response.json()["runs"] == [] and response.json()["analysis"] is None
        for path in ("/x/v1/policy-validation", "/x/v1/policy-validation/artifacts/manifest"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={**OWNER, "X-Role": "administrator"}).status_code == 403
            assert client.post(path, headers=ADMIN, json={"command": "run"}).status_code == 405
        after = client.get("/v1/metrics", headers=ADMIN).json()
        for key in ("version", "now", "counts"):
            assert after[key] == before[key]


def test_cp3_reader_failure_does_not_leak_old_report_or_change_raw_json(tmp_path, monkeypatch):
    class Reader:
        failed = False
        raw = b'{ "state": {"value":18446744073709551615}, "target_met":false }\n'

        def snapshot(self):
            if self.failed:
                raise ValueError("hash or build mismatch")
            return {"status": "ANALYZED", "analysis": {"target_met": False}}

        def artifact(self, name):
            if self.failed:
                raise ValueError("hash or build mismatch")
            if name != "analysis":
                raise KeyError(name)
            return self.raw

    reader = Reader()
    monkeypatch.setattr(devhost, "SealedPolicyValidation", lambda root: reader)
    app = devhost.create_app(data_dir=tmp_path, auth_tokens=TOKENS, worker_enabled=False)
    with TestClient(app) as client:
        assert client.get("/x/v1/policy-validation", headers=ADMIN).json()["analysis"]["target_met"] is False
        response = client.get("/x/v1/policy-validation/artifacts/analysis", headers=ADMIN)
        assert response.content == reader.raw
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get("/x/v1/policy-validation/artifacts/private.json", headers=ADMIN).status_code == 404
        reader.failed = True
        for path in ("/x/v1/policy-validation", "/x/v1/policy-validation/artifacts/analysis"):
            response = client.get(path, headers=ADMIN)
            assert response.status_code == 409
            assert response.json()["error"] == "invalid_evidence"
            assert "analysis" not in response.json()


def test_cp3_artifact_id_cannot_be_a_path_before_reading_local_files(tmp_path, monkeypatch):
    reader = SealedPolicyValidation(tmp_path)
    def unexpected():
        raise AssertionError("unsafe identifier should be rejected before disk access")
    monkeypatch.setattr(reader, "_load", unexpected)
    for name in ("../credentials.json", "/etc/passwd", "file:///tmp/data", "bad\\path", "a" * 181):
        try:
            reader.artifact(name)
        except KeyError:
            pass
        else:
            raise AssertionError(name)


def test_real_frozen_reader_download_and_hash_or_build_damage_fail_closed(tmp_path, monkeypatch):
    from experiments.cp3_suite import analyze_suite, digest, freeze_suite, write_json

    root = tmp_path / "source"
    checkout = Path(__file__).resolve().parents[2]
    for name in ("configs/cp1_inventory.json", "scripts/check_checkpoint.py"):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / name, destination)
    suite = root / "evidence/cp3/policy-suite"
    freeze_suite(root, suite, "development")
    analyze_suite(root, suite)
    reader = SealedPolicyValidation(root)
    monkeypatch.setattr(devhost, "SealedPolicyValidation", lambda source: reader)
    app = devhost.create_app(data_dir=tmp_path / "instance", auth_tokens=TOKENS, worker_enabled=False)
    with TestClient(app) as client:
        response = client.get("/x/v1/policy-validation", headers=ADMIN)
        assert response.status_code == 200, response.text
        assert response.json()["analysis"]["target_met"] is False
        assert response.json()["analysis"]["evidence_complete"] is False
        downloaded = client.get("/x/v1/policy-validation/artifacts/manifest", headers=ADMIN)
        assert downloaded.content == (suite / "manifest.json").read_bytes()
        # A registered file is checked again even when the prior report exists.
        index = reader._load()
        row = index["runs"][0]
        relative = f"runs/{row['run_id']}/summary.json"
        write_json(suite / relative, {"fixture": "not an executed experiment"})
        row["artifacts"]["summary"] = {"path": relative, "sha256": digest((suite / relative).read_bytes())}
        write_json(suite / "suite-index.json", index)
        (suite / relative).write_bytes(b"damaged")
        assert client.get("/x/v1/policy-validation", headers=ADMIN).status_code == 409
        row["artifacts"] = {}
        write_json(suite / "suite-index.json", index)
        (root / "scripts/source-change.py").write_text("# changed build\n")
        assert client.get("/x/v1/policy-validation", headers=ADMIN).status_code == 409
        assert client.get("/x/v1/policy-validation/artifacts/manifest", headers=ADMIN).status_code == 409
