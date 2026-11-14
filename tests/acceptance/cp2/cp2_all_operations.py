#!/usr/bin/env python3
"""Actual loopback CP2 API journeys, scoped fault and contributor evidence.

Run with ``uv run python tests/acceptance/cp2/cp2_all_operations.py``. This script
uses random per-run credentials, an ephemeral port and a temporary database.
It never reaches the human UI acceptance host. It does not certify real CPU
execution, exhaustive API conformance, B's UI, or the complete CP2 checkpoint.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from starlette.routing import compile_path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, allocation_body  # noqa: E402
from check_checkpoint import current_build  # noqa: E402


def allocation(h, mode="restartable"):
    body = allocation_body()
    body["preemption_policy"] = {"mode": mode}
    return h.request("POST", "/v1/allocations", body=body, expected=201)[1]["id"]


def job(h, aid):
    return h.request("POST", "/v1/jobs", expected=201, body={
        "allocation_id": aid, "image": "cp2-logical-test", "command": ["work"],
    })[1]["id"]


def configure(h, target, mode, **options):
    h.request("POST", "/_test/fault", actor="test", expected=204,
              body={"operation": target, "mode": mode, **options})


def exercise(h):
    h.start()
    assert h.get("/healthz")["control_plane_ready"] is True
    version = h.get("/v1/version")
    assert version["checkpoint"] == "CP2" and version["checkpoint_passed"] is False
    assert version["execution_mode"] == "logical_simulation"
    assert version["capabilities"]["real_gpu_execution"] is False
    h.reset()
    assert len(h.get("/v1/capacity")["nodes"]) == 1
    h.request("POST", "/_test/node-state", actor="a", expected=403,
              body={"node_id": "acceptance-node", "state": "NotReady"})
    h.check("test-control-authority", "Valid public bearer cannot invoke X-Test-Key controls")

    aid = allocation(h, "checkpoint")
    h.request("POST", f"/v1/allocations/{aid}/renew", expected=200)
    jid = job(h, aid)
    assert len(h.get("/v1/allocations")["allocations"]) == 1
    assert len(h.get("/v1/jobs")["jobs"]) == 1
    h.advance(2)
    h.request("POST", "/_test/contributor-demand", actor="test", expected=201,
              body={"gpu_ids": ["acceptance-gpu"], "notice_seconds": 5, "duration_seconds": 10})
    h.request("POST", f"/v1/allocations/{aid}/checkpoint", body={"status": "ready"}, expected=200)
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Preempted"
    h.request("POST", f"/v1/allocations/{aid}/resume", expected=202)
    h.advance(15)
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Running"
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Running"
    assert len(h.get("/v1/jobs")["jobs"]) == 1
    h.request("DELETE", f"/v1/jobs/{jid}", expected=202)
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
    assert h.get("/v1/metrics")["execution_mode"] == "logical_simulation"
    assert h.get("/v1/events")["events"]
    h.check("checkpoint-resume-lease", "Ready report, 202 replacement wait, same job on next attempt, cancel and release")
    h.request("GET", f"/v1/jobs/{jid}", actor="b", expected=404)
    h.request("POST", f"/v1/allocations/{aid}/resume", actor="b", expected=404)
    assert h.get("/v1/jobs", actor="b")["jobs"] == []
    h.check("owner-isolation", "Bob cannot list, read or resume Alice's work")

    h.reset()
    aid = allocation(h)
    jid = job(h, aid)
    h.request("POST", "/_test/node-state", actor="test", expected=200,
              body={"node_id": "acceptance-node", "state": "NotReady"})
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Evicted"
    h.request("POST", "/_test/node-state", actor="test", expected=200,
              body={"node_id": "acceptance-node", "state": "Ready"})
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Evicted"
    assert h.get(f"/v1/jobs/{jid}")["state"] in {"Failed", "Cancelled"}
    h.check("node-state-no-revival", "NotReady eviction persists when the node returns Ready")

    h.reset()
    recall = {"gpu_ids": ["acceptance-gpu"], "notice_seconds": 1, "duration_seconds": 3}
    h.request("POST", "/x/v1/contributions/recall", actor="other-contributor", expected=403,
              body=recall)
    h.request("POST", "/x/v1/contributions/recall", actor="a", expected=403, body=recall)
    h.request("POST", "/x/v1/contributions/recall", actor="contributor", expected=201, body=recall)
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 0
    h.advance(4)
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
    h.check("contributor-product-authority", "Owner bearer recalls own GPU with no test key; foreign contributor/researcher denied; interval restores capacity")

    h.reset()
    for mode in (429, 500, 503):
        configure(h, "POST /v1/allocations", mode)
        h.request("POST", "/v1/allocations", expected=mode, body=allocation_body(), key=f"retry-{mode}")
        assert h.get("/v1/allocations")["allocations"] == []
        first = h.request("POST", "/v1/allocations", expected=201,
                          body=allocation_body(), key=f"retry-{mode}")[1]
        replay = h.request("POST", "/v1/allocations", expected=201,
                           body=allocation_body(), key=f"retry-{mode}")[1]
        assert first == replay and len(h.get("/v1/allocations")["allocations"]) == 1
        h.reset()
    h.check("fault-statuses-no-side-effects", "429/500/503 each fire once before command; same idempotency key later creates exactly one lease")

    configure(h, "GET /v1/capacity", "503", count=7)
    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(lambda _: h.request("GET", "/v1/capacity")[0], range(32)))
    assert statuses.count(503) == 7 and statuses.count(200) == 25
    h.check("fault-concurrent-count", "32 real TCP requests consume exactly seven configured faults")

    configure(h, "GET /v1/metrics", "500", count=2)
    h.request("GET", "/v1/metrics", expected=500)
    h.stop(kill=True)
    h.start()
    h.request("GET", "/v1/metrics", expected=500)
    h.request("GET", "/v1/metrics", expected=200)
    h.check("fault-sigkill-persistence", "Remaining fault budget survives hard server restart and exhausts exactly")

    aid = allocation(h)
    configure(h, "POST /v1/allocations/{allocation_id}/renew", "429")
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Allocated"
    h.request("POST", f"/v1/allocations/{aid}/renew", expected=429)
    h.request("POST", f"/v1/allocations/{aid}/renew", expected=200)
    h.check("fault-route-template", "GET does not consume POST renew rule; concrete ID matches its frozen template")

    before = h.get("/v1/metrics")["logical_time"]
    configure(h, "GET /v1/metrics", "latency", delay_ms=80)
    started = time.monotonic()
    result = h.get("/v1/metrics")
    elapsed = time.monotonic() - started
    assert elapsed >= .07 and result["logical_time"] == before
    h.check("fault-latency-clock", f"Observed {elapsed:.6f} s wall delay; logical time unchanged at {before}")
    configure(h, "GET /v1/capacity", "503", count=20)
    h.request("POST", "/_test/reset", actor="test", expected=204)
    assert h.get("/v1/capacity")["nodes"] == []
    assert h.get("/v1/allocations")["allocations"] == []
    h.check("reset-clears-faults", "Reset remains reachable, clears faults and returns deterministic empty inventory")


def coverage(history):
    catalogue = json.loads((ROOT / "contracts/api_ownership.json").read_text())
    result = []
    for operation in catalogue["operations"]:
        pattern = compile_path(operation["path"])[0]
        requests = [item for item in history if item.get("kind") == "http"
                    and item["method"] == operation["method"] and pattern.fullmatch(item["path"])]
        successful = [item for item in requests if 200 <= item["status"] < 300]
        result.append({"operation_id": operation["operation_id"],
                       "operation": f"{operation['method']} {operation['path']}",
                       "status": "PASS" if successful else "NOT_MET",
                       "successful_request_ids": [item["request_id"] for item in successful],
                       "observed_statuses": sorted({item["status"] for item in requests})})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp2/http_operations_receipt.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build = current_build(ROOT)
    failure = None
    with tempfile.TemporaryDirectory(prefix="fabric-cp2-http-") as directory:
        h = Harness(directory)
        # A second contributor is a distinct trusted identity, not a spoofed header.
        token = secrets.token_urlsafe(24)
        h.tokens["other-contributor"] = token
        h.owners["other-contributor"] = "other-supplier"
        auth_path = Path(directory) / "auth.json"
        auth = json.loads(auth_path.read_text())
        auth[token] = {"actor_id": "other-supplier", "owner_id": "other-supplier", "role": "contributor"}
        auth_path.write_text(json.dumps(auth))
        try:
            exercise(h)
            operations = coverage(h.history)
            assert len(operations) == 21 and all(item["status"] == "PASS" for item in operations)
            h.check("all-21-operation-journeys", "Every frozen operation produced at least one asserted successful real HTTP response")
        except Exception:
            failure = h.redact(traceback.format_exc())
        finally:
            h.stop()
        operations = coverage(h.history)
        end_build = current_build(ROOT)
        status = "FAIL" if failure else "STALE" if end_build != build else "PASS"
        receipt = {
            "schema_version": "1.0.0", "scope": "A_CP2_LOOPBACK_HTTP_ONLY", "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(), "build": build,
            "build_at_finish": end_build, "source_changed_during_run": end_build != build,
            "command": "uv run python tests/acceptance/cp2/cp2_all_operations.py",
            "checks": h.checks, "operations": operations, "history": h.history,
            "failure": failure,
            "not_tested": ["B UI and two-human operation", "real CPU/GPU execution",
                           "exhaustive OpenAPI schema conformance", "complete joint CP2 gate"],
        }
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(f"{status}: {args.output}")
    if failure:
        print(failure, file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
