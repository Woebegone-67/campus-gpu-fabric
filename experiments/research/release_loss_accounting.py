"""Bounded real-CPU study; never claim an executor experiment is a product API."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.executors.cpu_files import read_signed
from campus_fabric.executors.cpu_reference import CpuReferenceConfig, CpuReferenceExecutor
from campus_fabric.executors.driver import CpuDriver
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator

ROOT = Path(__file__).resolve().parents[2]
VERSION = m.VersionStamp(epoch=0, revision=0)
CONFIG = CpuReferenceConfig(total_steps=10_000_000, batch_steps=50_000,
                            batch_pause_seconds=0, stop_timeout_seconds=0.2)
HORIZON = 40.0
NOTICE = 1.0
CASES = (("zero", 0, False), ("middle", 5_000_000, False),
         ("late", 9_000_000, False), ("save-failure", 5_000_000, True))
OWNER = m.AuthenticatedActor(actor_id="research-owner", role="researcher", owner_id="alice")
CONTRIBUTOR = m.AuthenticatedActor(actor_id="research-supplier", role="contributor",
                                   owner_id="supplier")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def oracle(seed, steps):
    # Independent acceptance oracle, not production advance/reference_result.
    spec = importlib.util.spec_from_file_location(
        "release_independent_oracle", ROOT / "tests/acceptance/cp3/cp3_cpu_equivalence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.oracle(seed, steps)


def command(action, generation=1, checkpoint=None):
    return m.ExecutorCommand(
        dispatch_id=f"{action}-{generation}", version=VERSION, mode="cpu_reference",
        attempt=m.AttemptRef(intent_id="research-work", allocation_id="research-allocation",
                             job_id="research-job", attempt_id=f"attempt-{generation}",
                             generation=generation), action=action, checkpoint=checkpoint,
        job=m.JobCreateParameters(kind="job.create", allocation_id="research-allocation",
                                  image="not-executed", command=("not-executed",))
        if action in {"start", "resume"} else None,
    )


@contextmanager
def measured_launch(fail_save=False):
    original = subprocess.Popen

    def launch(args, *pos, **kwargs):
        if (isinstance(args, list) and len(args) == 3
                and Path(args[1]).name == "cpu_worker.py"):
            args = [args[0], str(Path(__file__).with_name("release_cpu_worker.py")), args[2]]
            if fail_save:
                args.append("fail-save")
        return original(args, *pos, **kwargs)

    with patch("campus_fabric.executors.cpu_reference.subprocess.Popen", launch):
        yield


class MeasuredExecutor(CpuReferenceExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.verifications = []

    def _register_artifact(self, state, record, path, kind):
        start, cpu = time.monotonic_ns(), time.process_time_ns()
        result = super()._register_artifact(state, record, path, kind)
        self.verifications.append(dict(kind=kind, start_ns=start, end_ns=time.monotonic_ns(),
                                       cpu_ns=time.process_time_ns() - cpu))
        return result


def wait_until(predicate, timeout=HORIZON):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.001)
    raise TimeoutError("Bounded observation horizon expired")


def source_hashes():
    paths = sorted((ROOT / "src/campus_fabric").rglob("*.py"))
    paths += sorted(Path(__file__).parent.glob("release_*.py"))
    paths += [ROOT / "tests/acceptance/cp3/cp3_cpu_equivalence.py",
              ROOT / "evidence/release-20261115/research/PROTOCOL.md"]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def cleanup(executor):
    for handle in executor._handles.values():
        if handle.poll() is None:
            handle.terminate()
            try:
                handle.wait(timeout=2)
            except subprocess.TimeoutExpired:
                handle.kill()
                handle.wait(timeout=2)


def artifact_evidence(executor, uri):
    value = executor.read_artifact(uri, owner_id="alice", epoch=0)
    state = value["state"]
    assert state == oracle(CONFIG.seed, state["step"]), "Independent oracle mismatch"
    entry = json.loads((executor.root / "journal.json").read_text())["artifacts"][uri]
    return {"payload": value, "original_sha256": entry["sha256"],
            "independent_oracle_equal": True}


def corruption_probe(executor, checkpoint, destination):
    shutil.copytree(executor.root, destination)
    copied = CpuReferenceExecutor(destination, CONFIG)
    state = json.loads((destination / "journal.json").read_text())
    path = destination / state["artifacts"][checkpoint.artifact_uri]["path"]
    path.write_bytes(path.read_bytes() + b" ")
    try:
        copied.dispatch(command("resume", 3, checkpoint), owner_id="alice", logical_time=0)
    except (ValueError, OSError) as error:
        return {"rejected": True, "error": str(error),
                "new_attempts": len(copied.inspect()["attempts"]) - len(state["attempts"])}
    cleanup(copied)
    raise AssertionError("Corrupted artifact was accepted")


def strong_baseline(progress):
    rates = [(b["observed_ns"] - a["observed_ns"]) / 1e9 / (b["step"] - a["step"])
             for a, b in zip(progress, progress[1:]) if b["step"] > a["step"]]
    if not rates:
        return {"action": "checkpoint", "reason": "No causal throughput estimate"}
    remaining = (CONFIG.total_steps - progress[-1]["step"]) * max(rates) * 2
    return {"action": "finish" if remaining + 0.1 <= NOTICE else "checkpoint",
            "remaining_seconds_upper_estimate": remaining, "safety_margin_seconds": 0.1,
            "estimator": "2x slowest observed progress-interval seconds per step",
            "scope": "causal decision opportunity check only; finish was not executed"}


def one_run(runtime, name, threshold, fail_save, arm):
    executor = MeasuredExecutor(runtime / name / arm, CONFIG)
    output = {"case": name, "arm": arm, "status": "unfinished", "events": [],
              "progress_before_notice": [], "artifacts": [], "errors": [],
              "scope": "real executor/worker, no authoritative resource ledger"}
    start = time.monotonic_ns()
    output["arrival_ns"] = start

    def send(action, generation=1, cp=None):
        before = time.monotonic_ns()
        executor.dispatch(command(action, generation, cp), owner_id="alice",
                          logical_time=(before - start) / 1e9)
        output.setdefault("dispatches", []).append(dict(action=action, generation=generation,
                                                         start_ns=before, end_ns=time.monotonic_ns()))
        return before

    def poll(kind=None):
        events = executor.poll(logical_time=(time.monotonic_ns() - start) / 1e9, version=VERSION)
        matched = None
        for event in events:
            output["events"].append({"received_ns": time.monotonic_ns(),
                                     "event": event.model_dump(mode="json")})
            executor.acknowledge(event.callback_id)
            if event.kind == kind:
                matched = event
        return matched

    def observe_progress():
        poll()
        record = executor.inspect()["attempts"][0]
        path = executor.root / "attempts" / record["key"] / "progress.json"
        if not path.exists():
            return False
        data = read_signed(path, executor.key)
        step = data["state"]["step"]
        samples = output["progress_before_notice"]
        if not samples or step != samples[-1]["step"]:
            samples.append({"step": step, "observed_ns": data["observed_monotonic_ns"],
                            "available_ns": time.monotonic_ns()})
        return step >= threshold

    try:
        with measured_launch(fail_save):
            send("start")
            if threshold:
                wait_until(observe_progress)
            output["notice_ns"] = time.monotonic_ns()
            output["baseline_opportunity"] = strong_baseline(output["progress_before_notice"])
            checkpoint = None
            completed = None
            if arm == "finish_fits":
                cutoff = output["notice_ns"] / 1e9 + NOTICE - 0.1
                while time.monotonic() < cutoff:
                    completed = poll("completed")
                    if completed:
                        break
                    time.sleep(0.001)
                if completed:
                    output["finished_ns"] = time.monotonic_ns()
                    output["stop_confirmed_ns"] = output["finished_ns"]
                    output["artifacts"].append(artifact_evidence(executor, completed.result_uri))
                    output["status"] = "completed"
                    output["actual_branch"] = "finish_before_save_boundary"
                    return finalize(executor, output, start)
                output["actual_branch"] = "fixed_boundary_checkpoint"
            if arm in {"continuation", "finish_fits"}:
                send("checkpoint")

                def saved_or_failed():
                    poll()
                    for row in output["events"]:
                        event = m.ExecutorEvent.model_validate(row["event"])
                        if event.kind in {"checkpoint_reported", "failed"}:
                            return event
                    if (arm == "finish_fits" and time.monotonic_ns()
                            >= output["notice_ns"] + int(NOTICE * 1e9)):
                        send("stop")
                        raise TimeoutError("Checkpoint did not verify within original notice")
                    return None

                event = wait_until(saved_or_failed)
                if event.kind == "failed":
                    output["status"] = "failed"
                    output["failure"] = event.error.model_dump(mode="json")
                    output["stop_confirmed_ns"] = time.monotonic_ns()
                    return finalize(executor, output, start)
                checkpoint = event.checkpoint
            send("stop")
            wait_until(lambda: poll("stopped"))
            output["stop_confirmed_ns"] = time.monotonic_ns()
            assert executor.inspect()["attempts"][0]["stop_confirmed"]
            # Authorization is explicit and identical. No future resource timing is used.
            output["resume_authorized_ns"] = time.monotonic_ns()
            send("resume" if checkpoint else "start", 2, checkpoint)
            event = wait_until(lambda: poll("completed"))
            output["finished_ns"] = time.monotonic_ns()
            if checkpoint:
                output["artifacts"].append(artifact_evidence(executor, checkpoint.artifact_uri))
                if threshold == 0 and arm == "continuation":
                    assert output["artifacts"][-1]["payload"]["state"]["step"] == 0
                output["corruption_probe"] = corruption_probe(
                    executor, checkpoint, runtime / name / f"corrupted-copy-{arm}")
            output["artifacts"].append(artifact_evidence(executor, event.result_uri))
            output["status"] = "completed"
    except Exception as error:
        output["errors"].append({"type": type(error).__name__, "message": str(error),
                                 "traceback": traceback.format_exc()})
        output["status"] = "failed"
    finally:
        cleanup(executor)
    return finalize(executor, output, start)


def finalize(executor, output, start):
    attempts = executor.inspect()["attempts"]
    measured = []
    for record in attempts:
        directory = executor.root / "attempts" / record["key"]
        path = directory / "research-measurements.json"
        if not path.exists():
            continue
        value = json.loads(path.read_text())
        computation = value["compute"]
        value.update(generation=record["attempt"]["generation"],
                     initial_step=record["initial"]["step"],
                     final_step=computation[-1]["end_step"] if computation else record["initial"]["step"],
                     initial_source=record["resumed_from"] or "cold_start",
                     stop_confirmed=record["stop_confirmed"], exit_code=record.get("exit_code"))
        measured.append(value)
    measured.sort(key=lambda x: x["generation"])
    checkpoint_steps = [a["payload"]["state"]["step"] for a in output["artifacts"]
                        if a["payload"]["kind"] == "checkpoint"]
    retained = max(checkpoint_steps, default=0)
    executed = sum(a["final_step"] - a["initial_step"] for a in measured)
    first = measured[0] if measured else None
    output["measurements"] = measured
    output["verifications"] = executor.verifications
    output["metrics"] = {
        "executed_steps": executed, "required_steps": CONFIG.total_steps,
        "verified_saved_step": retained,
        "discarded_first_attempt_steps": None if first is None else (
            first["final_step"] - retained if len(measured) > 1 or output["status"] != "completed" else 0),
        "redundant_steps_if_completed": executed - CONFIG.total_steps
        if output["status"] == "completed" else None,
        "compute_cpu_seconds": sum(c["cpu_ns"] for a in measured for c in a["compute"]) / 1e9,
        "compute_wall_seconds": sum(c["end_ns"] - c["start_ns"] for a in measured for c in a["compute"]) / 1e9,
        "worker_cpu_seconds": sum(a["worker_cpu_ns"] for a in measured) / 1e9,
        "worker_occupied_wall_seconds": sum(a["worker_end_ns"] - a["worker_start_ns"] for a in measured) / 1e9,
        "checkpoint_write_seconds": sum(w["end_ns"] - w["start_ns"] for a in measured
                                         for w in a["writes"] if w["kind"] == "checkpoint") / 1e9,
        "checkpoint_write_bytes": sum(w["bytes"] for a in measured for w in a["writes"]
                                       if w["kind"] == "checkpoint"),
        "checkpoint_verify_seconds": sum(v["end_ns"] - v["start_ns"] for v in executor.verifications
                                          if v["kind"] == "checkpoint") / 1e9,
        "all_worker_write_bytes": sum(w["bytes"] for a in measured for w in a["writes"]),
        "end_to_end_seconds": (output["finished_ns"] - start) / 1e9
        if output["status"] == "completed" else None,
        "truncated_completion_cost_seconds": min(HORIZON, (output["finished_ns"] - start) / 1e9)
        if output["status"] == "completed" else HORIZON,
        "notice_to_stop_confirmed_seconds": (output["stop_confirmed_ns"] - output["notice_ns"]) / 1e9
        if "stop_confirmed_ns" in output else None,
    }
    stop_latency = output["metrics"]["notice_to_stop_confirmed_seconds"]
    output["metrics"]["notice_overrun_seconds"] = max(0., stop_latency - NOTICE) if stop_latency is not None else None
    if len(measured) > 1:
        started = next(w for w in measured[1]["writes"] if w["filename"] == "started.json")
        dispatch = next(d for d in output["dispatches"] if d["generation"] == 2)
        output["metrics"]["resume_or_cold_start_seconds"] = (started["end_ns"] - dispatch["start_ns"]) / 1e9
        output["metrics"]["measurement_audit_gap_seconds"] = (dispatch["start_ns"] - output["stop_confirmed_ns"]) / 1e9
    return output


def kernel_path(runtime):
    directory = runtime / "kernel-path"
    directory.mkdir()
    resources = m.ResourceAmounts(cpu_millicores=2000, memory_mib=1024,
                                   gpu_count=1, gpu_memory_mib=8192)
    node = m.NodeInventory(node_id="cpu-node", contributor_owner_id="supplier", state="Ready",
                           capacity=resources, available=resources, observed_at=0,
                           devices=(m.GPUDevice(device_id="g1", physical_gpu_id="g1", model="ref",
                                                memory_mib=8192, state="available"),))
    kernel = Kernel(directory / "state.db", inventory=(node,), execution_mode="cpu_reference")
    executor = MeasuredExecutor(directory / "cpu", CONFIG)
    driver = CpuDriver(kernel, executor, lock_path=directory / "driver.lock")
    workflow = WorkflowOrchestrator(kernel, kernel)
    result = {"scope": "one real Kernel + CpuDriver path; not a paired performance arm"}
    driver.start()
    try:
        with measured_launch():
            registration = m.WorkIntentRegistration(
                intent_id="loss-product-path", owner_id="alice", display_name="Loss accounting path",
                execution_mode="cpu_reference", settlement_action="release_on_success",
                specification=m.WorkSpecification(
                    allocation=m.AllocationCreateParameters(
                        kind="allocation.create", owner_id="alice", ttl_seconds=30,
                        preemption_mode="checkpoint", checkpoint_timeout_seconds=1, max_restarts=1,
                        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=256,
                                                   gpu=m.GPURequest(count=1))),
                    job=m.JobSpecification(image="not-executed", command=("not-executed",),
                                            timeout_seconds=30)))
            workflow.start(registration, kernel.decision_snapshot().version,
                           actor=OWNER, request_id="research-register")
            for _ in range(3):
                workflow.advance(registration.intent_id, actor=OWNER)
            result["arrival_ns"] = time.monotonic_ns()

            def tick():
                driver.tick()
                workflow.advance(registration.intent_id, actor=OWNER)
                return kernel.work_snapshot(registration.intent_id, OWNER)

            def progressed():
                tick()
                records = executor.inspect()["attempts"]
                if not records:
                    return False
                path = executor.root / "attempts" / records[0]["key"] / "progress.json"
                return path.exists() and read_signed(path, executor.key)["state"]["step"] >= 5_000_000

            wait_until(progressed)
            before = tick()
            allocation = before.allocations[0]
            result["original_expiry"] = allocation.expires_at
            result["notice_ns"] = time.monotonic_ns()
            receipt = kernel.commit(m.CommandEnvelope(
                request_id="research-contributor-recall", actor=CONTRIBUTOR,
                operation="contribution.recall", parameters=m.RecallParameters(
                    kind="contribution.recall", device_ids=("g1",), notice_seconds=1, duration_seconds=1)))
            assert receipt.outcome != "rejected", receipt
            wait_until(lambda: tick().allocations[0].state == "Preempted")
            result["ledger_released_ns"] = time.monotonic_ns()
            record = executor.inspect()["attempts"][0]
            assert record["stop_confirmed"] and executor._handles[record["key"]].poll() is not None
            result["physical_stop_confirmed_before_ledger_observation"] = True
            receipt = kernel.act_on_current_intent(registration.intent_id, "resume", 0,
                                                    actor=OWNER, request_id="research-resume")
            assert receipt.outcome != "rejected", receipt
            result["authorized_resume_ns"] = time.monotonic_ns()
            wait_until(lambda: tick().intents[0].state == "completed")
            final = tick()
            result["finished_ns"] = time.monotonic_ns()
            result["final_snapshot"] = final.model_dump(mode="json")
            assert final.allocations[0].expires_at == result["original_expiry"]
            assert final.allocations[0].state == "Released"
            assert final.allocations[0].restart_count == 1
            result["result"] = artifact_evidence(executor, final.jobs[0].result_uri)
            result["checkpoint"] = artifact_evidence(executor, final.jobs[0].checkpoint.artifact_uri)
            result["same_work_and_lease"] = len(final.intents) == len(final.allocations) == len(final.jobs) == 1
            result["notice_to_ledger_release_seconds"] = (result["ledger_released_ns"] - result["notice_ns"]) / 1e9
            result["status"] = "PASS"
    except Exception as error:
        result.update(status="FAIL", error=str(error), traceback=traceback.format_exc())
    finally:
        cleanup(executor)
        driver.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.runtime.exists():
        raise SystemExit("Refuse to replace an existing evidence or runtime directory")
    args.output.mkdir(parents=True)
    args.runtime.mkdir(parents=True, mode=0o700)
    before = source_hashes()
    manifest = {"source_hashes": before, "head": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "config": CONFIG.__dict__, "cases": CASES, "horizon": HORIZON,
        "notice_seconds": NOTICE, "frozen_at_ns": time.monotonic_ns(),
        "host": {"platform": platform.platform(), "python": platform.python_version(),
                 "cpus": os.cpu_count()}, "runtime_private": str(args.runtime.resolve()),
        "scope": "12 executor arms + one Kernel/Driver path; no GPU performance claim"}
    write(args.output / "manifest.json", manifest)
    runs = []
    for index, (name, threshold, fail_save) in enumerate(CASES):
        order = ("continuation", "cold") if index % 2 == 0 else ("cold", "continuation")
        for arm in (*order, "finish_fits"):
            value = one_run(args.runtime, name, threshold, fail_save, arm)
            write(args.output / f"{name}-{arm}.json", value)
            runs.append(value)
            print(name, arm, value["status"], value["metrics"], flush=True)
    path_result = kernel_path(args.runtime)
    write(args.output / "kernel-path.json", path_result)
    summary = {"source_unchanged": source_hashes() == before, "kernel_path": path_result["status"],
               "run_count": len(runs), "arms": {}, "pairs": [],
               "baseline_scope": "executed fixed-boundary save/finish with same deadline"}
    for arm in ("continuation", "cold", "finish_fits"):
        group = [r for r in runs if r["arm"] == arm]
        summary["arms"][arm] = {"completed": sum(r["status"] == "completed" for r in group),
                               "failed": sum(r["status"] == "failed" for r in group),
                               "unfinished": sum(r["status"] == "unfinished" for r in group),
                               "F_seconds": sum(r["metrics"]["truncated_completion_cost_seconds"] for r in group)}
    for name, _, _ in CASES:
        pair = {r["arm"]: r for r in runs if r["case"] == name}
        summary["pairs"].append({"case": name, **{arm: {
            "status": r["status"], "metrics": r["metrics"],
            "baseline_opportunity": r.get("baseline_opportunity"),
            "attempts": [{k: a[k] for k in ("generation", "initial_step", "final_step", "initial_source")}
                         for a in r["measurements"]], "errors": r["errors"]}
            for arm, r in pair.items()}})
    write(args.output / "summary.json", summary)
    write(args.output / "manifest-sha256.json", {p.name: digest(p) for p in args.output.glob("*.json")})
    print(json.dumps({"completed": summary["arms"], "kernel_path": path_result["status"],
                      "source_unchanged": summary["source_unchanged"]}), flush=True)


if __name__ == "__main__":
    main()
