#!/usr/bin/env python3
"""Run/review the full CP4 scale workload without claiming a smaller run passes."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from _load_runner import ROOT, TARGET, current_build, execute, inspect, refs_for, write_json


def review(root: Path, receipt_path=None):
    path = receipt_path or root / "evidence/cp4/scale_receipt.json"
    receipt = json.loads(path.read_text())
    assert receipt["check"] == "CP4-SCALE" and receipt["build"] == current_build(root)
    result = inspect(root, receipt["evidence"])
    assert result["source_unchanged"], "Build changed during load"
    assert result["observed"] == TARGET, f"Incomplete declared scale: {result['observed']}"
    assert result["declared_query_sample_complete"], "Missing or changed declared 1250-job-query sample"
    assert result["lifecycle_consistent"] and not result["request_errors"] and not result["run"]["errors"], "Lifecycle or HTTP failures"
    assert not result["ledger"]["final_held_physical_gpus"], "Leaked GPU claims"
    return {"check": "CP4-SCALE", "status": "PASS", "build": current_build(root),
            "actual": {k: v for k, v in result.items() if k != "run"},
            "evidence": receipt["evidence"], "unresolved_issues": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp4/scale_receipt.json")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--runtime-directory", type=Path, help="New native-runtime directory; default creates and retains a separate temporary database")
    parser.add_argument("--jobs", type=int, default=10000, help="Smaller diagnostic runs remain NOT_MET")
    parser.add_argument("--deadline-seconds", type=int, default=1200)
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    if args.review_only:
        try:
            result = review(ROOT, args.output)
            print(json.dumps({"check": "CP4-SCALE", "status": "PASS", "observed": result["actual"]["observed"]}))
            return 0
        except (OSError, ValueError, KeyError, AssertionError) as exc:
            print(json.dumps({"check": "CP4-SCALE", "status": "NOT_MET", "reason": str(exc)}))
            return 1
    directory = args.directory or ROOT / "evidence/cp4/load" / str(time.time_ns())
    receipt = {"schema_version": "1.0.0", "check": "CP4-SCALE", "evidence_kind": "load_test",
               "build": current_build(ROOT), "status": "NOT_RUN", "observed_at": datetime.now(timezone.utc).isoformat(),
               "checks": [], "unresolved_issues": []}
    try:
        execute(directory, args.jobs, args.deadline_seconds, args.runtime_directory)
        receipt["evidence"] = refs_for(directory)
        result = inspect(ROOT, receipt["evidence"])
        receipt["actual"] = {k: v for k, v in result.items() if k != "run"}
        receipt["status"] = "PASS" if (result["observed"] == TARGET and result["source_unchanged"]
            and result["lifecycle_consistent"] and not result["request_errors"] and not result["run"]["errors"]) else "NOT_MET"
        if receipt["status"] != "PASS":
            receipt["unresolved_issues"].append("Declared full scale and all quality conditions were not met; see actual and raw run.")
    except Exception as exc:
        receipt["status"] = "NOT_MET"
        receipt["unresolved_issues"].append(str(exc))
        receipt["evidence"] = refs_for(directory)
    receipt["build_at_finish"] = current_build(ROOT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, receipt)
    print(json.dumps({"check": receipt["check"], "status": receipt["status"], "output": str(args.output),
                      "actual": receipt.get("actual"), "unresolved_issues": receipt["unresolved_issues"]}))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
