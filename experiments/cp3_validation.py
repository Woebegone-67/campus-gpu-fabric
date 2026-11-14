"""Registered CP3 policy validators share a raw-artifact recomputation path."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from campus_fabric.workflows.acceptance_evidence import _gate_module
from experiments.cp3_suite import ROOT, audit_suite, digest, write_json


def main(check):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "evidence/cp3/policy-suite")
    suffix = "policy_evidence" if check == "CP3-POLICY-EVIDENCE" else "policy_target"
    parser.add_argument("--output", type=Path, default=ROOT / f"evidence/cp3/{suffix}_receipt.json")
    args = parser.parse_args()
    build = _gate_module(ROOT).current_build(ROOT)
    receipt = {"schema_version": "1.0.0", "check": check, "evidence_kind": "paired_experiment",
               "build": build, "status": "NOT_RUN", "observed_at": datetime.now(timezone.utc).isoformat(),
               "checks": [], "scope": "Recalculation of sealed logical-kernel traces; no real CPU/GPU claim"}
    try:
        analysis = audit_suite(ROOT, args.suite)
        recalculated = args.output.with_name(suffix + "_recalculated.json")
        write_json(recalculated, analysis)
        evidence = {"path": str(recalculated.relative_to(ROOT)), "sha256": digest(recalculated.read_bytes())}
        def record(identity, passed, observed):
            receipt["checks"].append({"id": identity, "status": "PASS" if passed else "FAIL", "observed": observed, "evidence": evidence})
        record("sealed-validation-partition", analysis["partition"] == "validation", analysis["partition"])
        record("complete-hash-checked-execution", analysis["evidence_complete"], analysis["run_counts"])
        if check == "CP3-POLICY-EVIDENCE":
            record("five-family-semantic-coverage", analysis["coverage_complete"], analysis["coverage"])
            record("opportunity-off-identity", analysis["instrumentation"].get("baseline_ablation_identity_violations") == 0,
                   analysis["instrumentation"])
        else:
            target = analysis["comparisons"]["B2"]["target"]
            record("R-at-most-0.85", target["ratio_met"], analysis["comparisons"]["B2"]["aggregate"])
            record("paired-completion-counts", target["paired_completion_not_lower"], target)
            record("no-new-severe-work-or-owner-harm", not target["work_severe_harms"] and not target["owner_severe_harms"], target)
        receipt["status"] = "PASS" if all(c["status"] == "PASS" for c in receipt["checks"]) else "FAIL"
        receipt["limitations"] = analysis["limitations"]
    except (FileNotFoundError, ValueError, KeyError, TypeError, OSError) as exc:
        receipt["status"] = "NOT_RUN" if isinstance(exc, FileNotFoundError) else "FAIL"
        receipt["checks"].append({"id": "sealed-input-and-trace-audit", "status": "FAIL", "observed": str(exc)})
    receipt["build_at_finish"] = _gate_module(ROOT).current_build(ROOT)
    if receipt["build_at_finish"] != build:
        receipt["status"] = "STALE"
    write_json(args.output, receipt)
    print(json.dumps({"check": check, "status": receipt["status"], "output": str(args.output)}))
    return 0 if receipt["status"] == "PASS" else 1
