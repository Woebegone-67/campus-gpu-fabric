#!/usr/bin/env python3
"""CP3: actual interrupted CPU runs, fresh runs and an independent uint64 oracle.

The mathematical oracle uses matrix exponentiation, never the executor's loop
or reference_result(). Real loopback requests drive two recall/stop/resume
cycles. Public artifact bodies, callback order, process exit observations and
automatic resource settlement are retained in an exact-build receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/acceptance/cp2"))

from accept_cp1_a import object_id, registration  # noqa: E402
from check_checkpoint import current_build  # noqa: E402
from cp2_cpu_reference import CpuHarness, process_cannot_execute  # noqa: E402

from campus_fabric.app import create_app  # noqa: E402
from campus_fabric.executors.cpu_reference import CpuReferenceConfig  # noqa: E402

MODULUS = 2**64
CASES = (
    {"name": "ordinary", "seed": 17, "total_steps": 100_003, "batch_steps": 997},
    {"name": "uint64-edge", "seed": MODULUS - 1, "total_steps": 65_537, "batch_steps": 509},
    {"name": "zero-seed", "seed": 0, "total_steps": 131_071, "batch_steps": 1237},
)


def oracle(seed: int, steps: int) -> dict[str, int]:
    """Compute T**steps [seed, 0, 1], modulo 2**64, in O(log steps).

    The specified kernel is v[i+1] = 1664525*v[i] + 1013904223 + i.
    T = [[1664525,1,1013904223],[0,1,1],[0,0,1]] advances (v,i,1).
    Matrix multiplication/exponentiation is independent of the production loop.
    Python integers preserve all 64 bits both here and in JSON artifact parsing.
    """
    if type(seed) is not int or not 0 <= seed < MODULUS:
        raise ValueError("seed must be a uint64 integer")
    if type(steps) is not int or steps < 0:
        raise ValueError("steps must be a nonnegative integer")
    power = ((1664525, 1, 1013904223), (0, 1, 1), (0, 0, 1))
    product = ((1, 0, 0), (0, 1, 0), (0, 0, 1))

    def multiply(left, right):
        return tuple(
            tuple(sum(left[i][k] * right[k][j] for k in range(3)) % MODULUS
                  for j in range(3))
            for i in range(3)
        )

    remaining = steps
    while remaining:
        if remaining & 1:
            product = multiply(product, power)
        power = multiply(power, power)
        remaining //= 2
    return {"step": steps, "value": (product[0][0] * seed + product[0][2]) % MODULUS}


def create_cpu_test_app():
    configuration = json.loads(os.environ["CP3_EQUIVALENCE_CONFIG"])
    return create_app(cpu_config=CpuReferenceConfig(**configuration))


class EquivalenceHarness(CpuHarness):
    def __init__(self, directory, case):
        super().__init__(directory)
        self.app_target = "cp3_cpu_equivalence:create_cpu_test_app"
        self.configuration = {key: value for key, value in case.items() if key != "name"}
        self.configuration["batch_pause_seconds"] = 0.03
        self.env["CP3_EQUIVALENCE_CONFIG"] = json.dumps(self.configuration)
        self.env["PYTHONPATH"] = os.pathsep.join(
            (str(Path(__file__).resolve().parent), self.env["PYTHONPATH"])
        )

    def start(self):
        super().start()
        self.history.append({"kind": "server_identity", "pid": self.process.pid,
                             "command": self.command, "working_directory": str(ROOT)})

    def check(self, name, observed):
        self.checks.append({"id": name, "status": "PASS", "observed": observed})
        print(f"PASS {name}", flush=True)

    def success_settling_work(self, name):
        specification = registration(name, settlement="release_on_success", timeout=100)
        specification["execution_mode"] = "cpu_reference"
        allocation = specification["specification"]["allocation"]
        allocation.update(preemption_mode="checkpoint", max_restarts=2)
        receipt = self.request("POST", "/x/v1/work-intents", body=specification, expected=202)[1]
        allocated = self.current_stage(name, "allocate", "allocate", receipt["version"])
        created = self.current_stage(name, "create_job", "job", allocated["version"])
        return object_id(allocated, "allocation"), object_id(created, "job")

    def process_record(self, attempt):
        record = next(value for value in self.journal()["attempts"].values()
                      if value["attempt"] == attempt)
        # The private journal also contains execution credentials. Export only
        # process facts, not authentication material or private artifact paths.
        return {key: record[key] for key in ("attempt", "pid", "stop_confirmed", "exit_code")}


def verify_state(payload, seed, *, expected_step=None):
    state = payload["state"]
    assert type(state["step"]) is int and type(state["value"]) is int, state
    assert 0 <= state["value"] < MODULUS, state
    assert state == oracle(seed, state["step"]), state
    if expected_step is not None:
        assert state["step"] == expected_step, state
    assert json.loads(json.dumps(payload))["state"] == state
    assert "token" not in payload


def exercise(h, case):
    h.start()
    mode = h.get("/v1/version")
    assert mode["execution_mode"] == "cpu_reference"
    assert mode["capabilities"]["real_cpu_execution"] is True
    assert mode["capabilities"]["real_gpu_execution"] is False
    name = "cp3-resumed-" + case["name"]
    work_path = "/x/v1/work-intents/" + name
    aid, jid = h.success_settling_work(name)
    expiry = h.get(f"/v1/allocations/{aid}")["expires_at"]
    source_pids = []
    saves = []
    prior_step = 0

    for generation in (1, 2):
        started = h.until(work_path, lambda value: (
            len(value["intent"]["attempts"]) == generation
            and value["intent"]["attempts"][-1]["started_at"] is not None
        ))["intent"]["attempts"][-1]
        ref = started["ref"]
        assert ref["generation"] == generation and ref["job_id"] == jid
        assert ref["allocation_id"] == aid
        if generation == 2:
            assert started["checkpoint"]["verification_status"] == "verified"
            assert started["checkpoint"]["continuation_status"] == "applied"
            assert started["checkpoint"]["resumed_attempt_id"] == ref["attempt_id"]
        # Wait for multiple actual compute batches; never inject worker progress.
        time.sleep(0.12)
        h.request("POST", "/_test/contributor-demand", actor="test", expected=201,
                  body={"gpu_ids": ["acceptance-gpu"], "notice_seconds": 3,
                        "duration_seconds": 1})
        h.until(f"/v1/allocations/{aid}", lambda value: value["state"] == "Preempted")
        stopped = h.get(work_path)["intent"]["attempts"][-1]
        checkpoint = stopped["checkpoint"]
        assert checkpoint["verification_status"] == "verified"
        assert checkpoint["continuation_status"] == "not_attempted"
        artifact = h.artifact(checkpoint["artifact_uri"])
        verify_state(artifact, case["seed"])
        assert artifact["kind"] == "checkpoint" and artifact["attempt"] == ref
        assert artifact["algorithm"] == "lcg64-indexed-v1"
        assert artifact["total_steps"] == case["total_steps"]
        saved_step = artifact["state"]["step"]
        assert prior_step < saved_step < case["total_steps"]
        prior_step = saved_step
        process = h.process_record(ref)
        assert process["stop_confirmed"] and process_cannot_execute(process["pid"])
        assert process["exit_code"] == 0
        assert process["pid"] not in source_pids
        source_pids.append(process["pid"])
        events = [item for item in h.journal()["events"] if item["event"]["attempt"] == ref]
        ready = next(item["sequence"] for item in events
                     if item["event"]["kind"] == "checkpoint_reported")
        stopped_sequence = next(item["sequence"] for item in events
                                if item["event"]["kind"] == "stopped")
        assert ready < stopped_sequence
        saves.append({"checkpoint": checkpoint, "public_artifact": artifact,
                      "source_process": process, "ready_sequence": ready,
                      "stop_sequence": stopped_sequence, "physical_exit_observed": True})
        h.artifact(checkpoint["artifact_uri"], actor="b", expected=404)

        # Reconstruct the application from disk after each cut, before resuming.
        # Only the owned acceptance server is killed; the source worker is
        # already confirmed stopped above.
        h.stop(kill=True)
        h.start()
        persisted = h.get(work_path)["intent"]["attempts"][-1]["checkpoint"]
        assert persisted == checkpoint
        assert h.artifact(checkpoint["artifact_uri"]) == artifact
        h.request("POST", f"/v1/allocations/{aid}/resume", expected=202)
        # End the supplier hold. This changes availability, not CPU progress.
        h.advance(4)

    final_start = h.until(work_path, lambda value: (
        len(value["intent"]["attempts"]) == 3
        and value["intent"]["attempts"][-1]["started_at"] is not None
    ))["intent"]
    final_attempt = final_start["attempts"][-1]
    assert final_attempt["ref"]["generation"] == 3
    assert final_attempt["checkpoint"]["verification_status"] == "verified"
    assert final_attempt["checkpoint"]["continuation_status"] == "applied"
    assert final_attempt["checkpoint"]["resumed_attempt_id"] == final_attempt["ref"]["attempt_id"]
    assert final_attempt["checkpoint"]["artifact_uri"] == saves[-1]["checkpoint"]["artifact_uri"]
    assert h.get(f"/v1/allocations/{aid}")["expires_at"] == expiry
    completed = h.until(work_path, lambda value: value["intent"]["state"] == "completed")["intent"]
    assert completed["job_ids"] == [jid] and completed["allocation_ids"] == [aid]
    assert len(completed["attempts"]) == 3 and completed["result"]["kind"] == "success"
    result_uri = completed["result"]["artifact_uris"][0]
    result = h.artifact(result_uri)
    verify_state(result, case["seed"], expected_step=case["total_steps"])
    assert result["kind"] == "result" and result["attempt"] == final_attempt["ref"]
    assert result["algorithm"] == "lcg64-indexed-v1"
    assert result["finished_monotonic_ns"] > result["started_monotonic_ns"]
    assert result["pid"] not in source_pids and process_cannot_execute(result["pid"])
    lease = h.get(f"/v1/allocations/{aid}")
    assert lease["state"] == "Released" and lease["restart_count"] == 2
    capacity = h.get("/v1/capacity")["totals"]
    assert capacity["available"] == capacity["capacity"], capacity
    h.check(case["name"] + ":two-real-resumes", {"saves": saves, "result": result,
             "final_attempt": final_attempt, "lease": lease,
             "full_capacity_restored_without_delete": True})

    uninterrupted_name = "cp3-uninterrupted-" + case["name"]
    reference_aid, reference_jid = h.success_settling_work(uninterrupted_name)
    reference = h.until("/x/v1/work-intents/" + uninterrupted_name,
                        lambda value: value["intent"]["state"] == "completed")["intent"]
    uninterrupted = h.artifact(reference["result"]["artifact_uris"][0])
    verify_state(uninterrupted, case["seed"], expected_step=case["total_steps"])
    assert uninterrupted["kind"] == "result"
    assert uninterrupted["attempt"] == reference["attempts"][0]["ref"]
    assert uninterrupted["workload_id"] == result["workload_id"]
    assert uninterrupted["finished_monotonic_ns"] > uninterrupted["started_monotonic_ns"]
    assert uninterrupted["state"] == result["state"]
    assert uninterrupted["pid"] not in [*source_pids, result["pid"]]
    assert process_cannot_execute(uninterrupted["pid"])
    assert len(reference["attempts"]) == 1
    assert h.get(f"/v1/allocations/{reference_aid}")["state"] == "Released"
    h.check(case["name"] + ":three-way-equality", {
        "oracle": oracle(case["seed"], case["total_steps"]), "resumed": result,
        "uninterrupted": uninterrupted, "uninterrupted_job": reference_jid,
        "distinct_actual_processes": [*source_pids, result["pid"], uninterrupted["pid"]],
        "exact_uint64_json_roundtrip": True,
    })

    h.stop()
    h.start()
    assert h.get(work_path)["intent"] == completed
    assert h.artifact(result_uri) == result
    h.request("GET", work_path, actor="b", expected=404)
    h.artifact(result_uri, actor="b", expected=404)
    capacity = h.get("/v1/capacity")["totals"]
    assert capacity["available"] == capacity["capacity"]
    h.check(case["name"] + ":settled-persistence-isolation", {
        "completed_intent_unchanged_after_restart": True,
        "artifact_unchanged_after_restart": True, "bob_status": 404,
        "capacity": capacity,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "evidence/cp3/cpu_equivalence_receipt.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build = current_build(ROOT)
    receipt = {"schema_version": "1.0.0", "check": "CP3-CPU-EQUIVALENCE",
               "scope": "A_CP3_REAL_CPU_EQUIVALENCE_HTTP_ONLY", "status": "RUNNING",
               "created_at": datetime.now(timezone.utc).isoformat(), "build": build,
               "command": [sys.executable, str(Path(__file__).resolve()), "--output",
                           str(args.output.resolve())], "cases": [], "checks": [], "failure": None,
               "oracle": "Independent 3x3 matrix exponentiation over integers modulo 2**64",
               "not_tested": ["B UI or two-human acceptance", "actual GPU execution",
                              "arbitrary user kernels", "cross-host or host-reboot recovery",
                              "browser JavaScript numeric display precision", "full CP3 acceptance"]}
    # An interrupted rerun must not leave the previous receipt looking current.
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fabric-cp3-equality-") as directory:
            harness = EquivalenceHarness(directory, case)
            failure = None
            try:
                exercise(harness, case)
            except Exception:
                failure = harness.redact(traceback.format_exc())
            finally:
                try:
                    harness.finish_owned_processes()
                except Exception:
                    failure = (failure or "") + "\nCleanup failed:\n" + harness.redact(
                        traceback.format_exc())
                harness.stop()
            receipt["cases"].append({"configuration": harness.configuration,
                                     "name": case["name"], "checks": harness.checks,
                                     "history": harness.history, "failure": failure})
            receipt["checks"].extend(harness.checks)
            if failure:
                receipt["failure"] = failure
                break
    end_build = current_build(ROOT)
    receipt.update(build_at_finish=end_build, source_changed_during_run=build != end_build,
                   status="FAIL" if receipt["failure"] else "STALE" if build != end_build else "PASS")
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(f"{receipt['status']}: {args.output}")
    if receipt["failure"]:
        print(receipt["failure"], file=sys.stderr)
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
