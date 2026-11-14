#!/usr/bin/env python3
"""Recompute ordinary client-observed latency from the declared CP4 TCP load."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from _load_runner import ROOT, TARGET, current_build, inspect, reference_limits, write_json


def review(root: Path, receipt_path=None):
    path = receipt_path or root / "evidence/cp4/latency_receipt.json"
    receipt = json.loads(path.read_text())
    assert receipt["check"] == "CP4-LATENCY" and receipt["build"] == current_build(root)
    result = inspect(root, receipt["evidence"])
    assert result["source_unchanged"], "Build changed during load"
    assert reference_limits(result["run"]["environment"]), "No observed cgroup 4-vCPU / 8-GiB resource limit"
    assert result["observed"] == TARGET and result["lifecycle_consistent"], "Incomplete declared workload"
    assert result["declared_query_sample_complete"], "Missing or changed declared 1250-job-query sample"
    assert not result["request_errors"] and not result["run"]["errors"], "Failed requests excluded from latency would bias the result"
    assert result["ordinary"]["p95_ms"] < 200, "Ordinary combined p95 exceeds 200 ms"
    assert all(v["p95_ms"] < 200 for v in result["by_operation"].values()), "An ordinary operation p95 exceeds 200 ms"
    return {"check": "CP4-LATENCY", "status": "PASS", "build": current_build(root),
            "actual": {"environment": result["run"]["environment"], "ordinary": result["ordinary"],
                       "by_operation": result["by_operation"], "observed_load": result["observed"],
                       "throughput_requests_per_second": result["throughput_requests_per_second"],
                       "excluded_workloads": result["run"]["excluded_workloads"]},
            "evidence": receipt["evidence"], "unresolved_issues": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp4/latency_receipt.json")
    parser.add_argument("--scale-receipt", type=Path, default=ROOT / "evidence/cp4/scale_receipt.json")
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    if args.review_only:
        try:
            result = review(ROOT, args.output)
            print(json.dumps({"check": "CP4-LATENCY", "status": "PASS", "ordinary": result["actual"]["ordinary"]}))
            return 0
        except (OSError, ValueError, KeyError, AssertionError) as exc:
            print(json.dumps({"check": "CP4-LATENCY", "status": "NOT_MET", "reason": str(exc)}))
            return 1
    receipt = {"schema_version": "1.0.0", "check": "CP4-LATENCY", "evidence_kind": "load_test",
               "build": current_build(ROOT), "status": "NOT_RUN", "observed_at": datetime.now(timezone.utc).isoformat(),
               "checks": [], "unresolved_issues": [], "evidence": []}
    try:
        scale = json.loads(args.scale_receipt.read_text())
        receipt["evidence"] = scale["evidence"]
        result = inspect(ROOT, receipt["evidence"])
        receipt["actual"] = {"environment": result["run"]["environment"], "ordinary": result["ordinary"],
                             "by_operation": result["by_operation"], "observed_load": result["observed"],
                             "throughput_requests_per_second": result["throughput_requests_per_second"],
                             "excluded_workloads": result["run"]["excluded_workloads"]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, receipt)
        review(ROOT, args.output)
        receipt["status"] = "PASS"
    except Exception as exc:
        receipt["status"] = "NOT_MET"
        receipt["unresolved_issues"].append(str(exc))
    receipt["build_at_finish"] = current_build(ROOT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, receipt)
    print(json.dumps({"check": receipt["check"], "status": receipt["status"], "output": str(args.output),
                      "actual": receipt.get("actual"), "unresolved_issues": receipt["unresolved_issues"]}))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
