#!/usr/bin/env python3
"""Run A's CP3 reliability gate plus integrated CP0--CP2 regressions.

This runner cannot grant joint CP3: policy, prefix replay, explanations and
product settlement require separate B and joint evidence on the integrated build.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from check_checkpoint import current_build, sha256_file

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/cp3"
RECEIPTS = {
    "CP3-CRASH-RECOVERY": "crash_recovery_receipt.json",
    "CP3-CPU-EQUIVALENCE": "cpu_equivalence_receipt.json",
    "CP3-INDEPENDENT-MODEL": "independent_model_receipt.json",
}
PENDING = {"CP3-PREFIX-REPLAY", "CP3-DECISION-COVERAGE", "CP3-POLICY-EVIDENCE",
           "CP3-POLICY-TARGET", "CP3-SETTLEMENT"}


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def artifact(path):
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256_file(path)}


def valid_requirements(requirements):
    ids = [item["id"] for item in requirements]
    return len(ids) == len(set(ids)) and set(ids) == set(RECEIPTS) | PENDING


def valid_receipt(run, receipt, build, current, cid):
    return (run["status"] == "PASS" and receipt.get("status") == "PASS"
            and receipt.get("schema_version") == "1.0.0" and receipt.get("check") == cid
            and receipt.get("build") == build == current == receipt.get("build_at_finish")
            and isinstance(receipt.get("checks"), list) and bool(receipt["checks"])
            and all(check.get("status") == "PASS" for check in receipt["checks"]))


def run_command(args, stream, timeout):
    # An ordinary timeout should let validator finally blocks reap CPU workers,
    # rather than kill only the parent and orphan its independent sessions.
    with subprocess.Popen(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            return 124


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "logs").mkdir(exist_ok=True)
    build = current_build(ROOT)
    runs, checks = [], []
    joint = {"schema_version": "1.0.0", "checkpoint": "CP3", "scope": "runtime_acceptance",
             "status": "NOT_MET", "build": build, "checks": checks}
    write(OUT / "receipt.json", joint)
    write(OUT / "a_core_receipt.json", {
        "schema_version": "1.0.0", "scope": "A_CP3_RELIABILITY_ONLY", "status": "RUNNING",
        "build": build, "joint_cp3": "NOT_MET", "runs": []})

    requirements = json.loads((ROOT / "configs/checkpoints.json").read_text())["checkpoints"]["CP3"]["required_checks"]
    if not valid_requirements(requirements):
        write(OUT / "a_core_receipt.json", {
            "schema_version": "1.0.0", "scope": "A_CP3_RELIABILITY_ONLY", "status": "FAIL",
            "build": build, "joint_cp3": "NOT_MET",
            "error": "CP3 must contain all eight unique registered gates."})
        print("FAIL: missing, duplicate or unexpected CP3 gates.", flush=True)
        return 1

    def execute(name, args, timeout=300):
        log = OUT / "logs" / (name + ".txt")
        print("RUN " + name, flush=True)
        try:
            with log.open("w") as stream:
                code = run_command(args, stream, timeout)
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
    # CP2 A includes all Python tests, contracts, lint, web build/client tests and
    # original CP0/CP1/CP2 runtime checks. B regression is code-driven, not a new
    # human attestation. Earlier CP2 human closeout remains in its frozen tree.
    execute("cp2-a-regression", [py, "scripts/accept_cp2_a.py"], timeout=1200)
    execute("web-ui-regression", ["npm", "--prefix", "web", "run", "test:ui"])
    execute("cp2-b-regression", [py, "scripts/accept_cp2_b.py"], timeout=600)
    # B's CPU-mode 409 report is a regression, not a ninth joint CP3 gate.
    overview_path = OUT / "overview_snapshot_receipt.json"
    overview_path.unlink(missing_ok=True)
    overview_run = execute("overview-snapshot-regression", [py,
        "tests/acceptance/cp3/cp3_overview_snapshot.py", "--output", str(overview_path)])
    try:
        overview_valid = valid_receipt(overview_run, json.loads(overview_path.read_text()),
                                       build, current_build(ROOT), "CP3-OVERVIEW-SNAPSHOT")
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        overview_valid = False
    if overview_path.is_file():
        overview_run["receipt"] = artifact(overview_path)
    if not overview_valid:
        overview_run["status"] = "FAIL"
        overview_run["validation_error"] = "Overview regression receipt is missing, failed or stale."
        print("FAIL overview-snapshot-regression receipt validation", flush=True)
    for requirement in requirements:
        cid = requirement["id"]
        check = {"id": cid, "target_id": requirement["target_id"], "build": build,
                 "expected": requirement["expected"], "status": "NOT_RUN"}
        checks.append(check)
        if cid not in RECEIPTS:
            check["unresolved_issues"] = ["Requires B and joint CP3 evidence; A reliability tests do not establish this check."]
            continue
        validator = ROOT / requirement["validator_path"]
        receipt_path = OUT / RECEIPTS[cid]
        receipt_path.unlink(missing_ok=True)
        run = execute(cid.lower(), [py, str(validator), "--output", str(receipt_path)])
        try:
            receipt = json.loads(receipt_path.read_text())
            valid = valid_receipt(run, receipt, build, current_build(ROOT), cid)
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            valid, receipt = False, {}
        check.update(
            status="MET" if valid else "NOT_MET",
            evidence_kind=requirement["allowed_evidence_kinds"][0],
            validator={"path": requirement["validator_path"], "sha256": sha256_file(validator)},
            input_version="Official participant v2.0.0 and deterministic cases/seeds declared in registered validator",
            executor="Fresh isolated process execution with independent assertions",
            command=run["command"], observed_at=run["observed_at"],
            actual=f"{len(receipt.get('checks', []))} executed checks; registered receipt status {receipt.get('status', 'missing')}",
            unresolved_issues=[] if valid else ["Registered validator failed, is stale, or has incomplete observations."],
            evidence=[run["evidence"]] + ([artifact(receipt_path)] if receipt_path.is_file() else []),
        )
        write(OUT / "receipt.json", joint)
    stable = current_build(ROOT) == build
    covered = {check["id"] for check in checks if check["status"] == "MET"}
    passed = stable and covered == set(RECEIPTS) and all(run["status"] == "PASS" for run in runs) and all(
        check["status"] == "MET" for check in checks if check["id"] in RECEIPTS)
    pending = [check["id"] for check in checks if check["id"] not in RECEIPTS]
    write(OUT / "receipt.json", joint)
    write(OUT / "a_core_receipt.json", {
        "schema_version": "1.0.0", "scope": "A_CP3_RELIABILITY_ONLY",
        "status": "PASS" if passed else "FAIL", "build": build,
        "created_at": datetime.now(timezone.utc).isoformat(), "source_stable": stable,
        "runs": runs, "core_gate_count": len(RECEIPTS), "joint_cp3": "NOT_MET",
        "pending": pending,
        "boundary": "A reliability pass is not policy success, full event replay or joint product acceptance.",
    })
    print(f"A CP3: {'PASS' if passed else 'FAIL'}. Joint CP3: NOT_MET; pending {', '.join(pending)}.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
