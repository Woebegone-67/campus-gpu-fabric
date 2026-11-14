"""Actual B-assembled CP2 journeys over loopback TCP; never human acceptance.

Every server and CPU process belongs to a fresh temporary test instance. Product
operations use researcher/contributor/admin credentials, never a browser test key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from accept_cp1_a import Harness, registration
from check_checkpoint import current_build

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/cp2/b"
APP = "campus_fabric.workflows.devhost:create_app"


def cpu_app():
    """Explicit reference pacing fixture; actual B host and real CPU adapter."""
    from campus_fabric.executors.cpu_reference import CpuReferenceConfig
    from campus_fabric.workflows.devhost import create_app
    return create_app(cpu_config=CpuReferenceConfig(
        total_steps=100_000, batch_steps=1_000, batch_pause_seconds=0.05, seed=17,
    ))


def until(h, path, predicate, *, actor="a", timeout=18):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = h.get(path, actor)
        if predicate(value):
            return value
        time.sleep(0.04)
    raise AssertionError(("Timed out", path, value))


def work(h, name, *, mode="logical_simulation", settlement="release_on_success"):
    body = registration(name, timeout=80, settlement=settlement)
    body["execution_mode"] = mode
    body["specification"]["allocation"].update(preemption_mode="checkpoint", max_restarts=2)
    h.request("POST", "/x/v1/work-intents", body=body, expected=202)
    return until(h, f"/x/v1/work-intents/{name}", lambda d: d["item"]["state"] == "running")


def act(h, name, action):
    # CPU clock can change revision between read and write. Only a proven stale
    # version is retried; no replay after transport/unknown commit failure.
    for _ in range(5):
        detail = h.get(f"/x/v1/work-intents/{name}")
        status, receipt = h.request("POST", f"/x/v1/work-intents/{name}/actions",
            body={"action": action, "expected_version": detail["version"]},
            expected=(200, 202, 409))
        if status != 409:
            return receipt
        error = receipt.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        if code != "stale_version":
            raise AssertionError(("Action rejected", action, receipt))
    raise AssertionError("Five consecutive stale versions prevented progress")


def clock(h, seconds):
    h.request("POST", "/x/v1/admin/clock", actor="admin",
              body={"advance_seconds": seconds}, expected=200)


def recall(h, *, notice=2, duration=3):
    return h.request("POST", "/x/v1/contributions/recall", actor="contributor",
        body={"gpu_ids": ["acceptance-gpu"], "notice_seconds": notice,
              "duration_seconds": duration}, expected=201)[1]


def logical(h, policy):
    h.env.update(FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY=policy)
    h.start()
    assert h.get("/v1/version")["execution_mode"] == "logical_simulation"
    name = f"logical-{policy.lower()}"
    initial = work(h, name)
    aid = initial["allocations"][0]["id"]
    jid = initial["jobs"][0]["id"]
    first = initial["item"]["attempts"][0]["ref"]
    expiry = initial["allocations"][0]["expires_at"]
    decision_id = next(event["decision_id"] for event in initial["events"]
                       if event.get("event_type") == "allocation.granted")
    explanation = h.get(f"/x/v1/decisions/{decision_id}")
    assert explanation["item"]["policy"]["name"] == policy
    assert h.get("/x/v1/contributions", "contributor")["items"]
    recall(h)
    notified = h.get(f"/x/v1/work-intents/{name}")
    assert notified["allocations"][0]["state"] == "PreemptRequested"
    assert notified["allocations"][0]["preemption"]
    h.request("POST", f"/v1/allocations/{aid}/checkpoint", body={"status": "ready"}, expected=200)
    clock(h, 2)
    stopped = h.get(f"/x/v1/work-intents/{name}")
    assert stopped["allocations"][0]["state"] == "Preempted"
    saved = stopped["allocations"][0]["checkpoint"]
    assert saved["report_status"] == "ready" and saved["verification_status"] != "verified"
    act(h, name, "resume")
    pending = h.get(f"/x/v1/work-intents/{name}")
    assert pending["allocations"][0]["state"] == "Pending"
    act(h, name, "resume")
    unchanged = h.get(f"/x/v1/work-intents/{name}")
    assert unchanged["allocations"][0]["restart_count"] == 0
    assert len(unchanged["item"]["attempts"]) == 1
    h.check(f"{policy}-recall-save-wait", "Contributor product recall; reported ready is not verified; repeated waiting resume consumes no generation/budget")
    clock(h, 3)
    resumed = until(h, f"/x/v1/work-intents/{name}",
        lambda d: len(d["item"]["attempts"]) == 2 and d["jobs"][0]["state"] == "Running")
    latest = resumed["item"]["attempts"][-1]["ref"]
    assert latest["job_id"] == jid and latest["allocation_id"] == aid
    assert latest["generation"] == first["generation"] + 1
    assert resumed["allocations"][0]["expires_at"] == expiry
    assert resumed["allocations"][0]["restart_count"] == 1
    h.request("GET", f"/x/v1/work-intents/{name}", actor="b", expected=404)
    h.check(f"{policy}-same-work-resume", "Same work, lease and job; new attempt, original expiry, owner isolation")
    clock(h, 10)
    completed = until(h, f"/x/v1/work-intents/{name}", lambda d: d["item"]["state"] == "completed")
    assert completed["allocations"][0]["state"] == "Released"
    assert completed["jobs"][0].get("result_uri") is None
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
    h.check(f"{policy}-honest-settlement", "Logical result has no real CPU artifact; authorized automatic settlement returned capacity exactly once")


def cpu(h):
    h.env.update(FABRIC_MODE="cpu_reference", FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY="P")
    h.env["PYTHONPATH"] = str(ROOT / "scripts") + os.pathsep + h.env.get("PYTHONPATH", "")
    h.start()
    version = h.get("/v1/version")
    assert version["execution_mode"] == "cpu_reference"
    initial = work(h, "cpu-b-journey", mode="cpu_reference", settlement="keep_lease")
    aid = initial["allocations"][0]["id"]
    jid = initial["jobs"][0]["id"]
    path = "/x/v1/work-intents/cpu-b-journey"
    until(h, path, lambda d: d["item"]["attempts"][0]["started_at"] is not None)
    recall(h, notice=3, duration=4)
    stopped = until(h, path, lambda d: d["allocations"][0]["state"] == "Preempted")
    cp = stopped["item"]["attempts"][0]["checkpoint"]
    assert cp["report_status"] == "ready" and cp["verification_status"] == "verified"
    assert cp["continuation_status"] == "not_attempted"
    artifact_path = "/x/v1/artifacts?" + urlencode({"uri": cp["artifact_uri"]})
    artifact = h.get(artifact_path)
    assert 0 < artifact["state"]["step"] < 100_000
    h.request("GET", artifact_path, actor="b", expected=404)
    h.check("real-cpu-save-stop", "Real CPU checkpoint verified; stopped before resource release; other owner cannot read artifact")
    act(h, "cpu-b-journey", "resume")
    continued = until(h, path, lambda d: len(d["item"]["attempts"]) == 2
        and (d["item"]["attempts"][-1].get("checkpoint") or {}).get("continuation_status") == "applied")
    assert continued["item"]["attempts"][-1]["ref"]["job_id"] == jid
    assert continued["item"]["attempts"][-1]["ref"]["allocation_id"] == aid
    completed = until(h, path, lambda d: d["item"]["state"] == "completed")
    result_uri = completed["jobs"][0]["result_uri"]
    result = h.get("/x/v1/artifacts?" + urlencode({"uri": result_uri}))
    assert result["state"]["step"] == 100_000
    assert completed["allocations"][0]["state"] == "Allocated"
    act(h, "cpu-b-journey", "release")
    until(h, path, lambda d: d["allocations"][0]["state"] == "Released")
    h.check("real-cpu-continue-result", "Actual generation two applied verified state; result artifact completed; keep_lease followed by explicit release")


def cleanup_cpu(h):
    if h.client is None:
        return
    for allocation in h.get("/v1/allocations").get("items", []):
        h.request("DELETE", f"/v1/allocations/{allocation['id']}", expected=(202, 409))
    journal = h.directory / "database/cpu-executor/journal.json"
    deadline = time.monotonic() + 12
    while journal.exists() and time.monotonic() < deadline:
        if all(a["stop_confirmed"] for a in json.loads(journal.read_text())["attempts"].values()):
            return
        time.sleep(0.05)
    if journal.exists():
        assert all(a["stop_confirmed"] for a in json.loads(journal.read_text())["attempts"].values()), "Owned CPU process stop not confirmed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT / "backend_receipt.json")
    parser.add_argument("--policies", nargs="+", choices=["B0", "B1", "B2", "P"], default=["B0", "B1", "B2", "P"])
    parser.add_argument("--skip-cpu", action="store_true", help="Focused logical diagnostic; does not certify complete B backend")
    args = parser.parse_args(argv)
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT / "evidence"):
        parser.error("Output must be inside this checkout's evidence directory")
    before = current_build(ROOT)
    records, checks, failures = [], [], []
    cases = [(policy, lambda h, p=policy: logical(h, p), APP) for policy in args.policies]
    if not args.skip_cpu:
        cases.append(("cpu", cpu, "accept_cp2_b:cpu_app"))
    for name, exercise, app in cases:
        with tempfile.TemporaryDirectory(prefix=f"fabric-cp2-b-{name}-") as directory:
            h = Harness(directory, app_target=app)
            try:
                exercise(h)
            except Exception:
                failures.append({"case": name, "failure": h.redact(traceback.format_exc())})
            finally:
                if name == "cpu":
                    try:
                        cleanup_cpu(h)
                    except Exception:
                        failures.append({"case": name, "failure": h.redact(traceback.format_exc())})
                h.stop()
            checks.extend(h.checks)
            records.append({"case": name, "records": h.history})
            assert not any(token in json.dumps(records) for token in [*h.tokens.values(), h.test_key])
    after = current_build(ROOT)
    if before != after:
        failures.append({"case": "build", "failure": "Source changed during run; rerun frozen build"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    history = args.output.with_name(args.output.stem + "_history.json")
    history.write_text(json.dumps({"build": before, "records": records}, indent=2) + "\n")
    receipt = {"schema_version": "1.0.0", "scope": "AUTOMATED_B_CP2_BACKEND_ONLY",
        "status": "PASS" if not failures else "FAIL", "build": before, "build_after": after,
        "observed_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
        "policies": args.policies, "cpu_executed": not args.skip_cpu,
        "joint_human_cp2": "NOT_RUN", "browser_ui": "NOT_RUN", "failures": failures,
        "evidence": {"path": history.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(history.read_bytes()).hexdigest()}}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "checks": len(checks), "failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
