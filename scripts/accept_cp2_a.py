#!/usr/bin/env python3
"""Execute A's CP2 acceptance; leave the joint UI gate unaccepted.

Exit zero means A's backend checks passed for this exact build. It never grants
CP2 as a whole or imports a previous run as fresh evidence.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from check_checkpoint import current_build, sha256_file

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/cp2"
RECEIPTS = {
    "CP2-ALL-OPERATIONS": "http_operations_receipt.json",
    "CP2-PREEMPTION": "cp2_preemption_receipt.json",
    "CP2-CONTRIBUTOR": "cp2_contributor_receipt.json",
    "CP2-CLOCK-BOUNDARIES": "cp2_clock_boundaries_receipt.json",
    "CP2-RESET-ISOLATION": "cp2_reset_isolation_receipt.json",
    "CP2-NODE-FAILURE": "cp2_node_failure_receipt.json",
    "CP2-FAULTS": "cp2_faults_receipt.json",
    "CP2-CPU-REFERENCE": "cpu_tcp_receipt.json",
}


def artifact(path):
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256_file(path)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "logs").mkdir(exist_ok=True)
    build = current_build(ROOT)
    runs = []
    # Interrupted or unlaunchable runs must never leave an earlier aggregate PASS.
    (OUT / "a_core_receipt.json").write_text(json.dumps({
        "schema_version": "1.0.0", "scope": "A_CP2_BACKEND_ONLY", "status": "RUNNING",
        "build": build, "joint_cp2": "NOT_MET", "runs": [],
    }, indent=2) + "\n")
    (OUT / "receipt.json").write_text(json.dumps({
        "schema_version": "1.0.0", "checkpoint": "CP2", "scope": "runtime_acceptance",
        "status": "NOT_MET", "build": build, "checks": [],
    }, indent=2) + "\n")

    def execute(name, args):
        log = OUT / "logs" / (name + ".txt")
        print("RUN " + name, flush=True)
        try:
            with log.open("w") as stream:
                result = subprocess.run(args, cwd=ROOT, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=240, check=False)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
        except OSError as exc:
            log.write_text(f"Cannot execute {args[0]}: {exc}\n")
            code = 127
        row = {"id": name, "status": "PASS" if code == 0 else "FAIL", "exit_code": code,
               "command": shlex.join(args), "observed_at": datetime.now(timezone.utc).isoformat(),
               "evidence": artifact(log)}
        runs.append(row)
        print(f"{row['status']} {name}", flush=True)
        if code:
            print(log.read_text()[-5000:], flush=True)
        return row

    py = sys.executable
    for name, args in [
        ("contracts", [py, "scripts/check_generated.py"]),
        ("lint", [py, "-m", "ruff", "check", "src", "scripts", "tests", "--no-cache"]),
        ("python", [py, "-m", "pytest", "-q", "--junitxml=evidence/cp2/unit_tests.xml"]),
        ("web-build", ["npm", "--prefix", "web", "run", "build"]),
        ("web-client", ["npm", "--prefix", "web", "run", "test:client"]),
        ("cp0-static", [py, "scripts/check_cp0.py"]),
        ("cp0-http", [py, "scripts/smoke_cp0.py"]),
        ("cp1-core-regression", [py, "scripts/accept_cp1_a.py"]),
        ("cp1-query-regression", [py, "scripts/accept_cp1_joint.py"]),
    ]:
        execute(name, args)

    requirements = json.loads((ROOT / "configs/checkpoints.json").read_text())["checkpoints"]["CP2"]["required_checks"]
    checks = []
    for requirement in requirements:
        cid = requirement["id"]
        check = {"id": cid, "target_id": requirement["target_id"], "build": build,
                 "expected": requirement["expected"], "status": "NOT_RUN"}
        checks.append(check)
        if cid not in RECEIPTS:
            check["unresolved_issues"] = ["B six-workspace, three-role dynamic UI journeys have not been accepted on this CP2 build."]
            continue
        validator = ROOT / requirement["validator_path"]
        receipt_path = OUT / RECEIPTS[cid]
        # Never allow a missing/newly failed run to inherit an older PASS.
        receipt_path.unlink(missing_ok=True)
        run = execute(cid.lower(), [py, str(validator), "--output", str(receipt_path)])
        try:
            receipt = json.loads(receipt_path.read_text())
            valid = (run["status"] == "PASS" and receipt["status"] == "PASS"
                     and receipt["build"] == build and current_build(ROOT) == build)
        except (OSError, KeyError, ValueError):
            valid, receipt = False, {}
        check.update(
            status="MET" if valid else "NOT_MET",
            evidence_kind="http_blackbox" if "http_blackbox" in requirement["allowed_evidence_kinds"]
            else requirement["allowed_evidence_kinds"][0],
            validator={"path": requirement["validator_path"], "sha256": sha256_file(validator)},
            input_version="official participant v2.0.0 frozen spec; isolated runtime fixture in registered validator",
            executor="Fresh local process execution with test-specific independent assertions",
            command=run["command"], observed_at=run["observed_at"],
            actual=f"{len(receipt.get('checks') or receipt.get('selected_tests', []))} executed checks; raw receipt status {receipt.get('status', 'missing')}",
            unresolved_issues=[] if valid else ["Registered validator failed, is stale, or did not emit a valid receipt."],
            evidence=[run["evidence"]] + ([artifact(receipt_path)] if receipt_path.is_file() else []),
        )
    stable = current_build(ROOT) == build
    passed = stable and all(r["status"] == "PASS" for r in runs) and all(
        c["status"] == "MET" for c in checks if c["id"] in RECEIPTS)
    joint = {"schema_version": "1.0.0", "checkpoint": "CP2", "status": "NOT_MET",
             "scope": "runtime_acceptance", "build": build, "checks": checks}
    (OUT / "receipt.json").write_text(json.dumps(joint, indent=2) + "\n")
    result = {"schema_version": "1.0.0", "scope": "A_CP2_BACKEND_ONLY",
              "status": "PASS" if passed else "FAIL", "build": build,
              "created_at": datetime.now(timezone.utc).isoformat(), "source_stable": stable,
              "runs": runs, "core_gate_count": 8, "joint_cp2": "NOT_MET",
              "pending": ["CP2-SIX-WORKSPACES"],
              "boundary": "Eight backend checks do not certify B UI or the complete joint checkpoint."}
    (OUT / "a_core_receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"A CP2: {result['status']}. Joint CP2: NOT_MET; CP2-SIX-WORKSPACES remains NOT_RUN.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
