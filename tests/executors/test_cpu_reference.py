from __future__ import annotations

import concurrent.futures
import json
import os
import signal
import time

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.executors.cpu_reference import (
    CpuReferenceConfig,
    CpuReferenceExecutor,
    ExecutorRejected,
)

VERSION = m.VersionStamp(epoch=0, revision=1)


def command(action="start", generation=1, dispatch_id=None, epoch=0, **kwargs):
    attempt = m.AttemptRef(
        intent_id="work-1",
        attempt_id=f"attempt-{generation}",
        generation=generation,
        allocation_id="allocation-1",
        job_id="job-1",
    )
    return m.ExecutorCommand(
        dispatch_id=dispatch_id or f"{action}-{generation}",
        version=m.VersionStamp(epoch=epoch, revision=1),
        attempt=attempt,
        action=action,
        mode="cpu_reference",
        job=m.JobCreateParameters(
            kind="job.create",
            allocation_id="allocation-1",
            image="unexecuted:latest",
            command=("false",),
        )
        if action in ("start", "resume")
        else None,
        **kwargs,
    )


def send(executor, value, owner="alice"):
    executor.dispatch(value, owner_id=owner, logical_time=0)


def wait_for(executor, kind, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = executor.poll(logical_time=0, version=VERSION)
        for event in events:
            executor.acknowledge(event.callback_id)
            if event.kind == kind:
                return event
        time.sleep(0.01)
    raise AssertionError(f"No {kind} callback; {executor.inspect()}")


@pytest.fixture
def processes():
    created = []
    yield created
    # Each PID comes from Popen owned by this test, never from user input.
    for executor in created:
        for handle in executor._handles.values():
            if handle.poll() is None:
                os.killpg(handle.pid, signal.SIGKILL)
            handle.wait(timeout=5)


def make_executor(tmp_path, processes, name="executor", **overrides):
    config = {"total_steps": 100_000, "batch_steps": 1_000, "batch_pause_seconds": 0.005}
    config.update(overrides)
    executor = CpuReferenceExecutor(tmp_path / name, CpuReferenceConfig(**config))
    processes.append(executor)
    return executor


def checkpoint_and_stop(executor):
    send(executor, command())
    wait_for(executor, "started")
    time.sleep(0.03)
    send(executor, command("checkpoint"))
    checkpoint = wait_for(executor, "checkpoint_reported").checkpoint
    assert checkpoint.verification_status == "verified"
    assert checkpoint.continuation_status == "not_attempted"
    assert not executor.inspect()["attempts"][0]["stop_confirmed"]
    with pytest.raises(ExecutorRejected, match="confirmed stopped"):
        send(executor, command("resume", 2, checkpoint=checkpoint))
    send(executor, command("stop"))
    wait_for(executor, "stopped")
    assert executor.inspect()["attempts"][0]["stop_confirmed"]
    return checkpoint


def test_actual_cpu_checkpoint_stop_and_resume_equal_uninterrupted(tmp_path, processes):
    executor = make_executor(tmp_path, processes)
    checkpoint = checkpoint_and_stop(executor)
    payload = executor.read_artifact(checkpoint.artifact_uri, owner_id="alice", epoch=0)
    assert "token" not in payload
    assert 0 < payload["state"]["step"] < executor.config.total_steps
    old_pid = executor.inspect()["attempts"][0]["pid"]
    assert executor._handles[executor.inspect()["attempts"][0]["key"]].poll() == 0

    # Reconstruct the adapter from its private journal before the resume.
    restored = CpuReferenceExecutor(executor.root, executor.config)
    processes.append(restored)
    send(restored, command("resume", 2, checkpoint=checkpoint))
    applied = wait_for(restored, "started")
    assert applied.checkpoint.continuation_status == "applied"
    assert applied.checkpoint.resumed_attempt_id == "attempt-2"
    completed = wait_for(restored, "completed")
    actual = restored.read_artifact(completed.result_uri, owner_id="alice", epoch=0)
    assert actual["pid"] != old_pid
    assert actual["state"] == restored.reference_result()
    assert restored.inspect()["attempts"][1]["exit_code"] == 0

    uninterrupted = make_executor(tmp_path, processes, "reference")
    send(uninterrupted, command())
    reference = wait_for(uninterrupted, "completed")
    expected = uninterrupted.read_artifact(reference.result_uri, owner_id="alice", epoch=0)
    assert actual["state"] == expected["state"]
    assert completed.physical_elapsed_seconds > 0


def test_persisted_unacknowledged_callbacks_keep_identity(tmp_path, processes):
    executor = make_executor(tmp_path, processes, total_steps=1)
    send(executor, command())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = executor.poll(logical_time=3, version=VERSION)
        if any(event.kind == "completed" for event in events):
            break
        time.sleep(0.01)
    else:
        pytest.fail("CPU process failed to complete")
    restored = CpuReferenceExecutor(executor.root, executor.config)
    again = restored.poll(logical_time=8, version=VERSION)
    assert again == events
    for event in again:
        restored.acknowledge(event.callback_id)
        restored.acknowledge(event.callback_id)
    assert restored.poll(logical_time=9, version=VERSION) == ()


def test_concurrent_duplicate_dispatch_spawns_exactly_one_process(tmp_path, processes):
    executor = make_executor(tmp_path, processes)
    second = CpuReferenceExecutor(executor.root, executor.config)
    processes.append(second)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: send(executor if i % 2 else second, command()), range(24)))
    assert len(executor.inspect()["attempts"]) == 1
    assert len(executor._handles) + len(second._handles) == 1
    with pytest.raises(ExecutorRejected, match="reused"):
        send(executor, command(), owner="bob")
    with pytest.raises(ExecutorRejected, match="already dispatched"):
        send(executor, command(dispatch_id="second-launch"))
    wait_for(executor, "completed")


