#!/usr/bin/env python3
"""CP3 deterministic SIGKILL windows against the actual CPU adapter and kernel.

Private child entry points wrap production calls with filesystem barriers. They
are never imported by the app and add no HTTP endpoint or production switch.
The control plane is reconstructed from its persisted state after each kill.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/concurrency"))
from check_checkpoint import current_build  # noqa: E402
from test_cp3_transaction_windows import OPERATIONS, WINDOWS, exercise  # noqa: E402

from campus_fabric.contracts import models as m  # noqa: E402
from campus_fabric.domain.kernel import Kernel  # noqa: E402
from campus_fabric.executors import cpu_files, cpu_reference, cpu_worker  # noqa: E402
from campus_fabric.executors.cpu_reference import (  # noqa: E402
    CpuReferenceConfig,
    CpuReferenceExecutor,
)
from campus_fabric.executors.driver import SYSTEM, CpuDriver  # noqa: E402

CONFIG = CpuReferenceConfig(total_steps=200_000, batch_steps=1000, batch_pause_seconds=0.01)
ALICE = m.AuthenticatedActor(actor_id="cp3-alice", role="researcher", owner_id="alice")
TEST = m.AuthenticatedActor(actor_id="cp3-test", role="test_driver")
VERSION = m.VersionStamp(epoch=0, revision=1)


def eventually(fn, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError(f"Timed out: {fn}")


def barrier(directory, name):
    directory = Path(directory)
    cpu_files.atomic_json(directory / (name + ".ready"), {"pid": os.getpid(), "phase": name})
    eventually(lambda: (directory / (name + ".release")).exists(), timeout=60)


def child_worker(manifest, directory, phase):
    if phase == "delayed_launch":
        barrier(directory, "worker_before_launch_lock")
    if phase == "inside_launch":
        original_run = cpu_worker._run_locked

        def run_locked(path):
            barrier(directory, "worker_inside_launch_lock")
            return original_run(path)

        cpu_worker._run_locked = run_locked
    if phase == "unpublished_checkpoint":
        original = cpu_files.os.replace

        def replace(source, destination):
            if Path(destination).name.startswith("checkpoint-"):
                # atomic_json has flushed/fsynced the complete temp file, but no
                # checkpoint filename is visible to the reader at this boundary.
                barrier(directory, "worker_checkpoint_before_publish")
            return original(source, destination)

        cpu_files.os.replace = replace
    cpu_worker.run(Path(manifest))


def child_host(root, command_path, directory, phase):
    directory = Path(directory)
    original_popen = cpu_reference.subprocess.Popen
    original_save = CpuReferenceExecutor._save
    original_atomic = cpu_reference.atomic_json

    def save(self, state):
        original_save(self, state)
        if phase == "reserved" and any(r["pid"] is None for r in state["attempts"].values()):
            barrier(directory, "host_after_reservation")

    def popen(args, **kwargs):
        worker_phase = (
            phase
            if phase in {"delayed_launch", "inside_launch", "unpublished_checkpoint"}
            else "normal"
        )
        manifest = Path(args[-1])
        process = original_popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                str(manifest),
                str(directory),
                worker_phase,
            ],
            **kwargs,
        )
        cpu_files.atomic_json(directory / "created-worker.json", {"pid": process.pid})
        if phase == "after_launch":
            eventually(lambda: (manifest.parent / "started.json").exists())
            barrier(directory, "host_after_popen_before_pid_commit")
        elif phase in {"delayed_launch", "inside_launch"}:
            worker_gate = (
                "worker_before_launch_lock"
                if phase == "delayed_launch"
                else "worker_inside_launch_lock"
            )
            eventually(lambda: (directory / (worker_gate + ".ready")).exists())
            barrier(directory, "host_after_popen_before_pid_commit")
        return process

    def atomic(path, value):
        if phase == "control_committed" and path.name.startswith("control-"):
            barrier(directory, "host_before_control_delivery")
        return original_atomic(path, value)

    CpuReferenceExecutor._save = save
    cpu_reference.subprocess.Popen = popen
    cpu_reference.atomic_json = atomic
    executor = CpuReferenceExecutor(root, CONFIG)
    command = m.ExecutorCommand.model_validate_json(Path(command_path).read_text())
    executor.dispatch(command, owner_id="alice", logical_time=0)
    cpu_files.atomic_json(directory / "host-returned.json", {"returned": True})


class CrashSuite:
    def __init__(self, directory):
        self.root = Path(directory)
        self.history = []
        self.checks = []
        self.hosts = []
        self.workers = []
        self.drivers = []
        self.sequence = 0

    def check(self, name, observed):
        self.checks.append({"id": name, "status": "PASS", "observed": observed})
        print("PASS", name, flush=True)

    def setup(self, name):
        directory = self.root / name
        directory.mkdir()
        cap = m.ResourceAmounts(
            cpu_millicores=2000, memory_mib=1024, gpu_count=1, gpu_memory_mib=8192
        )
        node = m.NodeInventory(
            node_id="node",
            contributor_owner_id="supplier",
            state="Ready",
            capacity=cap,
            available=cap,
            observed_at=0,
            devices=(
                m.GPUDevice(
                    device_id="gpu",
                    physical_gpu_id="gpu",
                    model="ref",
                    memory_mib=8192,
                    state="available",
                ),
            ),
        )
        kernel = Kernel(directory / "state.db", inventory=(node,), execution_mode="cpu_reference")
        a = self.commit(
            kernel,
            m.AllocationCreateParameters(
                kind="allocation.create",
                owner_id="alice",
                ttl_seconds=300,
                preemption_mode="checkpoint",
                checkpoint_timeout_seconds=10,
                request=m.ResourceRequest(
                    cpu_millicores=1000, memory_mib=256, gpu=m.GPURequest(count=1)
                ),
            ),
        )
        aid = next(r.entity_id for r in a.objects if r.kind == "allocation")
        j = self.commit(
            kernel,
            m.JobCreateParameters(
                kind="job.create",
                allocation_id=aid,
                image="never-executed",
                command=("never-executed",),
                timeout_seconds=200,
            ),
        )
        jid = next(r.entity_id for r in j.objects if r.kind == "job")
        return directory, kernel, aid, jid

    def commit(self, kernel, parameters, actor=ALICE):
        self.sequence += 1
        receipt = kernel.commit(
            m.CommandEnvelope(
                request_id=f"cp3-{self.sequence}",
                actor=actor,
                operation=parameters.kind,
                parameters=parameters,
            )
        )
        self.history.append(
            {
                "kind": "kernel_commit",
                "operation": parameters.kind,
                "receipt": receipt.model_dump(mode="json"),
            }
        )
        assert receipt.outcome != "rejected", receipt
        return receipt

    def pending(self, kernel, action):
        return next(
            m.ExecutorCommand.model_validate_json(json.dumps(r["command"]))
            for r in kernel.store.read()["outbox"].values()
            if r["command"]["action"] == action and r["status"] == "pending"
        )

    def launch_host(self, directory, command, phase):
        run = directory / (phase + "-" + str(len(self.hosts)))
        run.mkdir()
        path = run / "command.json"
        path.write_text(command.model_dump_json())
        log = (run / "process.log").open("ab")
        args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            str(directory / "cpu"),
            str(path),
            str(run),
            phase,
        ]
        process = subprocess.Popen(args, stdout=log, stderr=log, cwd=ROOT)
        log.close()
        self.hosts.append(process)
        self.history.append(
            {
                "kind": "host_started",
                "pid": process.pid,
                "phase": phase,
                "dispatch_id": command.dispatch_id,
            }
        )
        return process, run

    def host_ready(self, process, run, name):
        def ready():
            assert process.poll() is None, (run / "process.log").read_text()
            return (run / (name + ".ready")).exists()

        eventually(ready)
        if (run / "created-worker.json").exists():
            pid = json.loads((run / "created-worker.json").read_text())["pid"]
            self.workers.append((pid, run))
        self.history.append(
            {"kind": "barrier_reached", **json.loads((run / (name + ".ready")).read_text())}
        )

    def kill_host(self, process):
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        self.history.append({"kind": "host_killed", "pid": process.pid, "signal": "SIGKILL"})

    def driver(self, directory, kernel):
        executor = CpuReferenceExecutor(directory / "cpu", CONFIG)
        driver = CpuDriver(kernel, executor, lock_path=directory / "driver.lock")
        driver.start()
        self.drivers.append(driver)
        return executor, driver

    @staticmethod
    def spin(driver, fn):
        return eventually(lambda: (driver.tick(advance_wall=False), fn())[1])

    def release(self, kernel, driver, aid):
        self.commit(
            kernel, m.AllocationTargetParameters(kind="allocation.release", allocation_id=aid)
        )
        self.spin(driver, lambda: kernel.store.read()["allocations"][aid]["state"] == "Released")
        assert kernel.decision_snapshot().inventory[0].available.gpu_count == 1

    def launch_window(self, phase):
        directory, kernel, aid, jid = self.setup(phase)
        start = self.pending(kernel, "start")
        process, run = self.launch_host(directory, start, phase)
        name = (
            "host_after_reservation"
            if phase == "reserved"
            else "host_after_popen_before_pid_commit"
        )
        self.host_ready(process, run, name)
        before = json.loads((directory / "cpu/journal.json").read_text())
        assert len(before["attempts"]) == 1
        assert next(iter(before["attempts"].values()))["pid"] is None
        self.kill_host(process)
        # Reopen authority as well as adapter, as a restarted service does.
        kernel = Kernel(directory / "state.db", execution_mode="cpu_reference")
        executor, driver = self.driver(directory, kernel)
        driver.tick(advance_wall=False)
        records = executor.inspect()["attempts"]
        assert len(records) == 1
        if phase == "inside_launch":
            pid = json.loads((run / "created-worker.json").read_text())["pid"]
            assert process_alive(pid) and records[0]["pid"] is None
            assert not records[0]["stop_confirmed"]
            assert kernel.decision_snapshot().inventory[0].available.gpu_count == 0
            self.commit(
                kernel, m.AllocationTargetParameters(kind="allocation.release", allocation_id=aid)
            )
            driver.tick(advance_wall=False)
            assert kernel.store.read()["allocations"][aid]["state"] == "PreemptRequested"
            assert kernel.store.read()["allocations"][aid]["pending_terminal"] == "Released"
            assert kernel.decision_snapshot().inventory[0].available.gpu_count == 0
            assert not executor.inspect()["attempts"][0]["stop_confirmed"]
            self.history.append(
                {
                    "kind": "ambiguous_live_worker_quarantined",
                    "pid": pid,
                    "capacity_returned": False,
                }
            )
            (run / "worker_inside_launch_lock.release").touch()
            self.spin(
                driver, lambda: kernel.store.read()["allocations"][aid]["state"] == "Released"
            )
            assert not process_alive(pid)
            assert executor.inspect()["attempts"][0]["stop_confirmed"]
        elif phase == "after_launch":
            pid = json.loads((run / "created-worker.json").read_text())["pid"]
            assert records[0]["pid"] == pid
            self.spin(driver, lambda: kernel.store.read()["jobs"][jid]["state"] == "Succeeded")
            result = executor.read_artifact(
                kernel.store.read()["jobs"][jid]["result_uri"], owner_id="alice", epoch=0
            )
            assert result["state"] == executor.reference_result()
            assert len(executor.inspect()["attempts"]) == 1
        else:
            assert records[0]["stop_confirmed"]
            assert records[0]["launch_error"] == "unstarted_launch_fenced"
            assert kernel.store.read()["jobs"][jid]["state"] == "Failed"
            if phase == "delayed_launch":
                (run / "worker_before_launch_lock.release").touch()
                pid = json.loads((run / "created-worker.json").read_text())["pid"]
                eventually(lambda: not process_alive(pid))
                attempt_dir = directory / "cpu/attempts" / records[0]["key"]
                assert not (attempt_dir / "started.json").exists()
                assert not (attempt_dir / "progress.json").exists()
        self.release(kernel, driver, aid)
        # Replaying the same dispatch cannot reopen a cancelled or completed attempt.
        executor.dispatch(start, owner_id="alice", logical_time=kernel.store.read()["now"])
        assert len(executor.inspect()["attempts"]) == 1
        self.check(
            "launch-" + phase,
            {
                "attempt_count": 1,
                "job_state": kernel.store.read()["jobs"][jid]["state"],
                "resources_returned": True,
                "fenced_before_workload": phase in {"reserved", "delayed_launch"},
                "ambiguous_live_worker_quarantined_until_exit": phase == "inside_launch",
            },
        )
        driver.close()

    def saving_and_resume(self):
        directory, kernel, aid, jid = self.setup("saving-resume")
        executor, driver = self.driver(directory, kernel)
        driver.tick(advance_wall=False)
        self.spin(driver, lambda: bool(executor.inspect()["attempts"][0]["pid"]))
        time.sleep(0.06)
        self.commit(
            kernel,
            m.RecallParameters(
                kind="test.contributor_demand",
                device_ids=("gpu",),
                notice_seconds=5,
                duration_seconds=1,
            ),
            TEST,
        )
        checkpoint_command = self.pending(kernel, "checkpoint")
        process, run = self.launch_host(directory, checkpoint_command, "control_committed")
        self.host_ready(process, run, "host_before_control_delivery")
        self.kill_host(process)
        self.spin(driver, lambda: kernel.store.read()["allocations"][aid]["state"] == "Preempted")
        state = kernel.store.read()
        checkpoint = state["jobs"][jid]["checkpoint"]
        assert checkpoint["verification_status"] == "verified"
        cp_payload = executor.read_artifact(checkpoint["artifact_uri"], owner_id="alice", epoch=0)
        old_record = executor.inspect()["attempts"][0]
        old_pid = old_record["pid"]
        assert old_record["stop_confirmed"] and not process_alive(old_pid)
        assert 0 < cp_payload["state"]["step"] < CONFIG.total_steps
        old_event = next(
            e["event"]
            for e in json.loads((directory / "cpu/journal.json").read_text())["events"]
            if e["event"]["kind"] == "started"
        )
        self.commit(
            kernel, m.AllocationTargetParameters(kind="allocation.resume", allocation_id=aid)
        )
        self.commit(
            kernel,
            m.ClockAdvanceParameters(
                kind="clock.advance", advance_seconds=6.0, source="test_control"
            ),
            TEST,
        )
        resume = self.pending(kernel, "resume")
        driver.close()
        process, run = self.launch_host(directory, resume, "after_launch")
        self.host_ready(process, run, "host_after_popen_before_pid_commit")
        self.kill_host(process)
        kernel = Kernel(directory / "state.db", execution_mode="cpu_reference")
        executor, driver = self.driver(directory, kernel)
        self.spin(driver, lambda: kernel.store.read()["jobs"][jid]["state"] == "Succeeded")
        state = kernel.store.read()
        assert state["allocations"][aid]["restart_count"] == 1
        assert state["jobs"][jid]["retry_count"] == 0
        assert state["jobs"][jid]["checkpoint"]["continuation_status"] == "applied"
        result = executor.read_artifact(state["jobs"][jid]["result_uri"], owner_id="alice", epoch=0)
        assert result["state"] == executor.reference_result()
        assert result["pid"] != old_pid
        assert len(executor.inspect()["attempts"]) == 2
        # A distinct delayed callback carrying the superseded generation must be
        # rejected by the actual authority, not merely dropped by the adapter.
        old_event["callback_id"] += "-late-copy"
        before = kernel.store.read()
        event = m.ExecutorEvent.model_validate_json(json.dumps(old_event))
        receipt = kernel.commit(
            m.CommandEnvelope(
                request_id="late-generation",
                actor=SYSTEM,
                operation="executor.report",
                parameters=m.ExecutorReportParameters(kind="executor.report", event=event),
            )
        )
        assert receipt.outcome == "rejected" and receipt.error.code == "attempt_stale"
        assert kernel.store.read() == before
        self.history.append(
            {"kind": "rejected_stale_callback", "receipt": receipt.model_dump(mode="json")}
        )
        self.release(kernel, driver, aid)
        self.check(
            "saving-control-and-resume-crashes",
            {
                "checkpoint_step": cp_payload["state"]["step"],
                "old_pid": old_pid,
                "resumed_pid": result["pid"],
                "old_process_dead_before_resume": True,
                "attempt_count": 2,
                "restart_count": 1,
                "result_matches_reference": True,
                "stale_callback": "rejected_without_mutation",
            },
        )
        driver.close()

    def unpublished_checkpoint(self):
        directory, kernel, aid, jid = self.setup("unpublished")
        process, run = self.launch_host(
            directory, self.pending(kernel, "start"), "unpublished_checkpoint"
        )
        eventually(lambda: process.poll() is not None)
        assert process.returncode == 0, (run / "process.log").read_text()
        pid = json.loads((run / "created-worker.json").read_text())["pid"]
        self.workers.append((pid, run))
        executor, driver = self.driver(directory, kernel)
        driver.tick(advance_wall=False)
        self.commit(
            kernel,
            m.RecallParameters(
                kind="test.contributor_demand",
                device_ids=("gpu",),
                notice_seconds=5,
                duration_seconds=1,
            ),
            TEST,
        )
        driver.tick(advance_wall=False)
        eventually(lambda: (run / "worker_checkpoint_before_publish.ready").exists())
        records = executor.inspect()["attempts"]
        attempt_dir = directory / "cpu/attempts" / records[0]["key"]
        assert list(attempt_dir.glob(".write-*"))
        assert not list(attempt_dir.glob("checkpoint-*.json"))
        os.kill(pid, signal.SIGKILL)
        self.history.append(
            {"kind": "worker_killed", "pid": pid, "phase": "checkpoint_fsynced_not_published"}
        )
        eventually(lambda: not process_alive(pid))
        driver.tick(advance_wall=False)
        assert not kernel.store.read()["jobs"][jid].get("checkpoint")
        assert not any(
            e["event"]["kind"] == "checkpoint_reported"
            for e in json.loads((directory / "cpu/journal.json").read_text())["events"]
        )
        self.commit(
            kernel,
            m.ClockAdvanceParameters(
                kind="clock.advance", advance_seconds=6.0, source="test_control"
            ),
            TEST,
        )
        self.release(kernel, driver, aid)
        self.check(
            "saving-unpublished-artifact",
            {
                "partial_artifact_rejected": True,
                "checkpoint_accepted": False,
                "worker_dead_before_release": True,
            },
        )
        driver.close()

    def transaction_windows(self):
        directory = self.root / "transactions"
        directory.mkdir()
        for operation in OPERATIONS:
            for window in WINDOWS:
                self.check(
                    "transaction-" + operation + "-" + window,
                    exercise(directory, operation, window),
                )

    def cleanup(self):
        for host in self.hosts:
            if host.poll() is None:
                host.kill()
                host.wait(timeout=5)
        for driver in self.drivers:
            driver.close()
        # Discover children even when their host died before the expected barrier.
        for marker in self.root.glob("*/*/created-worker.json"):
            self.workers.append((json.loads(marker.read_text())["pid"], marker.parent))
        for pid, run in set(self.workers):
            if process_alive(pid):
                # Only kill the private child whose command still carries this
                # unique test directory; never a persisted PID without identity.
                cmd = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
                ).stdout
                if str(run) in cmd:
                    os.kill(pid, signal.SIGKILL)
        # Locally-owned Popen handles can be stopped without PID identity guesses.
        for driver in self.drivers:
            for handle in driver.executor._handles.values():
                if handle.poll() is None:
                    handle.kill()
                handle.wait(timeout=5)


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


def run_suite(directory):
    suite = CrashSuite(directory)
    try:
        for phase in ("reserved", "after_launch", "delayed_launch", "inside_launch"):
            suite.launch_window(phase)
        suite.saving_and_resume()
        suite.unpublished_checkpoint()
        suite.transaction_windows()
    finally:
        suite.cleanup()
    return suite


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_host(*sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        child_worker(*sys.argv[2:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evidence/cp3/crash_recovery_receipt.json"
    )
    args = parser.parse_args()
    build = current_build(ROOT)
    suite = None
    failure = None
    with tempfile.TemporaryDirectory(prefix="fabric-cp3-crash-") as directory:
        suite = CrashSuite(directory)
        try:
            for phase in ("reserved", "after_launch", "delayed_launch", "inside_launch"):
                suite.launch_window(phase)
            suite.saving_and_resume()
            suite.unpublished_checkpoint()
            suite.transaction_windows()
        except Exception:
            failure = traceback.format_exc()
        finally:
            suite.cleanup()
    after = current_build(ROOT)
    receipt = {
        "schema_version": "1.0.0",
        "check": "CP3-CRASH-RECOVERY",
        "gate_id": "CP3-CRASH-RECOVERY",
        "scope": "actual_CPU_executor_and_kernel_crash_windows_not_UI_or_GPU",
        "status": "FAIL" if failure else "STALE" if build != after else "PASS",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "build": build,
        "current_build": after,
        "build_at_finish": after,
        "source_changed_during_run": build != after,
        "command": [
            sys.executable,
            "tests/acceptance/cp3/cp3_crash_recovery.py",
            "--output",
            str(args.output),
        ],
        "checks": suite.checks,
        "history": suite.history,
        "failure": failure,
        "boundaries": [
            "SIGKILL of real dispatch-host and worker subprocesses; no injected success callbacks",
            "Same host and boot only; host reboot and storage loss are not certified",
            "Launch cancelled before workload may leave an OS process briefly alive but durably fenced",
            "SQLite precommit/postcommit windows cover allocation and both workflow binding transactions",
            "These finite crash histories do not prove correctness for every interleaving",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "output": str(args.output), "failure": failure}))
    raise SystemExit(0 if receipt["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
