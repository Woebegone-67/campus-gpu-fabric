"""The new launch fence must never turn ambiguity into a false stopped claim."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from test_cpu_reference import VERSION, command, wait_for

from campus_fabric.executors import cpu_reference, cpu_worker
from campus_fabric.executors.cpu_reference import (
    CpuReferenceConfig,
    CpuReferenceExecutor,
    ExecutorRejected,
)


class HostCrash(BaseException):
    pass


def reserved(tmp_path):
    executor = CpuReferenceExecutor(tmp_path / "executor", CpuReferenceConfig(total_steps=1))
    # Unit-level setup of an abandoned reservation; the CP3 gate independently
    # reaches this boundary with a real host process and SIGKILL.
    with patch.object(cpu_reference.subprocess, "Popen", side_effect=HostCrash):
        with pytest.raises(HostCrash):
            executor.dispatch(command(), owner_id="alice", logical_time=0)
    return executor


def only_record(executor):
    return next(iter(executor._read()["attempts"].values()))


def assert_quarantined(executor):
    events = executor.poll(logical_time=0, version=VERSION)
    assert not only_record(executor)["stop_confirmed"]
    assert not any(e.kind in {"failed", "stopped", "completed"} for e in events)
    with pytest.raises(ExecutorRejected, match="not safely stopped"):
        executor.dispatch(command(generation=2), owner_id="alice", logical_time=0)


def test_legacy_unstarted_launch_is_not_retired_by_new_protocol(tmp_path):
    executor = reserved(tmp_path)
    state = executor._read()
    next(iter(state["attempts"].values())).pop("launch_protocol")
    executor._save(state)
    assert_quarantined(CpuReferenceExecutor(executor.root, executor.config))


def test_worker_holding_launch_lock_without_ready_marker_remains_quarantined(tmp_path):
    executor = reserved(tmp_path)
    directory = executor._directory(only_record(executor))
    with (directory / "launch.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            assert_quarantined(CpuReferenceExecutor(executor.root, executor.config))
            assert not (directory / "launch-cancelled.json").exists()
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    # Only after the exclusive workload lock is released can recovery retire it.
    executor.poll(logical_time=0, version=VERSION)
    assert only_record(executor)["stop_confirmed"]


def test_known_live_pid_never_becomes_stop_confirmed_from_absent_ready(tmp_path):
    executor = reserved(tmp_path)
    state = executor._read()
    # A persisted PID that is alive but cannot be linked to evidence must remain
    # quarantined. The adapter must not signal or release this unrelated process.
    next(iter(state["attempts"].values()))["pid"] = os.getpid()
    executor._save(state)
    assert_quarantined(CpuReferenceExecutor(executor.root, executor.config))


def test_invalid_ready_evidence_does_not_authorize_unstarted_fence(tmp_path):
    executor = reserved(tmp_path)
    directory = executor._directory(only_record(executor))
    (directory / "started.json").write_text('{"corrupt": true}')
    assert_quarantined(executor)
    assert not (directory / "launch-cancelled.json").exists()


def test_stop_before_recovered_launch_cancels_delayed_worker(tmp_path):
    executor = reserved(tmp_path)
    executor.dispatch(command("stop"), owner_id="alice", logical_time=0)
    events = executor.poll(logical_time=0, version=VERSION)
    assert only_record(executor)["stop_confirmed"]
    assert any(e.kind == "failed" for e in events)
    directory = executor._directory(only_record(executor))
    process = subprocess.run(
        [sys.executable, cpu_worker.__file__, str(directory / "manifest.json")],
        capture_output=True,
        timeout=5,
    )
    assert process.returncode == 0
    assert not (directory / "started.json").exists()
    assert not (directory / "progress.json").exists()
    assert not (directory / "result.json").exists()


def test_completed_attempt_cannot_execute_again_from_same_manifest(tmp_path):
    executor = CpuReferenceExecutor(tmp_path / "executor", CpuReferenceConfig(total_steps=1))
    executor.dispatch(command(), owner_id="alice", logical_time=0)
    wait_for(executor, "completed")
    directory = executor._directory(only_record(executor))
    before = {name: (directory / name).read_bytes() for name in ("started.json", "result.json")}
    repeated = subprocess.run(
        [sys.executable, cpu_worker.__file__, str(directory / "manifest.json")],
        capture_output=True,
        timeout=5,
    )
    assert repeated.returncode != 0
    assert b"refusing a second launch" in repeated.stderr
    assert before == {name: (directory / name).read_bytes() for name in before}
    assert len(executor.inspect()["attempts"]) == 1