@pytest.mark.parametrize(
    "attack", ["owner", "epoch", "job", "intent", "generation", "uri", "changed", "symlink"]
)
def test_resume_rejects_untrusted_or_wrong_identity_artifacts(tmp_path, processes, attack):
    executor = make_executor(tmp_path, processes)
    checkpoint = checkpoint_and_stop(executor)
    resume = command("resume", 2, checkpoint=checkpoint)
    owner = "alice"
    if attack == "owner":
        owner = "bob"
    elif attack == "epoch":
        resume = command("resume", 2, checkpoint=checkpoint, epoch=1)
    elif attack in ("job", "intent", "generation"):
        field, value = {
            "job": ("job_id", "job-2"),
            "intent": ("intent_id", "work-2"),
            "generation": ("generation", 1),
        }[attack]
        resume = resume.model_copy(
            update={"attempt": resume.attempt.model_copy(update={field: value})}
        )
    elif attack == "uri":
        checkpoint = checkpoint.model_copy(update={"artifact_uri": "file:///etc/passwd"})
        resume = command("resume", 2, checkpoint=checkpoint)
    else:
        journal = json.loads((executor.root / "journal.json").read_text())
        path = executor.root / journal["artifacts"][checkpoint.artifact_uri]["path"]
        if attack == "changed":
            artifact = json.loads(path.read_text())
            artifact["payload"]["state"]["value"] = 1
            path.write_text(json.dumps(artifact))
        else:
            copied = tmp_path / "forged.json"
            copied.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(copied)
    with pytest.raises((ExecutorRejected, ValueError)):
        send(executor, resume, owner)
    assert len(executor.inspect()["attempts"]) == 1


def test_submitted_command_and_environment_are_never_executed(tmp_path, processes):
    executor = make_executor(tmp_path, processes, total_steps=1)
    sentinel = tmp_path / "must-not-exist"
    original = command()
    job = original.job.model_copy(
        update={
            "image": "malicious:ignored",
            "command": ("sh", "-c", f"touch {sentinel}"),
            "env": (m.KeyValue(key="PYTHONSTARTUP", value=str(sentinel)),),
        }
    )
    send(executor, original.model_copy(update={"job": job}))
    completed = wait_for(executor, "completed")
    assert not sentinel.exists()
    assert (
        executor.read_artifact(completed.result_uri, owner_id="alice", epoch=0)["state"]
        == executor.reference_result()
    )


def test_stop_frozen_process_never_confirms_before_observed_exit(tmp_path, processes):
    executor = make_executor(tmp_path, processes, stop_timeout_seconds=0.03)
    send(executor, command())
    wait_for(executor, "started")
    record = executor.inspect()["attempts"][0]
    handle = executor._handles[record["key"]]
    os.kill(handle.pid, signal.SIGSTOP)
    send(executor, command("stop"))
    immediate = executor.poll(logical_time=0, version=VERSION)
    assert all(event.kind != "stopped" for event in immediate)
    assert not executor.inspect()["attempts"][0]["stop_confirmed"]
    wait_for(executor, "stopped")
    assert handle.poll() is not None
    assert executor.inspect()["attempts"][0]["stop_confirmed"]


def test_restart_recognizes_running_and_cooperative_stop(tmp_path, processes):
    executor = make_executor(tmp_path, processes)
    send(executor, command())
    wait_for(executor, "started")
    restored = CpuReferenceExecutor(executor.root, executor.config)
    send(restored, command("probe"))
    wait_for(restored, "alive")
    send(restored, command("stop"))
    wait_for(restored, "stopped")
    assert restored.inspect()["attempts"][0]["stop_confirmed"]


