"""Exercise worker and controller safety without cloning an uncommitted checkout."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from experiments import cp4_clean_deploy as clean


def test_existing_empty_output_is_not_shared_or_overwritten(tmp_path):
    existing = tmp_path / "already-owned"
    existing.mkdir()
    with pytest.raises(ValueError, match="overwrite"):
        clean.fresh_output(existing)


@pytest.mark.parametrize("worker", [True, False])
def test_optimized_python_cannot_issue_evidence_or_clone(tmp_path, worker):
    output = tmp_path / "must-not-be-created"
    args = [sys.executable, "-O", "-m", "experiments.cp4_clean_deploy", "--output", str(output)]
    args += ["--worker"] if worker else ["--uv", sys.executable]
    response = subprocess.run(args, cwd=clean.ROOT, capture_output=True, text=True, timeout=8,
        env={**clean.clean_environment(), "PYTHONPATH": str(clean.ROOT)})
    assert response.returncode != 0
    assert response.stderr.splitlines()[-1] == (
        "RuntimeError: Acceptance validation requires Python assertions; remove -O/-OO and "
        "unset PYTHONOPTIMIZE before running validators or the acceptance host."
    )
    assert not output.exists()


def test_release_manifest_binds_deploy_files_and_rejects_symlinks(tmp_path):
    for name in clean.RELEASE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    file = deploy / "start.sh"
    file.write_text("exit 0\n")
    before = clean.release_manifest(tmp_path)
    build = {"git_commit": "a" * 40, "source_sha256": "b" * 64, "source_file_count": 7}
    file.write_text("exit 1\n")
    with pytest.raises(ValueError, match="Deployment"):
        clean.verify_identity(build, build, before, clean.release_manifest(tmp_path))
    (deploy / "linked").symlink_to(file)
    with pytest.raises(ValueError, match="symlinks"):
        clean.release_manifest(tmp_path)
    with pytest.raises(ValueError, match="commit the candidate"):
        clean.verify_identity(build, {**build, "source_sha256": "c" * 64}, before, before)


def test_environment_and_command_logs_do_not_forward_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_INDEX_URL", "https://user:secret@example.invalid")
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-forward")
    monkeypatch.setenv("VIRTUAL_ENV", "/unrelated/environment")
    env = clean.clean_environment()
    assert all(key not in env for key in ("UV_INDEX_URL", "GITHUB_TOKEN", "VIRTUAL_ENV"))
    commands = clean.Commands(tmp_path, 10)
    row = commands.run("log-control", [sys.executable, "-c",
        "print('https://user:secret@example.invalid/pkg'); print('Authorization: Bearer hidden')"],
        cwd=tmp_path, env=env, timeout=3)
    raw = (tmp_path / row["log"]["path"]).read_text()
    assert "secret" not in raw and "hidden" not in raw
    assert "<redacted>" in raw
    saved = (tmp_path / "commands.json").read_text()
    assert "secret" not in saved and "hidden" not in saved


def test_controller_rejects_dirty_source_before_install_without_real_cloning(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    build = {"git_commit": "a" * 40, "source_sha256": "b" * 64, "source_file_count": 7}
    monkeypatch.setattr(clean, "ROOT", source)
    monkeypatch.setattr(clean, "current_build", lambda root: build if root == source else {
        **build, "source_sha256": "c" * 64})
    monkeypatch.setattr(clean, "release_manifest", lambda root: {"files": [], "fixture": True})
    calls = []

    def no_execution(self, name, argv, **kwargs):
        calls.append(name)
        # Deliberately no subprocess and no clone; only the identity failure path.
        return {"status": "MET"}

    monkeypatch.setattr(clean.Commands, "run", no_execution)
    report = clean.run_rehearsal(tmp_path / "rejected", sys.executable)
    assert calls == ["clone", "detach-head"]
    assert report["status"] == report["checks"][0]["status"] == "NOT_MET"
    assert all(row["status"] == "NOT_RUN" for row in report["checks"][1:])
    assert "commit the candidate first" in report["unresolved_issues"][0]
    assert report["formal_cp4_clean_deploy"] == "NOT_RUN"


@pytest.mark.skipif(os.name != "posix", reason="Owned process groups use POSIX signals")
def test_command_deadline_fails_and_reaps_its_owned_process_group(tmp_path):
    commands = clean.Commands(tmp_path, 5)
    code = "import os,time; print(os.getpid(), flush=True); time.sleep(30)"
    with pytest.raises(clean.CommandFailed, match="timed_out=True"):
        commands.run("timeout-control", [sys.executable, "-c", code], cwd=tmp_path,
                     env=clean.clean_environment(), timeout=.2)
    row = commands.records[0]
    assert row["status"] == "NOT_MET" and row["timed_out"] is True
    pid = int((tmp_path / row["log"]["path"]).read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_backend_worker_executes_two_tcp_journeys_without_claiming_clean_clone(tmp_path):
    report = clean.run_worker(tmp_path / "worker")
    assert [row["status"] for row in report["checks"]] == ["MET", "MET"], report
    assert report["status"] == ("MET" if report["build"] == report["build_at_finish"] else "NOT_MET")
    assert report["formal_cp4_clean_deploy"] == report["human_acceptance"] == "NOT_RUN"
    raw = (tmp_path / "worker/http_history.json").read_bytes()
    assert clean.digest(raw) == report["evidence"][0]["sha256"]
    history = json.loads(raw)
    assert history["credentials_recorded"] is False
    assert any(row["kind"] == "process_stopped" for row in history["records"])
    with pytest.raises(ValueError, match="overwrite"):
        clean.run_worker(tmp_path / "worker")
