"""Execute CP2 boundary checks afresh; never certify from an old receipt alone."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, allocation_body  # noqa: E402
from check_checkpoint import current_build  # noqa: E402


def new_allocation(h, *, mode="restartable", restarts=2, checkpoint_timeout=15, ttl=300):
    body = allocation_body(ttl=ttl)
    body["preemption_policy"] = {"mode": mode, "max_restarts": restarts,
                                 "checkpoint_timeout_seconds": checkpoint_timeout}
    return h.request("POST", "/v1/allocations", body=body, expected=201)[1]


def new_job(h, aid, timeout=100):
    return h.request("POST", "/v1/jobs", expected=201, body={
        "allocation_id": aid, "image": "cp2-logical-model", "command": ["work"],
        "timeout_seconds": timeout,
    })[1]["id"]


def recall(h, notice=5, duration=10):
    h.request("POST", "/_test/contributor-demand", actor="test", expected=201, body={
        "gpu_ids": ["acceptance-gpu"], "notice_seconds": notice, "duration_seconds": duration,
    })


def preemption_http(h):
    h.start()
    h.reset()
    initial = new_allocation(h, mode="checkpoint", restarts=1)
    aid, expiry = initial["id"], initial["expires_at"]
    jid = new_job(h, aid)
    h.advance(2)
    recall(h)
    h.request("POST", f"/v1/allocations/{aid}/checkpoint", expected=200,
              body={"status": "ready"})
    checkpoint = h.get(f"/v1/allocations/{aid}")["checkpoint"]
    assert checkpoint["verification_status"] == "unverified"
    assert checkpoint["continuation_status"] == "not_attempted"
    for _ in range(3):
        result = h.request("POST", f"/v1/allocations/{aid}/resume", expected=202)[1]
        assert result["state"] == "Pending" and result["restart_count"] == 0
        assert result["expires_at"] == expiry
    h.advance(15)
    assert h.get(f"/v1/jobs/{jid}")["attempt"]["generation"] == 2
    assert h.get(f"/v1/allocations/{aid}")["restart_count"] == 1
    recall(h)
    h.request("POST", f"/v1/allocations/{aid}/checkpoint", expected=200,
              body={"status": "ready"})
    denied = h.request("POST", f"/v1/allocations/{aid}/resume", expected=409)[1]
    assert denied["error"] == "budget_exhausted"
    h.check("ready-pending-resume-budget", "Unverified ready; three 202 waits charge zero; one replacement keeps job/TTL and charges one; second resume exhausts budget")

    for branch, timeout, delta in (("failed", 15, 0), ("timeout", 2, 2),
                                    ("exact-deadline", 15, 5)):
        h.reset()
        aid = new_allocation(h, mode="checkpoint", checkpoint_timeout=timeout)["id"]
        jid = new_job(h, aid)
        recall(h)
        if branch == "failed":
            h.request("POST", f"/v1/allocations/{aid}/checkpoint", expected=200,
                      body={"status": "failed"})
        else:
            h.advance(delta)
            h.request("POST", f"/v1/allocations/{aid}/checkpoint", expected=409,
                      body={"status": "ready"})
        assert h.get(f"/v1/allocations/{aid}")["state"] == "Evicted"
        assert h.get(f"/v1/jobs/{jid}")["state"] == "Failed"
        h.request("POST", f"/v1/allocations/{aid}/resume", expected=409)
        h.check(branch, "Strict checkpoint failure/cutoff remains Evicted; no late ready or resume revives it")

    h.reset()
    aid = new_allocation(h)["id"]
    jid = new_job(h, aid)
    recall(h, notice=5, duration=10)
    h.advance(2)
    recall(h, notice=5, duration=20)
    h.advance(3)
    h.request("POST", f"/v1/allocations/{aid}/resume", expected=202)
    h.advance(10)
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Pending"
    h.advance(12)
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Running"
    assert h.get(f"/v1/allocations/{aid}")["restart_count"] == 1
    h.check("overlapping-recall-union", "Earliest cutoff at 5; first interval ending at 15 does not reopen; replacement starts after final interval at 27")

    h.reset()
    aid = new_allocation(h, ttl=30)["id"]
    h.advance(25)
    jid = new_job(h, aid, timeout=5)
    recall(h)
    h.advance(5)
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Expired"
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Failed"
    h.check("expiry-before-recall-timeout", "Lease expiry wins the shared instant 30 before recall and job timeout")

    h.reset()
    aid = new_allocation(h)["id"]
    recall(h)
    h.request("POST", "/v1/jobs", expected=409,
              body={"allocation_id": aid, "image": "model", "command": ["work"]})
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    waiting = h.request("POST", "/v1/allocations", expected=202,
                        body=allocation_body())[1]
    assert waiting["state"] == "Pending"
    h.check("notice-blocks-new-work", "New jobs reject during notice; returning an old claim cannot reopen a recalled GPU")


def node_http(h):
    def set_state(state):
        return h.request("POST", "/_test/node-state", actor="test", expected=200,
                         body={"node_id": "acceptance-node", "state": state})[1]

    h.start()
    h.reset()
    aid = new_allocation(h)["id"]
    jid = new_job(h, aid)
    set_state("Draining")
    h.advance(10)
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Succeeded"
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    waiting = h.request("POST", "/v1/allocations", expected=202,
                        body=allocation_body())[1]["id"]
    set_state("Ready")
    assert h.get(f"/v1/allocations/{waiting}")["state"] == "Allocated"
    h.check("draining-preserves-existing", "Running job finishes while Draining; replacement waits until Ready")
    h.reset()
    aid = new_allocation(h)["id"]
    jid = new_job(h, aid)
    failed = set_state("NotReady")
    assert failed["generation"] == 1
    repeated = set_state("NotReady")
    assert repeated["generation"] == 1
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Evicted"
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Failed"
    set_state("Ready")
    h.request("POST", f"/v1/allocations/{aid}/resume", expected=409)
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Failed"
    h.check("node-generation-no-revival", "One NotReady transition increments generation once; Ready does not revive its evicted job")
    h.reset()
    recall(h)
    set_state("Draining")
    set_state("Ready")
    waiting = h.request("POST", "/v1/allocations", expected=202,
                        body=allocation_body())[1]["id"]
    h.advance(15)
    assert h.get(f"/v1/allocations/{waiting}")["state"] == "Allocated"
    h.check("ready-keeps-recall-block", "A Ready node remains blocked by an unfinished contributor interval")


SELECTIONS = {
    "CP2-CLOCK-BOUNDARIES": [
        "test_large_advance_equals_every_boundary_with_recall_retry_and_expiry",
        "test_expiry_beats_recall_and_completion_at_same_instant",
        "test_expiry_beats_due_retry_without_charging_nonexistent_attempt",
        "test_total_job_timeout_beats_retry_and_does_not_restart_timeout",
        "test_cpu_protocol_clock_and_unknown_observation_cannot_fabricate_stop",
        "test_cpu_protocol_timeout_and_recall_stop_complete_in_both_orders",
    ],
    "CP2-RESET-ISOLATION": [
        "test_reset_fences_old_epoch_callback_even_after_new_resources_and_job",
        "test_cpu_protocol_reset_quarantine_cannot_be_bypassed_by_renaming_node",
        "test_stale_attempt_cannot_complete_or_fail_replacement",
        "test_restart_from_disk_preserves_pending_resume_and_its_deadline",
    ],
}


def main(check_id):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp2" /
                        (check_id.lower().replace("-", "_") + "_receipt.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    before = current_build(ROOT)
    record = {"check_id": check_id, "observed_at": datetime.now(timezone.utc).isoformat(),
              "build": before, "status": "NOT_RUN", "checks": [], "history": [],
              "scope": "Individual CP2 check only; no joint UI or complete CP2 certification"}
    try:
        if check_id in {"CP2-PREEMPTION", "CP2-NODE-FAILURE"}:
            with tempfile.TemporaryDirectory(prefix="cp2-adversarial-http-") as directory:
                harness = Harness(directory)
                try:
                    (preemption_http if check_id == "CP2-PREEMPTION" else node_http)(harness)
                finally:
                    harness.stop()
                    record["checks"], record["history"] = harness.checks, harness.history
            record["evidence_kind"] = "http_blackbox"
        else:
            tests = ["tests/domain/test_cp2_adversarial.py::" + name
                     for name in SELECTIONS[check_id]]
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "src")
            invocation = [sys.executable, "-m", "pytest", "-q", *tests]
            result = subprocess.run(invocation, cwd=ROOT, env=env, capture_output=True,
                                    text=True, timeout=120, check=False)
            record.update(evidence_kind="independent_history", command=invocation,
                          selected_tests=tests, stdout=result.stdout, stderr=result.stderr,
                          returncode=result.returncode)
            assert result.returncode == 0, result.stdout + result.stderr
        record["status"] = "PASS"
    except Exception:
        record["status"] = "FAIL"
        record["failure"] = traceback.format_exc()
    record["build_at_finish"] = current_build(ROOT)
    if record["status"] == "PASS" and record["build_at_finish"] != before:
        record["status"] = "STALE"
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"check": check_id, "status": record["status"],
                      "evidence": str(args.output)}, ensure_ascii=False))
    if record.get("failure"):
        print(record["failure"], file=sys.stderr)
    return 0 if record["status"] == "PASS" else 1