def test_restarted_adapter_cannot_fake_stop_of_nonresponsive_process(tmp_path, processes):
    executor = make_executor(tmp_path, processes, stop_timeout_seconds=0.01)
    send(executor, command())
    wait_for(executor, "started")
    record = executor.inspect()["attempts"][0]
    os.kill(record["pid"], signal.SIGSTOP)
    restored = CpuReferenceExecutor(executor.root, executor.config)
    send(restored, command("stop"))
    unknown = wait_for(restored, "unknown")
    assert unknown.kind == "unknown"
    assert not restored.inspect()["attempts"][0]["stop_confirmed"]
    assert executor._handles[record["key"]].poll() is None


def test_new_epoch_cannot_launch_while_old_process_not_confirmed_stopped(tmp_path, processes):
    executor = make_executor(tmp_path, processes)
    send(executor, command())
    wait_for(executor, "started")
    with pytest.raises(ExecutorRejected, match="old-epoch"):
        send(executor, command(generation=2, epoch=1))


def test_checkpoint_and_stop_are_durable_ordered_commands(tmp_path, processes):
    executor = make_executor(tmp_path, processes)
    send(executor, command())
    wait_for(executor, "started")
    send(executor, command("checkpoint", dispatch_id="save-1"))
    send(executor, command("checkpoint", dispatch_id="save-2"))
    send(executor, command("stop"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = executor.poll(logical_time=0, version=VERSION)
        if any(event.kind == "stopped" for event in events):
            break
        time.sleep(0.01)
    else:
        pytest.fail("Ordered stop was not observed")
    assert [event.kind for event in events if event.kind != "progress"] == [
        "checkpoint_reported",
        "checkpoint_reported",
        "stopped",
    ]
    checkpoints = [event.checkpoint for event in events if event.kind == "checkpoint_reported"]
    assert checkpoints[0].artifact_uri != checkpoints[1].artifact_uri
    assert "token" not in json.dumps(executor.inspect())


def test_unsigned_progress_is_not_promoted_to_trusted_observation(tmp_path, processes):
    executor = make_executor(tmp_path, processes, total_steps=1000000)
    send(executor, command())
    wait_for(executor, "started")
    record = executor.inspect()["attempts"][0]
    handle = executor._handles[record["key"]]
    os.killpg(handle.pid, signal.SIGSTOP)
    path = executor.root / "attempts" / record["key"] / "progress.json"
    path.write_text(json.dumps({"state": {"step": 999999}, "observed_monotonic_ns": time.monotonic_ns()}))
    events = executor.poll(logical_time=2, version=VERSION)
    assert any(event.kind == "unknown" for event in events)
    assert not any(event.kind == "progress" and event.observed_at == 2 for event in events)
    assert not executor.inspect()["attempts"][0]["stop_confirmed"]


def test_interrupted_control_delivery_replays_from_journal(tmp_path, processes, monkeypatch):
    from campus_fabric.executors import cpu_reference

    executor = make_executor(tmp_path, processes)
    send(executor, command())
    wait_for(executor, "started")
    original = cpu_reference.atomic_json

    def interrupted(path, value):
        if path.name.startswith("control-"):
            raise OSError("injected pre-control crash")
        original(path, value)

    monkeypatch.setattr(cpu_reference, "atomic_json", interrupted)
    with pytest.raises(OSError):
        send(executor, command("checkpoint"))
    monkeypatch.setattr(cpu_reference, "atomic_json", original)
    # The command journal was committed even though no control file was sent.
    restored = CpuReferenceExecutor(executor.root, executor.config)
    checkpoint = wait_for(restored, "checkpoint_reported")
    assert checkpoint.checkpoint.verification_status == "verified"
    send(restored, command("stop"))
    wait_for(restored, "stopped")


def test_invalid_result_is_failure_not_success_or_poll_crash(tmp_path, processes):
    executor = make_executor(tmp_path, processes, total_steps=1)
    send(executor, command())
    record = executor.inspect()["attempts"][0]
    executor._handles[record["key"]].wait(timeout=5)
    result = executor.root / "attempts" / record["key"] / "result.json"
    data = json.loads(result.read_bytes())
    data["payload"]["state"]["value"] += 1
    result.write_text(json.dumps(data))
    failed = wait_for(executor, "failed")
    assert failed.result_uri is None
    assert executor.inspect()["attempts"][0]["stop_confirmed"]


def test_launch_failure_reports_no_running_process(tmp_path, processes, monkeypatch):
    from campus_fabric.executors import cpu_reference

    executor = make_executor(tmp_path, processes)

    def refuse(*args, **kwargs):
        raise OSError("injected launch failure")

    monkeypatch.setattr(cpu_reference.subprocess, "Popen", refuse)
    send(executor, command())
    event = wait_for(executor, "failed")
    assert event.error.code == "execution_unconfirmed"
    assert executor.inspect()["attempts"][0]["pid"] is None
    assert executor.inspect()["attempts"][0]["stop_confirmed"]
