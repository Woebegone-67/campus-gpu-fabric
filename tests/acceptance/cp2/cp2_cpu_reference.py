#!/usr/bin/env python3
"""Real TCP, real child-process CPU continuation and persistence acceptance.

Uses an isolated temporary database, random credentials, an ephemeral loopback
port, and a fixed three-second reference task. No callbacks or progress facts
are injected. This is backend acceptance, not a joint human/UI or GPU gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, object_id, registration  # noqa: E402
from check_checkpoint import current_build  # noqa: E402

from campus_fabric.app import create_app  # noqa: E402
from campus_fabric.executors.cpu_files import advance  # noqa: E402
from campus_fabric.executors.cpu_reference import CpuReferenceConfig  # noqa: E402

STEPS = 100_000
SEED = 17


def create_cpu_test_app():
    """Only pacing differs from production; the actual adapter and kernel are used."""
    return create_app(
        cpu_config=CpuReferenceConfig(
            total_steps=STEPS, batch_steps=1_000, batch_pause_seconds=0.03, seed=SEED
        )
    )


class CpuHarness(Harness):
    def __init__(self, directory):
        super().__init__(directory, app_target="cp2_cpu_reference:create_cpu_test_app")
        self.env["FABRIC_MODE"] = "cpu_reference"
        module_path = str(Path(__file__).resolve().parent)
        self.env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (module_path, self.env.get("PYTHONPATH")))
        )

    def current_stage(self, name, action, stage_id, version):
        # The 50 ms real clock may legitimately win an optimistic-write race.
        # Bound retries: a middleware that invalidates every request must fail.
        for retry in range(10):
            status, receipt = self.request(
                "POST",
                f"/x/v1/work-intents/{name}/actions",
                expected=(200, 409),
                body={"action": action, "stage_id": stage_id, "expected_version": version},
            )
            if status == 200:
                return receipt
            error = receipt["error"]
            code = error.get("code") if isinstance(error, dict) else error
            assert code == "stale_version", receipt
            version = self.get(f"/x/v1/work-intents/{name}")["version"]
        raise AssertionError("Every stage request became stale; CPU UI cannot make progress")

    def until(self, path, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.get(path)
            if predicate(value):
                return value
            time.sleep(0.04)
        raise AssertionError(f"Timed out observing {path}: {value}")

    def work(self, name, *, timeout=100, exercise_stale=False):
        specification = registration(name, settlement="keep_lease", timeout=timeout)
        specification["execution_mode"] = "cpu_reference"
        specification["specification"]["allocation"]["preemption_mode"] = "checkpoint"
        receipt = self.request("POST", "/x/v1/work-intents", body=specification, expected=202)[1]
        if exercise_stale:
            self.advance(0.001)
        allocated = self.current_stage(name, "allocate", "allocate", receipt["version"])
        created = self.current_stage(name, "create_job", "job", allocated["version"])
        return object_id(allocated, "allocation"), object_id(created, "job")

    def artifact(self, uri, actor="a", expected=200):
        return self.request(
            "GET", "/x/v1/artifacts?" + urlencode({"uri": uri}), actor=actor, expected=expected
        )[1]

    def journal(self):
        return json.loads((self.directory / "database/cpu-executor/journal.json").read_text())

    def finish_owned_processes(self):
        """Keep failed acceptance runs from orphaning a real reference worker."""
        if self.client is None:
            self.start()
        for allocation in self.get("/v1/allocations")["allocations"]:
            self.request("DELETE", f"/v1/allocations/{allocation['id']}", expected=202)
        deadline = time.monotonic() + 10
        journal_path = self.directory / "database/cpu-executor/journal.json"
        while journal_path.exists() and time.monotonic() < deadline:
            self.get("/v1/metrics")
            records = self.journal()["attempts"].values()
            if all(record["stop_confirmed"] for record in records):
                return
            time.sleep(0.05)
        if journal_path.exists():
            assert all(
                record["stop_confirmed"] for record in self.journal()["attempts"].values()
            ), "Test process cleanup did not confirm all exits"


def process_cannot_execute(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )
    return result.returncode != 0 or result.stdout.strip().startswith("Z")


def exercise(h):
    h.start()
    version = h.get("/v1/version")
    assert version["execution_mode"] == "cpu_reference"
    assert version["capabilities"]["real_cpu_execution"] is True
    assert version["capabilities"]["real_gpu_execution"] is False
    assert version["checkpoint"] == "CP2" and version["checkpoint_passed"] is False
    h.check("truthful-mode", "Real fixed CPU reference execution, GPU false, joint CP2 not claimed")

    aid, jid = h.work("cpu-continuation", exercise_stale=True)
    stale = [item for item in h.history if item.get("status") == 409]
    assert stale and any(item["response_body"]["error"] == "stale_version" for item in stale)
    h.check(
        "optimistic-stage-retry",
        "An intentionally stale version returns 409; the same stage succeeds after refresh within a bounded retry budget",
    )
    work_path = "/x/v1/work-intents/cpu-continuation"
    first = h.until(
        work_path, lambda value: value["intent"]["attempts"][0]["started_at"] is not None
    )
    original = first["intent"]["attempts"][0]["ref"]
    assert original["generation"] == 1 and original["job_id"] == jid
    expiry = h.get(f"/v1/allocations/{aid}")["expires_at"]
    time.sleep(0.12)
    h.request(
        "POST",
        "/_test/contributor-demand",
        actor="test",
        expected=201,
        body={"gpu_ids": ["acceptance-gpu"], "notice_seconds": 3, "duration_seconds": 1},
    )
    h.until(f"/v1/allocations/{aid}", lambda value: value["state"] == "Preempted")
    stopped = h.get(work_path)["intent"]
    checkpoint = stopped["attempts"][0]["checkpoint"]
    assert checkpoint["verification_status"] == "verified"
    assert checkpoint["continuation_status"] == "not_attempted"
    saved = h.artifact(checkpoint["artifact_uri"])
    assert 0 < saved["state"]["step"] < STEPS
    assert "token" not in saved
    journal = h.journal()
    source = next(
        record for record in journal["attempts"].values() if record["attempt"] == original
    )
    assert source["stop_confirmed"] and process_cannot_execute(source["pid"])
    callbacks = [item for item in journal["events"] if item["event"]["attempt"] == original]
    ready_sequence = next(
        item["sequence"] for item in callbacks if item["event"]["kind"] == "checkpoint_reported"
    )
    stop_sequence = next(
        item["sequence"] for item in callbacks if item["event"]["kind"] == "stopped"
    )
    assert ready_sequence < stop_sequence
    h.check(
        "real-checkpoint-and-stop",
        {
            "source_pid": source["pid"],
            "source_exit_code": source["exit_code"],
            "saved_step": saved["state"]["step"],
            "ready_sequence": ready_sequence,
            "stop_sequence": stop_sequence,
            "physical_stop_observed": True,
        },
    )

    h.artifact(checkpoint["artifact_uri"], actor="b", expected=404)
    h.request("GET", work_path, actor="b", expected=404)
    h.artifact("file:///etc/passwd", expected=404)
    h.check(
        "artifact-owner-isolation",
        "Alice can inspect the signed save; Bob and arbitrary file URIs receive 404",
    )

    h.request("POST", f"/v1/allocations/{aid}/resume", expected=202)
    pending = h.get(f"/v1/jobs/{jid}")
    assert pending["attempt"]["generation"] == 1
    # Explicit test clock ends the contributed reservation. It does not finish CPU work.
    h.advance(4)
    resumed = h.until(
        work_path,
        lambda value: (
            len(value["intent"]["attempts"]) == 2
            and value["intent"]["attempts"][1]["started_at"] is not None
        ),
    )
    target = resumed["intent"]["attempts"][1]
    assert target["ref"]["generation"] == 2
    assert target["ref"]["job_id"] == jid and target["ref"]["allocation_id"] == aid
    assert target["checkpoint"]["continuation_status"] == "applied"
    assert h.get(f"/v1/allocations/{aid}")["expires_at"] == expiry
    h.check(
        "same-work-generation-two",
        "Same work, job and lease; generation two applies the verified save without resetting lease expiry",
    )

    # Kill only the ephemeral server. The worker's independent PID must reconcile.
    h.stop(kill=True)
    h.start()
    completed = h.until(work_path, lambda value: value["intent"]["state"] == "completed")
    intent = completed["intent"]
    assert len(intent["job_ids"]) == 1 and len(intent["attempts"]) == 2
    assert intent["result"]["kind"] == "success"
    assert intent["attempts"][1]["checkpoint"]["continuation_status"] == "applied"
    result_uri = intent["result"]["artifact_uris"][0]
    result = h.artifact(result_uri)
    expected = {"step": STEPS, "value": advance(SEED, 0, STEPS)}
    assert result["state"] == expected
    assert result["pid"] != source["pid"] and process_cannot_execute(result["pid"])
    h.artifact(result_uri, actor="b", expected=404)
    h.check(
        "server-sigkill-continuation",
        {
            "result_pid": result["pid"],
            "result": result["state"],
            "source_step": saved["state"]["step"],
            "reference_equal": True,
            "server_restart_preserved_same_job": True,
        },
    )
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    h.until("/v1/capacity", lambda value: value["totals"]["available"]["gpu_count"] == 1)

    reference_aid, reference_jid = h.work("cpu-uninterrupted")
    reference = h.until(
        "/x/v1/work-intents/cpu-uninterrupted",
        lambda value: value["intent"]["state"] == "completed",
    )
    uninterrupted = h.artifact(reference["intent"]["result"]["artifact_uris"][0])
    assert uninterrupted["state"] == result["state"]
    assert uninterrupted["pid"] not in {source["pid"], result["pid"]}
    assert len(reference["intent"]["attempts"]) == 1
    h.request("DELETE", f"/v1/allocations/{reference_aid}", expected=202)
    h.check(
        "independent-uninterrupted-reference",
        {
            "job_id": reference_jid,
            "pid": uninterrupted["pid"],
            "state": uninterrupted["state"],
            "matches_resumed": True,
        },
    )

    h.stop()
    h.start()
    assert h.get(work_path)["intent"] == intent
    assert h.artifact(result_uri) == result
    h.request("GET", work_path, actor="b", expected=404)
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
    h.check(
        "settled-restart-persistence",
        "Completed work, checkpoint history and result survive restart; leases released and Bob remains isolated",
    )
    downtime_timeout(h)


def downtime_timeout(h):
    """A worker keeps computing while the server is down; its deadline must not pause."""
    assert h.get("/v1/version")["execution_mode"] == "cpu_reference"
    aid, jid = h.work("cpu-timeout-downtime", timeout=1)
    work_path = "/x/v1/work-intents/cpu-timeout-downtime"
    h.until(work_path, lambda value: value["intent"]["attempts"][0]["started_at"] is not None)
    logical_before = h.get("/v1/metrics")["logical_time"]
    journal = h.journal()
    record = next(
        record for record in journal["attempts"].values() if record["attempt"]["job_id"] == jid
    )
    physical_start = time.monotonic()
    h.stop(kill=True)
    time.sleep(3.5)
    h.start()
    terminal = h.until(f"/v1/jobs/{jid}", lambda value: value["state"] in {"Succeeded", "Failed"})
    logical_after = h.get("/v1/metrics")["logical_time"]
    observed = {
        "timeout_seconds": 1,
        "server_downtime_sleep_seconds": 3.5,
        "elapsed_with_restart_seconds": time.monotonic() - physical_start,
        "logical_before": logical_before,
        "logical_after": logical_after,
        "worker_pid": record["pid"],
        "terminal_state": terminal["state"],
    }
    # Record the discriminating observation even when the assertion fails.
    h.history.append({"kind": "downtime_timeout_observation", **observed})
    assert logical_after - logical_before >= 3.5, observed
    assert terminal["state"] == "Failed", observed
    assert h.get(work_path)["intent"]["result"]["kind"] == "failure"
    assert process_cannot_execute(record["pid"])
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    h.until("/v1/capacity", lambda value: value["totals"]["available"]["gpu_count"] == 1)
    h.check("server-downtime-keeps-timeout", observed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp2/cpu_tcp_receipt.json")
    parser.add_argument(
        "--only-downtime", action="store_true", help="Run only the hard-restart timeout regression"
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build = current_build(ROOT)
    failure = None
    with tempfile.TemporaryDirectory(prefix="fabric-cp2-cpu-tcp-") as directory:
        h = CpuHarness(directory)
        try:
            if args.only_downtime:
                h.start()
                downtime_timeout(h)
            else:
                exercise(h)
        except Exception:
            failure = h.redact(traceback.format_exc())
        finally:
            try:
                h.finish_owned_processes()
            except Exception:
                cleanup_error = h.redact(traceback.format_exc())
                failure = (failure or "") + "\nProcess cleanup failed:\n" + cleanup_error
            h.stop()
        end_build = current_build(ROOT)
        status = "FAIL" if failure else "STALE" if end_build != build else "PASS"
        receipt = {
            "schema_version": "1.0.0",
            "scope": "A_CP2_CPU_DOWNTIME_HTTP_ONLY"
            if args.only_downtime
            else "A_CP2_REAL_CPU_LOOPBACK_HTTP_ONLY",
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "build": build,
            "build_at_finish": end_build,
            "source_changed_during_run": build != end_build,
            "command": "uv run python tests/acceptance/cp2/cp2_cpu_reference.py"
            + (" --only-downtime" if args.only_downtime else ""),
            "checks": h.checks,
            "history": h.history,
            "failure": failure,
            "reference_configuration": {
                "total_steps": STEPS,
                "seed": SEED,
                "batch_steps": 1000,
                "batch_pause_seconds": 0.03,
            },
            "not_tested": [
                "B UI and two-human acceptance",
                "GPU or arbitrary image execution",
                "full CP2 acceptance",
                "cross-host or cross-reboot recovery",
            ],
        }
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(f"{status}: {args.output}")
    if failure:
        print(failure, file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
