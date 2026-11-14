"""Actual TCP challenges and fail-closed partial evidence, never human sign-off."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiments import cp4_reliability as cp4


def test_fixed_challenges_execute_over_owned_tcp_and_retain_evidence(tmp_path):
    result = cp4.run_suite(tmp_path / "complete")
    assert [row["id"] for row in result["checks"]] == list(cp4.CASE_IDS)
    assert all(row["status"] == "MET" for row in result["checks"]), result["checks"]
    # Concurrent source edits correctly invalidate the receipt, not case observations.
    assert result["status"] == ("MET" if result["build_matches"] else "NOT_MET")
    assert result["full_cp4_reliability"] == result["human_acceptance"] == "NOT_RUN"
    for row in result["checks"]:
        ref = row["evidence"][0]
        raw = (tmp_path / "complete" / ref["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
        history = json.loads(raw)
        assert history["credentials_recorded"] is False
        assert any(event["kind"] == "process_started" for event in history["records"])
        assert any(event["kind"] == "process_stopped" for event in history["records"])
        assert any(event["kind"] == "http" for event in history["records"])
        # Public error prose may explain Bearer authentication. Credentials are
        # never logged as request headers, and the runner checks actual tokens.
        for event in history["records"]:
            assert not {"Authorization", "X-Test-Key"} & set(event.get("header_overrides", {}))


def test_failed_and_omitted_cases_cannot_be_upgraded_to_met(tmp_path, monkeypatch):
    def failed(h):
        h.request("GET", "/v1/capacity", expected=200)
        raise AssertionError("deliberate negative control " + h.tokens["a"])

    monkeypatch.setitem(cp4.HANDLERS, "owner-isolation", failed)
    result = cp4.run_suite(tmp_path / "failed", selected=["owner-isolation"])
    assert result["status"] == "NOT_MET"
    assert result["checks"][0]["status"] == "NOT_MET"
    assert "deliberate negative control <redacted>" in result["checks"][0]["failure"]
    assert all(row["status"] == "NOT_RUN" and not row["evidence"] for row in result["checks"][1:])
    assert result["unresolved_issues"]


def test_evidence_directory_is_never_overwritten_and_empty_selection_is_not_run(tmp_path):
    output = tmp_path / "preserved"
    output.mkdir()
    marker = output / "old-negative-result.json"
    marker.write_text('{"status":"NOT_MET"}')
    with pytest.raises(ValueError, match="overwrite"):
        cp4.run_suite(output)
    assert marker.read_text() == '{"status":"NOT_MET"}'
    result = cp4.run_suite(tmp_path / "unexecuted", selected=[])
    assert result["status"] == "NOT_RUN"
    assert all(row["status"] == "NOT_RUN" for row in result["checks"])


@pytest.mark.parametrize("mode", ["command_flag", "environment"])
def test_optimized_interpreter_is_rejected_before_creating_evidence(tmp_path, mode):
    output = tmp_path / "optimized"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONOPTIMIZE"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable]
    if mode == "command_flag":
        command.append("-O")
    else:
        env["PYTHONOPTIMIZE"] = "1"
    command.extend(["-c", "import sys; from pathlib import Path; "
                    "from experiments.cp4_reliability import run_suite; "
                    "run_suite(Path(sys.argv[1]), selected=[])", str(output)])
    result = subprocess.run(command, cwd=cp4.ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert result.stderr.splitlines()[-1] == (
        "RuntimeError: Acceptance validation requires Python assertions; remove -O/-OO and "
        "unset PYTHONOPTIMIZE before running validators or the acceptance host."
    )
    assert not output.exists()


def test_existing_empty_directory_and_concurrent_writers_are_rejected(tmp_path):
    existing = tmp_path / "empty"
    existing.mkdir()
    with pytest.raises(ValueError, match="overwrite"):
        cp4.run_suite(existing, selected=[])
    assert list(existing.iterdir()) == []

    output = tmp_path / "exclusive"
    barrier = threading.Barrier(2)

    def writer(_):
        barrier.wait(timeout=5)
        try:
            cp4.run_suite(output, selected=[])
            return "created"
        except ValueError as exc:
            assert "overwrite" in str(exc)
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(writer, range(2)))
    assert sorted(outcomes) == ["created", "rejected"]
    assert json.loads((output / "receipt.json").read_text())["checks"]
