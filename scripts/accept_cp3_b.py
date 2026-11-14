#!/usr/bin/env python3
"""Execute B's CP3 validators and assemble the joint gate without inventing A proof.

Freeze/run/analyze experiments separately first. A numerical target failure or
an uncovered replay requirement remains NOT_MET, even when engineering tests pass.
Existing A receipts are usable only on the exact current build.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from accept_cp3_a import run_command
from check_checkpoint import (
    _nested_evidence_errors,
    current_build,
    evaluate_checkpoint,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/cp3"
B_CHECKS = {
    "CP3-PREFIX-REPLAY": "prefix_replay_receipt.json",
    "CP3-DECISION-COVERAGE": "decision_coverage_receipt.json",
    "CP3-POLICY-EVIDENCE": "policy_evidence_receipt.json",
    "CP3-POLICY-TARGET": "policy_target_receipt.json",
    "CP3-SETTLEMENT": "settlement_receipt.json",
}
A_CHECKS = {
    "CP3-CRASH-RECOVERY": "crash_recovery_receipt.json",
    "CP3-CPU-EQUIVALENCE": "cpu_equivalence_receipt.json",
    "CP3-INDEPENDENT-MODEL": "independent_model_receipt.json",
}


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def artifact(path):
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256_file(path)}


def valid_observation(raw, cid, build, allowed_kinds):
    if not isinstance(raw, dict):
        return False
    observations = raw.get("checks")
    return (raw.get("schema_version") == "1.0.0" and raw.get("check") == cid
            and raw.get("build") == build == raw.get("build_at_finish")
            and isinstance(observations, list) and bool(observations)
            and all(isinstance(c, dict) and isinstance(c.get("id"), str)
                    and c["id"] for c in observations)
            and len({c["id"] for c in observations}) == len(observations)
            and isinstance(raw.get("unresolved_issues", []), list)
            # Legacy A validators omit this field; an explicit wrong kind is
            # never replaced by a supposedly allowed kind in the joint receipt.
            and (raw.get("evidence_kind") in allowed_kinds
                 or ("evidence_kind" not in raw and cid in {"CP3-CRASH-RECOVERY", "CP3-CPU-EQUIVALENCE"})))


def main(*, review_existing=False):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "logs").mkdir(exist_ok=True)
    build = current_build(ROOT)
    required = json.loads((ROOT / "configs/checkpoints.json").read_text())["checkpoints"]["CP3"]["required_checks"]
    if (len(required) != 8
            or {r["id"] for r in required} != set(B_CHECKS) | set(A_CHECKS)):
        raise ValueError("Expected all eight unique CP3 registered checks")
    previous = {}
    if review_existing:
        try:
            previous = json.loads((OUT / "b_delivery_receipt.json").read_text())
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
    retained_runs = previous.get("retained_runs", previous.get("runs", []))
    if not isinstance(retained_runs, list):
        retained_runs = []
    checks, runs = [], []
    joint = {"schema_version": "1.0.0", "checkpoint": "CP3", "scope": "runtime_acceptance",
             "status": "NOT_MET", "build": build, "checks": checks}
    write(OUT / "receipt.json", joint)
    for requirement in required:
        cid = requirement["id"]
        validator = ROOT / requirement["validator_path"]
        raw_path = OUT / (B_CHECKS | A_CHECKS)[cid]
        command = shlex.join([sys.executable, str(validator), "--output", str(raw_path)])
        log = OUT / "logs" / (cid.lower() + ".txt")
        code = None
        review_error = None
        if cid in B_CHECKS and review_existing:
            prior_runs = [r for r in retained_runs if isinstance(r, dict) and r.get("id") == cid]
            prior_rows = [r for r in previous.get("checks", [])
                          if isinstance(r, dict) and r.get("id") == cid] if isinstance(previous.get("checks"), list) else []
            if (previous.get("build") != build or previous.get("build_at_finish") != build
                    or len(prior_runs) != 1 or prior_runs[0].get("exit_code") != 0
                    or len(prior_rows) != 1 or prior_rows[0].get("status") != "MET"):
                review_error = "No successful exact-build execution provenance; run the B validators first."
            else:
                prior_evidence = prior_rows[0].get("evidence")
                if (not isinstance(prior_evidence, list) or not prior_evidence
                        or not any(isinstance(ref, dict) and ref.get("path") == raw_path.relative_to(ROOT).as_posix()
                                   for ref in prior_evidence)
                        or _nested_evidence_errors(ROOT, "CP3", prior_evidence, OUT / "receipt.json")):
                    review_error = "Retained execution evidence changed or is incomplete; rerun the B validators."
            if review_error:
                code = 1
        if cid in B_CHECKS and not review_existing:
            raw_path.unlink(missing_ok=True)
            print("RUN " + cid, flush=True)
            with log.open("w") as stream:
                code = run_command([sys.executable, str(validator), "--output", str(raw_path)],
                                   stream, timeout=600)
            runs.append({"id": cid, "exit_code": code, "command": command,
                         "log": artifact(log)})
        try:
            raw = json.loads(raw_path.read_text())
        except (OSError, ValueError):
            raw = {}
        executed = valid_observation(raw, cid, build, requirement["allowed_evidence_kinds"])
        if not isinstance(raw, dict):
            raw = {}
        observations = raw.get("checks") if isinstance(raw.get("checks"), list) else []
        issues = raw.get("unresolved_issues") if isinstance(raw.get("unresolved_issues"), list) else []
        passed = (executed and raw.get("status") == "PASS"
                  and all(c.get("status") == "PASS" for c in raw["checks"])
                  and not issues
                  and (code in (0, None)) and current_build(ROOT) == build)
        row = {"id": cid, "target_id": requirement["target_id"], "build": build,
               "expected": requirement["expected"],
               "status": "MET" if passed else ("NOT_MET" if executed else "NOT_RUN"),
               "evidence_kind": requirement["allowed_evidence_kinds"][0],
               "input_version": "CP3 registered deterministic validators and sealed five-family protocol",
               "executor": ("Review of retained exact-build validator receipts; no new execution" if review_existing
                            else "Independent isolated validator process; no human sign-off inferred"),
               "command": command,
               "observed_at": datetime.now(timezone.utc).isoformat(),
               "actual": f"{len(observations)} recorded checks; validator status {raw.get('status', 'missing or stale')}",
               "unresolved_issues": [] if passed else issues or [
                   "No passing exact-build evidence, or a registered condition remains unmet."],
               "evidence": ([artifact(raw_path)] if raw_path.is_file() else [])
                           + ([artifact(log)] if cid in B_CHECKS and log.is_file() else [])}
        if review_error:
            row["unresolved_issues"] = [review_error]
        if passed:
            nested_errors = _nested_evidence_errors(ROOT, "CP3", row["evidence"], OUT / "receipt.json")
            if nested_errors:
                row["status"] = "NOT_MET"
                row["unresolved_issues"] = nested_errors
        if validator.is_file():
            row["validator"] = {"path": requirement["validator_path"], "sha256": sha256_file(validator)}
        checks.append(row)
        print(f"{row['status']} {cid}", flush=True)
        write(OUT / "receipt.json", joint)
    stable = current_build(ROOT) == build
    if not stable:
        for check in checks:
            check["status"] = "NOT_MET"
            check["unresolved_issues"] = ["Source changed during acceptance; rerun on a stable build."]
    joint["status"] = "MET" if stable and all(c["status"] == "MET" for c in checks) else "NOT_MET"
    write(OUT / "receipt.json", joint)
    gate = evaluate_checkpoint(ROOT, "CP3")
    joint["status"] = gate["status"]
    joint["technical_status"] = gate.get("technical_status", "NOT_MET")
    joint["human_acceptance"] = gate.get("human_acceptance", "NOT_RUN")
    write(OUT / "receipt.json", joint)
    write(OUT / "joint_gate_review.json", gate)
    b_passed = stable and all(c["status"] == "MET" for c in checks if c["id"] in B_CHECKS)
    write(OUT / "b_delivery_receipt.json", {
        "schema_version": "1.0.0", "scope": "B_CP3_REGISTERED_VALIDATORS",
        "build": build, "build_at_finish": current_build(ROOT),
        "status": "PASS" if b_passed else "NOT_MET", "runs": runs,
        "operation": "review_existing" if review_existing else "execute_validators",
        "retained_runs": retained_runs if review_existing else runs,
        "checks": checks, "joint_cp3": gate["status"],
        "human_acceptance": gate.get("human_acceptance", "NOT_RUN"),
        "boundary": "Observed policy outcomes and agent UI checks do not sign human acceptance.",
    })
    print(f"B CP3 {'PASS' if b_passed else 'NOT_MET'}; joint gate {gate['status']}", flush=True)
    return 0 if b_passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-existing", action="store_true",
                        help="Recheck retained exact-build receipts and CP3 human evidence without rerunning validators")
    raise SystemExit(main(review_existing=parser.parse_args().review_existing))
