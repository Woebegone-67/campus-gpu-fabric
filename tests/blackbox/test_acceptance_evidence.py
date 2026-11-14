"""Receipt-reader regressions; temporary synthetic data is never product evidence."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from campus_fabric.contracts.models import AuthenticatedActor
from campus_fabric.workflows import acceptance_evidence as reader
from campus_fabric.workflows import devhost

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_MARKER = "private-fixture-owner-and-command-should-not-be-returned"


@pytest.fixture
def receipt_repo(tmp_path):
    """Use the real gate and real source/artifact hashes in an isolated Git repo."""
    for relative in ("configs/checkpoints.json", "configs/semantics.json",
                     "scripts/check_checkpoint.py"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    config = json.loads((tmp_path / "configs/checkpoints.json").read_text())
    for requirement in config["checkpoints"]["CP1"]["required_checks"]:
        target = tmp_path / requirement["validator_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Temporary parser-fixture validator; no actual product run.\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/product.py").write_text("version = 1\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "-c", "user.name=Receipt fixture",
        "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
        "-m", "Temporary receipt parser fixture",
    ], check=True, capture_output=True)
    return tmp_path


def synthetic_receipt(root):
    """Create a temporary parser fixture, not a claimed product acceptance run."""
    config = json.loads((root / "configs/checkpoints.json").read_text())
    gate = reader._gate_module(root)
    build = gate.current_build(root)
    artifact = root / "evidence/cp1/synthetic-parser-fixture.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Synthetic parser input only; no product behavior executed.\n")
    checks = []
    for requirement in config["checkpoints"]["CP1"]["required_checks"]:
        validator = root / requirement["validator_path"]
        checks.append({
            "id": requirement["id"], "target_id": requirement["target_id"], "status": "MET",
            "build": build, "expected": requirement["expected"],
            "evidence_kind": requirement["allowed_evidence_kinds"][0],
            "validator": {"path": requirement["validator_path"],
                          "sha256": gate.sha256_file(validator)},
            "input_version": "synthetic-parser-input-v1", "executor": PRIVATE_MARKER,
            "command": PRIVATE_MARKER,
            "actual": f"Synthetic parser fixture only. {PRIVATE_MARKER}",
            "observed_at": datetime.now(timezone.utc).isoformat(), "unresolved_issues": [],
            "evidence": [{"path": str(artifact.relative_to(root)),
                          "sha256": gate.sha256_file(artifact)}],
        })
    receipt = {"schema_version": "1.0.0", "checkpoint": "CP1", "status": "MET",
               "scope": "runtime_acceptance", "build": build, "checks": checks}
    path = root / "evidence/cp1/receipt.json"
    path.write_text(json.dumps(receipt))
    return receipt, artifact


def cp1_checks(root):
    checks, matches = reader.checkpoint_checks(root)
    return [check for check in checks if check.expected.startswith("CP1-")], matches


def test_missing_receipts_remain_not_run(receipt_repo):
    checks, matches = reader.checkpoint_checks(receipt_repo)
    assert checks and all(check.status == "NOT_RUN" for check in checks)
    assert all(check.actual is None for check in checks)
    assert matches is None


def test_valid_current_receipt_is_shown_without_private_receipt_prose(receipt_repo):
    synthetic_receipt(receipt_repo)
    checks, matches = reader.checkpoint_checks(receipt_repo)
    current = [check for check in checks if check.expected.startswith("CP1-")]
    future = [check for check in checks if not check.expected.startswith("CP1-")]
    assert len(current) == 7 and all(check.status == "MET" for check in current)
    assert all(check.status == "NOT_RUN" for check in future)
    assert matches is True
    assert PRIVATE_MARKER not in json.dumps([check.model_dump() for check in checks])
    assert all(check.evidence_uris == ("repo:evidence/cp1/receipt.json",) for check in current)
    assert all("不是本次重新执行测试" in check.actual for check in current)


def test_source_change_invalidates_a_previously_passing_receipt(receipt_repo):
    synthetic_receipt(receipt_repo)
    assert all(check.status == "MET" for check in cp1_checks(receipt_repo)[0])
    (receipt_repo / "src/product.py").write_text("version = 2\n")
    checks, matches = cp1_checks(receipt_repo)
    assert all(check.status == "NOT_MET" for check in checks)
    assert matches is False
    assert all("源码已变化" in check.actual for check in checks)


def test_artifact_content_change_is_checked_even_when_size_and_mtime_match(receipt_repo):
    _, artifact = synthetic_receipt(receipt_repo)
    assert all(check.status == "MET" for check in cp1_checks(receipt_repo)[0])
    info = artifact.stat()
    data = artifact.read_bytes()
    artifact.write_bytes(b"X" + data[1:])
    os.utime(artifact, ns=(info.st_atime_ns, info.st_mtime_ns))
    checks, matches = cp1_checks(receipt_repo)
    assert all(check.status == "NOT_MET" for check in checks)
    assert matches is True  # The source still matches; its execution artifact does not.
    assert all("摘要不一致" in check.actual for check in checks)


@pytest.mark.parametrize("payload", ["{", "null", "[]", '{"status":"MET"}',
                                     '{"checks":[{"id":[]}]}'])
def test_malformed_or_merely_labelled_receipt_cannot_crash_or_pass(receipt_repo, payload):
    path = receipt_repo / "evidence/cp1/receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload)
    checks, matches = cp1_checks(receipt_repo)
    assert all(check.status == "NOT_MET" for check in checks)
    assert matches is False


def test_missing_required_check_rejects_the_whole_checkpoint(receipt_repo):
    receipt, _ = synthetic_receipt(receipt_repo)
    receipt["checks"].pop()
    (receipt_repo / "evidence/cp1/receipt.json").write_text(json.dumps(receipt))
    checks, _ = cp1_checks(receipt_repo)
    assert all(check.status == "NOT_MET" for check in checks)


def test_receipt_changed_during_validation_cannot_return_met(receipt_repo, monkeypatch):
    receipt, _ = synthetic_receipt(receipt_repo)
    gate = reader._gate_module(receipt_repo)
    evaluate = gate.evaluate_checkpoint

    def swap_after_validation(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        replacement = copy.deepcopy(receipt)
        replacement["checks"][0]["status"] = "NOT_RUN"
        (receipt_repo / "evidence/cp1/receipt.json").write_text(json.dumps(replacement))
        return result

    monkeypatch.setattr(gate, "evaluate_checkpoint", swap_after_validation)
    monkeypatch.setattr(reader, "_gate_module", lambda root: gate)
    checks, matches = cp1_checks(receipt_repo)
    assert all(check.status == "NOT_MET" for check in checks)
    assert matches is False


def test_artifact_changed_after_initial_validation_cannot_return_met(receipt_repo, monkeypatch):
    _, artifact = synthetic_receipt(receipt_repo)
    receipt_path = receipt_repo / "evidence/cp1/receipt.json"
    receipt_before = receipt_path.read_bytes()
    gate = reader._gate_module(receipt_repo)
    build_before = gate.current_build(receipt_repo)
    evaluate = gate.evaluate_checkpoint
    first_validation = True

    def change_artifact_after_validation(*args, **kwargs):
        nonlocal first_validation
        result = evaluate(*args, **kwargs)
        if first_validation:
            assert result["status"] == "MET"
            first_validation = False
            artifact.write_text("Evidence changed after the gate initially accepted it.\n")
        return result

    monkeypatch.setattr(gate, "evaluate_checkpoint", change_artifact_after_validation)
    monkeypatch.setattr(reader, "_gate_module", lambda root: gate)
    checks, matches = cp1_checks(receipt_repo)
    assert gate.current_build(receipt_repo) == build_before
    assert receipt_path.read_bytes() == receipt_before
    assert evaluate(receipt_repo, "CP1")["status"] == "NOT_MET"
    assert all(check.status == "NOT_MET" for check in checks)
    assert all("摘要不一致" in check.actual for check in checks)
    assert matches is True  # Only evidence changed, not the source build.


def test_host_reads_registered_receipt_and_keeps_acceptance_admin_only(receipt_repo, monkeypatch):
    synthetic_receipt(receipt_repo)
    monkeypatch.setattr(devhost, "ROOT", receipt_repo)
    tokens = {
        "reader-admin": AuthenticatedActor(actor_id="reader-admin", role="administrator"),
        "reader-alice": AuthenticatedActor(actor_id="reader-alice", role="researcher",
                                           owner_id="alice"),
    }
    app = devhost.create_app(data_dir=receipt_repo / "runtime-test", auth_tokens=tokens,
                             worker_enabled=False)
    with TestClient(app) as client:
        denied = client.get("/x/v1/acceptance", headers={"Authorization": "Bearer reader-alice"})
        assert denied.status_code == 403
        response = client.get("/x/v1/acceptance", headers={"Authorization": "Bearer reader-admin"})
        assert response.status_code == 200, response.text
        current = [check for check in response.json()["items"]
                   if check["expected"].startswith("CP1-")]
        assert all(check["status"] == "MET" for check in current)
        assert response.json()["build_matches"] is True
        assert PRIVATE_MARKER not in response.text
        # Evidence mutation after the first HTTP read cannot reuse a cached MET.
        path = receipt_repo / "evidence/cp1/synthetic-parser-fixture.txt"
        path.write_text("Changed synthetic fixture, intentionally invalid evidence.\n")
        second = client.get("/x/v1/acceptance", headers={"Authorization": "Bearer reader-admin"})
        assert second.status_code == 200
        assert all(check["status"] == "NOT_MET" for check in second.json()["items"]
                   if check["expected"].startswith("CP1-"))
