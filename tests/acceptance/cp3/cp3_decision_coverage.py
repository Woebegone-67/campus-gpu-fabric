#!/usr/bin/env python3
"""Actual loopback HTTP journeys cross-checked with submitted plans and receipts.

Decision submission is an internal worker port in the product, not a public HTTP
API. This validator triggers that port through real HTTP registration and reads
actual explanations through the real query API. It does not certify browser UI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, inventory_node, registration  # noqa: E402
from cp3_b_evidence import finish, receipt, retain  # noqa: E402

POLICIES = ("B0", "B1", "B2", "P")
CASES = ("accepted", "budget", "stale", "illegal-placement")


def candidate_coverage(plan):
    examined = [c for c in plan["candidates"] if c["feasibility"] != "unexamined"]
    assert plan["coverage"]["examined_count"] == len(examined)
    assert plan["coverage"]["scope"] == "partial"
    assert plan["coverage"]["truncation_reason"]
    assert not plan["asserts_pool_infeasible"]
    return examined


def exercise(directory, policy, case):
    directory.mkdir(parents=True, exist_ok=True)
    h = Harness(directory, app_target="cp3_decision_fixture:create_app")
    node = inventory_node()
    node["capacity"]["gpu_count"] = node["available"]["gpu_count"] = 2
    node["capacity"]["gpu_memory_mib"] = node["available"]["gpu_memory_mib"] = 16384
    node["devices"].append({**node["devices"][0], "device_id": "second-gpu", "physical_gpu_id": "second-gpu", "model": "A100"})
    (directory / "inventory.json").write_text(json.dumps({"nodes": [node]}))
    capture_path = directory / "decision_trace.json"
    h.env.update(FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY=policy,
        CP3_DECISION_CASE=case, CP3_DECISION_TRACE=str(capture_path),
        PYTHONPATH=os.pathsep.join((str(ROOT / "tests/acceptance/cp3"), str(ROOT / "src"), str(ROOT / "scripts"))))
    try:
        h.start()
        name = f"coverage-{policy.lower()}-{case}"
        h.request("POST", "/x/v1/work-intents", body=registration(name), expected=202)
        trace = json.loads(capture_path.read_text())
        assert len(trace["submissions"]) == 1, trace
        attempted = trace["submissions"][0]
        plan = attempted["command"]["parameters"]["plan"]
        examined = candidate_coverage(plan)
        assert len(plan["candidates"]) <= (1 if case == "budget" else 32)
        decision = plan["decision_id"]
        if case in {"stale", "illegal-placement"}:
            error = "stale_version" if case == "stale" else "insufficient_resources"
            assert attempted["receipt"]["outcome"] == "rejected"
            assert attempted["receipt"]["error"]["code"] == error
            assert attempted["before"] == attempted["after"], "Rejected decision changed visible authoritative state"
            h.request("GET", f"/x/v1/decisions/{decision}", actor="admin", expected=404)
            detail = h.get(f"/x/v1/work-intents/{name}")
            assert detail["allocations"][0]["state"] == "Pending"
            assert not any(e.get("decision_id") == decision for e in detail["events"])
            assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 2
            observation = f"Actual {error} rejection, unchanged snapshot/capacity, no committed explanation or decision-linked event."
        else:
            assert attempted["receipt"]["outcome"] == "committed"
            admin = h.get(f"/x/v1/decisions/{decision}", "admin")
            owner = h.get(f"/x/v1/decisions/{decision}")
            h.request("GET", f"/x/v1/decisions/{decision}", actor="b", expected=404)
            assert admin["item"] == plan, "Administrator explanation differs from the actual accepted plan"
            assert admin["commit_status"] == owner["commit_status"] == "committed"
            selected = next(c for c in examined if c["candidate_id"] == plan["selected_candidate_id"])
            granted = [e for e in admin["actual_events"] if e["event_type"] == "allocation.granted"]
            assert len(granted) == 1
            grant = granted[0]
            assert grant["event_id"] in attempted["receipt"]["event_ids"]
            assert grant["decision_snapshot_version"] == plan["snapshot_version"]
            assert grant["decision_id"] == decision
            assert {d["allocation_id"] for d in grant["resource_deltas"]} == {selected["allocation_id"]}
            placed = sorted(device for piece in selected["placement"]["slices"] for device in piece["device_ids"])
            assert sorted(device for delta in grant["resource_deltas"] for device in delta["device_ids_added"]) == placed
            for dimension in ("cpu_millicores", "memory_mib", "ephemeral_storage_mib"):
                assert sum(d[dimension] for d in grant["resource_deltas"]) == sum(
                    p["resources"][dimension] for p in selected["placement"]["slices"])
            detail = h.get(f"/x/v1/work-intents/{name}")
            allocation = next(a for a in detail["allocations"] if a["id"] == selected["allocation_id"])
            # The accepted event delta is the authoritative applied placement;
            # the product allocation detail must identify that same lease.
            assert allocation["state"] == "Running"
            assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
            candidate_coverage(owner["item"])
            assert all(c["owner_id"] == "alice" and not c["scores"] for c in owner["item"]["candidates"])
            assert owner["item"]["selected_candidate_id"] == plan["selected_candidate_id"]
            if case == "budget":
                assert len(examined) == 1
                assert "budget" in plan["coverage"]["truncation_reason"].lower()
            else:
                assert any(c["feasibility"] == "infeasible" for c in examined), "Fixture must exercise excluded candidates"
            observation = "Accepted plan equals HTTP administrator explanation; selected placement matches committed grant; owner explanation is explicitly partial and another owner receives 404."
            if case == "budget":
                observation += " Candidate budget 1 is retained as partial coverage, never pool-wide infeasibility."
        return {"id": f"{policy}-{case}", "status": "PASS", "observed": observation}, trace, h.history
    except Exception:
        trace = json.loads(capture_path.read_text()) if capture_path.exists() else {}
        trace["failure"] = h.redact(traceback.format_exc())
        return {"id": f"{policy}-{case}", "status": "FAIL", "observed": trace["failure"]}, trace, h.history
    finally:
        h.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp3/decision_coverage_receipt.json")
    args = parser.parse_args(argv)
    result = receipt("CP3-DECISION-COVERAGE", "http_blackbox", {
        "transport": "Real loopback TCP product registration and decision/capacity queries; not TestClient.",
        "acceptance": "Product worker submits plans through the unchanged A command port; receipt and before/after snapshot captured by test-only assembly.",
        "fault_injection": "Test policy adapter proposes a stale version or illegal placement; no new HTTP endpoint or production override.",
        "limits": "Finite fixtures for B0/B1/B2/P; candidate-budget declarations, actual grant and rejection integrity, and owner isolation. Browser rendering and exhaustive domain completeness are not certified."})
    directory = args.output.resolve().parent / (args.output.stem + "_histories")
    result["evidence"] = []
    with tempfile.TemporaryDirectory(prefix="fabric-cp3-decisions-") as temp:
        for policy in POLICIES:
            for case in CASES:
                check, trace, history = exercise(Path(temp) / f"{policy}-{case}", policy, case)
                result["checks"].append(check)
                result["evidence"].append(retain(directory, f"{policy}-{case}", {"server_capture": trace, "http_history": history}))
    result["status"] = "PASS" if all(c["status"] == "PASS" for c in result["checks"]) else "FAIL"
    if result["status"] != "PASS":
        result["unresolved_issues"].append("At least one HTTP decision case failed; full raw response and server capture retained.")
    return finish(args.output, result)


if __name__ == "__main__":
    raise SystemExit(main())
