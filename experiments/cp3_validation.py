"""Registered CP3 policy validators share a raw-artifact recomputation path."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from campus_fabric.workflows.acceptance_evidence import _gate_module
from experiments.cp3_suite import (
    ROOT,
    audit_suite,
    digest,
    load_suite_index,
    read_artifact,
    read_artifact_bytes,
    write_json,
)


def suite_evidence(root: Path, suite: Path) -> list[dict[str, str]]:
    """Bind a receipt to every sealed input and trace, not only its summary."""
    root, suite = Path(root).resolve(), Path(suite).absolute()
    relative_suite = suite.relative_to(root)
    if not relative_suite.is_relative_to(Path("evidence/cp3")):
        raise ValueError("Policy suite must be inside repository evidence/cp3")
    index_path = suite / "suite-index.json"
    index_bytes = index_path.read_bytes()
    index = load_suite_index(suite, root=root)
    manifest = read_artifact(suite, "manifest.json", index["manifest_sha256"])
    refs = [{"path": "manifest.json", "sha256": index["manifest_sha256"]},
            *({"path": entry["path"], "sha256": entry["sha256"]} for entry in manifest["inputs"]),
            *(ref for row in index["runs"] for ref in row["artifacts"].values())]
    if index.get("analysis"):
        refs.append(index["analysis"])
    evidence = {}
    for ref in refs:
        read_artifact_bytes(suite, ref["path"], ref["sha256"])
        evidence[str(relative_suite / ref["path"])] = ref["sha256"]
    if index_path.read_bytes() != index_bytes:
        raise ValueError("Suite index changed while collecting evidence")
    evidence[str(relative_suite / "suite-index.json")] = digest(index_bytes)
    evidence[str(relative_suite / "manifest.sha256")] = digest((suite / "manifest.sha256").read_bytes())
    return [{"path": path, "sha256": sha} for path, sha in sorted(evidence.items())]


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
        sealed_evidence = suite_evidence(ROOT, args.suite)
        analysis = audit_suite(ROOT, args.suite)
        if suite_evidence(ROOT, args.suite) != sealed_evidence:
            raise ValueError("Sealed suite changed during its audit")
        recalculated = args.output.with_name(suffix + "_recalculated.json")
        write_json(recalculated, analysis)
        evidence = {"path": str(recalculated.relative_to(ROOT)), "sha256": digest(recalculated.read_bytes())}
        receipt["evidence"] = [*sealed_evidence, evidence]
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
