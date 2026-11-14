"""CP3 presentation isolation and exact artifact bytes; no formal suite claims."""

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
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


def test_experiment_reads_do_not_wait_for_unrelated_checkpoint_validation(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    gate_calls = []

    def slow_gate(_root):
        gate_calls.append(True)
        entered.set()
        if not release.wait(15):
            raise AssertionError("Test did not release checkpoint validation")
        return (), False

    reader = SealedPolicyValidation(tmp_path, tmp_path / "no-suite")
    monkeypatch.setattr(devhost, "SealedPolicyValidation", lambda root: reader)
    monkeypatch.setattr(devhost, "checkpoint_checks", slow_gate)
    app = devhost.create_app(data_dir=tmp_path / "instance", auth_tokens=TOKENS,
                             worker_enabled=False)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        before = client.get("/v1/metrics", headers=ADMIN).json()
        checking = pool.submit(client.get, "/x/v1/acceptance", headers=ADMIN)
        assert entered.wait(3)
        try:
            for path, expected in (
                ("/x/v1/experiments?limit=25", 200),
                ("/x/v1/experiments/catalog", 200),
                ("/x/v1/experiments/unknown-run", 404),
                ("/x/v1/policy-validation", 200),
            ):
                response = pool.submit(client.get, path, headers=ADMIN).result(timeout=3)
                assert response.status_code == expected, response.text
            assert not checking.done()
            assert len(gate_calls) == 1
            for path in ("/x/v1/experiments", "/x/v1/experiments/catalog",
                         "/x/v1/policy-validation"):
                assert client.get(path, headers=OWNER).status_code == 403
            assert client.get("/x/v1/experiments/unknown-run", headers=OWNER).status_code == 404
        finally:
            release.set()
        assert checking.result(timeout=3).status_code == 200
        after = client.get("/v1/metrics", headers=ADMIN).json()
        for key in ("version", "now", "counts"):
            assert after[key] == before[key]


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


def test_explicit_historical_reader_pins_original_index_and_checks_download_bytes(tmp_path, monkeypatch):
    from experiments.cp3_suite import analyze_suite, digest, freeze_suite, write_json

    checkout = Path(__file__).resolve().parents[2]
    original = tmp_path / "original"
    for name in ("configs/cp1_inventory.json", "scripts/check_checkpoint.py"):
        destination = original / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / name, destination)
    suite = tmp_path / "archive"
    freeze_suite(original, suite, "development")
    analyze_suite(original, suite)
    index = json.loads((suite / "suite-index.json").read_bytes())
    row = index["runs"][0]
    relative = f"runs/{row['run_id']}/summary.json"
    raw = b'{ "large":18446744073709551615, "fixture":true }\n'
    (suite / relative).parent.mkdir(parents=True, exist_ok=True)
    (suite / relative).write_bytes(raw)
    row["artifacts"]["summary"] = {"path": relative, "sha256": digest(raw)}
    write_json(suite / "suite-index.json", index)
    seal = digest((suite / "manifest.json").read_bytes())
    index_seal = digest((suite / "suite-index.json").read_bytes())
    with pytest.raises(ValueError, match="build differs"):
        SealedPolicyValidation(checkout, suite).snapshot()
    with pytest.raises(ValueError, match="manifest and index"):
        SealedPolicyValidation(checkout, suite, historical_manifest_sha256=seal)
    with pytest.raises(ValueError, match="manifest SHA256"):
        SealedPolicyValidation(checkout, suite, historical_manifest_sha256="0" * 64,
                               historical_index_sha256=index_seal).snapshot()
    with pytest.raises(ValueError, match="configured SHA256"):
        SealedPolicyValidation(checkout, suite, historical_manifest_sha256=seal,
                               historical_index_sha256="0" * 64).snapshot()
    reader = SealedPolicyValidation(checkout, suite, historical_manifest_sha256=seal,
                                    historical_index_sha256=index_seal)
    monkeypatch.setattr(devhost, "SealedPolicyValidation", lambda root: reader)
    app = devhost.create_app(data_dir=tmp_path / "instance", auth_tokens=TOKENS, worker_enabled=False)
    with TestClient(app) as client:
        response = client.get("/x/v1/policy-validation", headers=ADMIN)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["historical"] is True
        assert result["build"] == index["build"] != result["serving_build"]
        assert result["current_build_acceptance_eligible"] is False
        assert result["integrity_scope"] == "manifest_index_analysis; artifacts_verified_on_download"
        path = f"/x/v1/policy-validation/artifacts/{row['run_id']}:summary"
        assert client.get(path, headers=ADMIN).content == raw
        assert client.get(path, headers=OWNER).status_code == 403
        assert client.get("/x/v1/policy-validation", headers=OWNER).status_code == 403
        (suite / relative).write_bytes(b"damaged")
        # Historical display does not pretend to rescan every large artifact.
        assert client.get("/x/v1/policy-validation", headers=ADMIN).status_code == 200
        assert client.get(path, headers=ADMIN).status_code == 409
        # Even changing both the analysis and its digest cannot replace the old report.
        analysis = json.loads((suite / "analysis.json").read_bytes())
        analysis["target_met"] = True
        write_json(suite / "analysis.json", analysis)
        index["analysis"]["sha256"] = digest((suite / "analysis.json").read_bytes())
        write_json(suite / "suite-index.json", index)
        assert client.get("/x/v1/policy-validation", headers=ADMIN).status_code == 409
        assert client.get("/x/v1/policy-validation/artifacts/analysis", headers=ADMIN).status_code == 409


@pytest.mark.parametrize("configured", [
    {"FABRIC_CP3_ARCHIVE_SUITE": "/missing"},
    {"FABRIC_CP3_ARCHIVE_MANIFEST_SHA256": "a" * 64},
    {"FABRIC_CP3_ARCHIVE_INDEX_SHA256": "b" * 64},
])
def test_historical_config_requires_directory_and_both_digests(monkeypatch, configured):
    for key in ("FABRIC_CP3_ARCHIVE_SUITE", "FABRIC_CP3_ARCHIVE_MANIFEST_SHA256",
                "FABRIC_CP3_ARCHIVE_INDEX_SHA256"):
        monkeypatch.delenv(key, raising=False)
    for key, value in configured.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="archive directory, manifest and index"):
        devhost.policy_validation_reader()
