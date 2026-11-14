"""A+B assembled backend TCP/process checks, explicitly not a human/UI CP1 gate."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from accept_cp1_a import OUTPUT, ROOT, Harness, allocation_body, object_id, registration
from check_checkpoint import current_build

SCOPE = "AUTOMATED_A_B_ASSEMBLED_CP1_BACKEND_ONLY"
APP = "campus_fabric.workflows.devhost:create_app"


def manual_app():
    """Test fixture: real A+B host, autonomous worker disabled for a deterministic race."""
    from campus_fabric.workflows.devhost import create_app
    return create_app(worker_enabled=False)


def wait_intent(h, name, state="running"):
    deadline = time.monotonic() + 10
    while True:
        detail = h.get(f"/x/v1/work-intents/{name}")
        if detail["item"]["state"] == state:
            return detail
        if time.monotonic() >= deadline:
            raise AssertionError((name, state, detail))
        time.sleep(0.05)


def submit(h, name, **kwargs):
    h.request("POST", "/x/v1/work-intents", body=registration(name, **kwargs), expected=202)
    return wait_intent(h, name)


def advance_admin(h, seconds):
    h.request("POST", "/x/v1/admin/clock", actor="admin",
              body={"advance_seconds": seconds}, expected=200)


def run_checks(h):
    h.start()
    assert h.get("/v1/version")["execution_mode"] == "logical_simulation"
    initial = submit(h, "normal", settlement="release_on_success")
    assert len(initial["jobs"]) == len(initial["allocations"]) == 1
    advance_admin(h, 10)
    final = wait_intent(h, "normal", "completed")
    assert final["allocations"][0]["state"] == "Released"
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
    h.check("automatic-normal-and-terminal-settlement",
            "Real devhost worker registered, bound, ran logical work; admin clock completed and released")

    h.reset()
    barrier = threading.Barrier(32)

    def compete(index):
        barrier.wait(timeout=10)
        return h.request("POST", "/v1/allocations", body=allocation_body(),
                         key=f"joint-{index}", expected=(201, 202))

    with ThreadPoolExecutor(max_workers=32) as pool:
        responses = list(pool.map(compete, range(32)))
    assert Counter(code for code, _ in responses) == {201: 1, 202: 31}
    items = h.get("/v1/allocations?limit=100")["items"]
    assert len(items) == 32 and sum(a["state"] == "Allocated" for a in items) == 1
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 0
    h.check("32-network-last-resource", "A+B host: 32 simultaneous requests, one grant, 31 pending")

    h.reset()
    body = registration("lost-register", settlement="keep_lease")
    sock = h.send_without_reading("/x/v1/work-intents", body)
    try:
        # Poll a list first: the unread TCP request may not yet have committed.
        deadline = time.monotonic() + 10
        while not h.get("/x/v1/work-intents")["items"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        running = wait_intent(h, "lost-register")
        h.stop(kill=True)
    finally:
        sock.close()
    h.start()
    replay = h.request("POST", "/x/v1/work-intents", body=body, expected=202)[1]
    assert object_id(replay, "intent") == "lost-register"
    recovered = wait_intent(h, "lost-register")
    for field in ("allocation_ids", "job_ids", "stages", "first_arrival_at"):
        assert recovered["item"][field] == running["item"][field]
    for stage in recovered["item"]["stages"]:
        action = "allocate" if stage["operation"] == "allocate" else "create_job"
        replay_stage = h.stage("lost-register", action, stage["stage_id"],
                               {"epoch": recovered["version"]["epoch"], "revision": 0})
        assert replay_stage["objects"] == stage["receipt"]["objects"]
    assert len(h.get("/v1/jobs")["items"]) == len(h.get("/v1/allocations")["items"]) == 1
    h.check("lost-register-sigkill-recovery-and-stage-replay",
            "Unread registration response; running process SIGKILL; restart retained IDs and stage receipts")
    h.request("GET", "/x/v1/work-intents/lost-register", actor="b", expected=404)
    assert h.get("/v1/jobs", "b")["items"] == []
    h.check("joint-owner-isolation", "Bob cannot read Alice's work or job through B query projections")

    h.reset()
    started = submit(h, "failed-keep", timeout=1, settlement="keep_lease")
    aid = started["allocations"][0]["id"]
    advance_admin(h, 1)
    failed = wait_intent(h, "failed-keep", "failed")
    assert failed["jobs"][0]["state"] == "Failed"
    assert failed["allocations"][0]["state"] == "Allocated"
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 0
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
    h.check("failed-job-keeps-lease-explicit-release",
            "1-second timeout fails 10-second model; keep_lease holds resource until explicit release, no double return")

    h.reset()
    submit(h, "failure-settle", timeout=1, settlement="release_on_terminal")
    advance_admin(h, 1)
    failed = wait_intent(h, "failure-settle", "failed")
    assert failed["allocations"][0]["state"] == "Released"
    h.check("failure-terminal-settlement", "release_on_terminal returns the allocation on job failure")

    # A deterministic version conflict needs no timing luck: restart the same
    # assembled host with only its background worker disabled by this test fixture.
    h.stop()
    h.app_target = "accept_cp1_joint:manual_app"
    h.start()
    h.history.append({"kind": "test_fixture", "worker_enabled": False,
                      "reason": "Deterministic concurrent-version change before job-stage submission"})
    h.reset()
    registered, allocated = h.register_and_allocate("partial")
    aid = object_id(allocated, "allocation")
    advance_admin(h, 1)
    h.stage("partial", "create_job", "job-v1", allocated["version"], expected=409)
    detail = h.get("/x/v1/work-intents/partial")
    assert detail["jobs"] == [] and detail["allocations"][0]["state"] == "Allocated"
    assert object_id(h.stage("partial", "allocate", "allocate", registered["version"]),
                     "allocation") == aid
    started = h.stage("partial", "create_job", "job-v1", detail["version"])
    retried = h.stage("partial", "create_job", "job-v1", detail["version"])
    assert object_id(started, "job") == object_id(retried, "job")
    assert len(h.get("/v1/jobs")["items"]) == len(h.get("/v1/allocations")["items"]) == 1
    h.check("partial-success-stale-stage-safe-retry",
            "Explicit worker-disabled fixture: stale job stage409 preserves lease; same-stage retry binds once")


def main():
    started = time.monotonic()
    before = current_build(ROOT)
    failure = None
    with tempfile.TemporaryDirectory(prefix="fabric-cp1-joint-") as directory:
        h = Harness(directory, app_target=APP)
        # The test-only factory lives in scripts; no production factory is replaced.
        h.env["PYTHONPATH"] = str(ROOT / "scripts") + os.pathsep + h.env.get("PYTHONPATH", "")
        try:
            run_checks(h)
        except Exception:
            failure = h.redact(traceback.format_exc())
        finally:
            h.stop()
        after = current_build(ROOT)
        if before != after and failure is None:
            failure = "Source tree changed during acceptance; rerun against the final build."
        history = json.dumps({"schema_version": "1.0", "scope": SCOPE,
            "credentials_recorded": False, "records": h.history}, indent=2, ensure_ascii=False) + "\n"
        assert all(token not in history for token in [*h.tokens.values(), h.test_key])
        OUTPUT.mkdir(parents=True, exist_ok=True)
        history_path = OUTPUT / "joint_backend_history.json"
        history_path.write_text(history)
        receipt = {"schema_version": "1.0", "scope": SCOPE, "checkpoint": "CP1",
            "status": "PASS" if failure is None else "FAIL", "joint_human_cp1": "NOT_RUN",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "command": "uv run python scripts/accept_cp1_joint.py", "build": before,
            "build_after": after, "execution_mode": "logical_simulation",
            "app_target": APP, "scheduling_mode": "builtin",
            "elapsed_seconds": round(time.monotonic() - started, 3), "checks": h.checks,
            "failure": failure,
            "evidence": {"path": "evidence/cp1/joint_backend_history.json",
                         "sha256": hashlib.sha256(history.encode()).hexdigest()},
            "not_validated": ["Two human operators and browser UI", "CP2 recovery and real CPU",
                              "External policy mode (separate pytest audit)", "Competition scoring"],
            "test_injection": "Worker disabled only for deterministic stale job-stage conflict; no production error injected"}
        (OUTPUT / "joint_backend_receipt.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    if failure:
        print(failure, file=sys.stderr)
        return 1
    print(f"A+B CP1 backend: {len(h.checks)} checks PASS; human/UI joint gate NOT_RUN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
