#!/usr/bin/env python3
"""Execute B settlement DOM regressions and fresh loopback HTTP journeys.

Synthetic DOM fixtures exercise actual React interactions. Independent HTTP
tests run the assembled service and a real CPU process. Neither is a human
attestation, a live-browser accessibility review, or evidence of GPU execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from check_checkpoint import current_build  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "evidence/cp3/settlement_receipt.json")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "evidence/cp3"):
        parser.error("Output must stay inside this checkout's evidence/cp3 directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = output.parent / (output.stem + "-artifacts")
    evidence.mkdir(parents=True, exist_ok=True)
    before = current_build(ROOT)
    receipt = {
        "schema_version": "1.0.0", "check": "CP3-SETTLEMENT", "evidence_kind": "ui_runtime",
        "scope": "B_CP3_SETTLEMENT_AUTOMATED_DOM_AND_LOOPBACK_HTTP", "status": "NOT_MET",
        "created_at": datetime.now(timezone.utc).isoformat(), "build": before,
        "build_at_finish": None, "checks": [], "unresolved_issues": [], "commands": [],
        "human_acceptance": "NOT_RUN", "browser_review": "NOT_RUN",
        "not_tested": ["two-human CP3 acceptance", "real browser layout/accessibility",
                       "actual GPU execution", "A admin overview 409"],
        "failure": None,
    }
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")

    def run(name, command, cwd, timeout=120, env=None):
        completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                                   text=True, timeout=timeout, check=False)
        log = evidence / (name + ".log")
        log.write_text(completed.stdout + completed.stderr)
        receipt["commands"].append({"argv": command, "cwd": str(cwd),
                                    "exit_code": completed.returncode,
                                    "log": log.relative_to(ROOT).as_posix()})
        if completed.returncode:
            raise AssertionError(f"{name} exited {completed.returncode}; see {log}")
        return completed.stdout

    try:
        ui = run("settlement-ui", [str(ROOT / "web/node_modules/.bin/tsx"), "--tsconfig",
                 "tsconfig.json", "--test", "--test-reporter=tap",
                 "--test-name-pattern=CP3 settlement", "tests/ui.test.tsx"], ROOT / "web")
        passed = re.search(r"^# pass (\d+)$", ui, re.MULTILINE)
        assert passed and int(passed[1]) >= 8, "Settlement DOM cases were not executed"
        assert re.search(r"^# fail 0$", ui, re.MULTILINE), "DOM suite did not report zero failures"
        receipt["checks"].append({"id": "settlement-dom-interactions", "status": "PASS",
            "observed": {"passed": int(passed[1]), "fixture_scope": "synthetic_api_actual_react",
                "covered": ["terminal failure reason", "result before history", "held lease",
                            "pending stop", "cancelled and released", "waiting recovery",
                            "failed stage", "raw uint64 preview", "denied artifact",
                            "identity isolation", "rejected receipt", "receipt after refresh"]}})
        junit = evidence / "settlement-http.xml"
        env = {**os.environ, "CP3_SETTLEMENT_HISTORY_DIR": str(evidence / "http")}
        run("settlement-http", [sys.executable, "-m", "pytest", "-q",
            "tests/blackbox/test_cp3_settlement.py", f"--junitxml={junit}"], ROOT, env=env)
        suites = ET.parse(junit).getroot().findall(".//testsuite")
        count = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
        assert count >= 9 and all(int(suite.attrib.get(key, 0)) == 0
            for suite in suites for key in ("failures", "errors", "skipped"))
        histories = sorted((evidence / "http").glob("*.json"))
        assert len(histories) >= 9, "Missing actual HTTP journey transcripts"
        receipt["checks"].append({"id": "settlement-real-http", "status": "PASS",
            "observed": {"passed": count, "isolated_loopback_instances": len(histories),
                "covered": ["success keep or auto-release", "failure settlement variants",
                            "cancel keep or auto-release", "ready does not imply verified/applied",
                            "waiting does not imply resumed", "same work/lease/job across resume",
                            "actual CPU result", "exact artifact bytes", "owner isolation",
                            "unauthenticated artifact denied", "full capacity restored"]}})
        run("web-build", ["npm", "--prefix", "web", "run", "build"], ROOT)
        receipt["checks"].append({"id": "settlement-web-build", "status": "PASS",
                                  "observed": "TypeScript and Vite production build completed"})
    except Exception:
        receipt["failure"] = traceback.format_exc()
        receipt["unresolved_issues"].append("Automated settlement validation failed; inspect logs")
    after = current_build(ROOT)
    receipt["build_at_finish"] = after
    if before != after:
        receipt["unresolved_issues"].append("Source changed during validation; rerun frozen build")
    receipt["status"] = "FAIL" if receipt["failure"] else "NOT_MET" if before != after else "PASS"
    receipt["evidence"] = [{"path": path.relative_to(ROOT).as_posix(),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                           for path in sorted(evidence.rglob("*")) if path.is_file()]
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"check": receipt["check"], "status": receipt["status"],
                      "output": str(output), "human_acceptance": "NOT_RUN"}))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
