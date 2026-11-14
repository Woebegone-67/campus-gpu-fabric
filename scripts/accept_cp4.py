#!/usr/bin/env python3
"""Run selected CP4 validators or collect current primary-evidence reviews.

This never signs for a person, changes a target or reruns the 125 policy runs.
After a frozen candidate passes technical checks, --collect-only can incorporate
actual human/video evidence without repeating load or CPU experiments.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from accept_cp3_a import run_command
from check_checkpoint import (
    _cp4_invocation_errors,
    current_build,
    evaluate_checkpoint,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/cp4"


def artifact(path):
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256_file(path)}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def record_invocation(latest, record, *, starting=False):
    """An older concurrent attempt must never overwrite the newest attempt."""
    latest.parent.mkdir(parents=True, exist_ok=True)
    archive = latest.parent / f'{record["check"]}-{record["run_id"]}.json'
    with latest.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write(archive, record)
        current = json.loads(latest.read_text()) if not starting and latest.exists() else None
        if starting or current is None or current.get('run_id') == record['run_id']:
            write(latest, record)


def collect():
    build = current_build(ROOT)
    config = json.loads((ROOT / "configs/checkpoints.json").read_text())["checkpoints"]["CP4"]
    checks = []
    for requirement in config["required_checks"]:
        path = ROOT / requirement["validator_path"]
        sys.path.insert(0, str(path.parent))
        try:
            invocation_errors = _cp4_invocation_errors(ROOT, requirement, build)
            if invocation_errors:
                raise ValueError('; '.join(invocation_errors))
            spec = importlib.util.spec_from_file_location("cp4_collect_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            reviewed = module.review(ROOT)
        except FileNotFoundError as error:
            reviewed = {"status": "NOT_RUN", "actual": str(error), "evidence": []}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError,
                AssertionError) as error:
            reviewed = {"status": "NOT_MET", "actual": str(error), "evidence": [],
                        "unresolved_issues": [str(error)]}
        finally:
            sys.path.pop(0)
        status = "MET" if reviewed.get("status") == "PASS" and reviewed.get("build") == build else (
            "NOT_RUN" if reviewed.get("status") == "NOT_RUN" else "NOT_MET")
        row = {"id": requirement["id"], "target_id": requirement["target_id"],
               "expected": requirement["expected"], "status": status, "build": build,
               "evidence_kind": requirement["allowed_evidence_kinds"][0],
               "validator": {"path": requirement["validator_path"],
                             "sha256": sha256_file(path) if path.exists() else ""},
               "input_version": "CP4 declared validator workloads and immutable observed primary artifacts",
               "executor": "Registered read-only CP4 evidence reviewer; human participation only from separate records",
               "command": "uv run python scripts/accept_cp4.py --collect-only",
               "observed_at": reviewed.get("observed_at", datetime.now(timezone.utc).isoformat()),
               "actual": (reviewed.get("actual") if isinstance(reviewed.get("actual"), str)
                          else json.dumps(reviewed.get("actual", {}), ensure_ascii=False,
                                          sort_keys=True)),
               "evidence": reviewed.get("evidence", []),
               "unresolved_issues": [] if status == "MET" else (reviewed.get("unresolved_issues") or ["Required primary evidence is absent, failed or stale"])}
        if status == "NOT_RUN":
            # Reviewing a missing record is not an execution of that requirement.
            for key in ("executor", "command", "observed_at", "actual"):
                row[key] = ""
            row["evidence"] = []
        checks.append(row)
        print(row["id"], status, row["actual"], flush=True)
    stable = current_build(ROOT) == build
    record = {"schema_version": "1.0.0", "checkpoint": "CP4", "scope": "runtime_acceptance",
              "status": "MET" if stable and all(x["status"] == "MET" for x in checks) else "NOT_MET",
              "build": build, "checks": checks}
    write(OUT / "receipt.json", record)
    report = evaluate_checkpoint(ROOT, "CP4")
    write(OUT / "gate_review.json", report)
    return report


def invoke(name):
    """Persist the newest attempt before spawning, including abnormal exits."""
    validator = ROOT / f'tests/acceptance/cp4/cp4_{name}.py'
    check = 'CP4-' + name.replace('_', '-').upper()
    run_id = uuid4().hex
    command = [sys.executable, str(validator), '--output', str(OUT / f'{name}_receipt.json')]
    log = OUT / 'invocations' / f'{check}-{run_id}.log'
    latest = OUT / 'invocations' / f'{check}.json'
    record = {'schema': 'cp4-invocation-v1', 'check': check, 'run_id': run_id,
              'build': current_build(ROOT), 'command': command, 'state': 'RUNNING',
              'started_at': datetime.now(timezone.utc).isoformat(), 'exit_code': None,
              'failure': None}
    record_invocation(latest, record, starting=True)
    print('RUN', name, shlex.join(command), flush=True)
    code = None
    try:
        with log.open('w') as stream:
            stream.write(f'CP4 invocation {run_id}\n{shlex.join(command)}\n')
            stream.flush()
            code = run_command(command, stream, timeout=14400 if name == 'scale' else 1800)
        record['exit_code'] = code
        if code != 0:
            record['failure'] = f'Validator exited with code {code}'
        else:
            receipt = OUT / f'{name}_receipt.json'
            if not receipt.is_file() or not receipt.stat().st_size:
                record['failure'] = 'Validator returned success without its receipt'
            else:
                record['receipt'] = artifact(receipt)
    except BaseException as exc:
        record['failure'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        record['build_at_finish'] = current_build(ROOT)
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        if log.is_file():
            record['log'] = artifact(log)
        if record['build_at_finish'] != record['build']:
            record['failure'] = 'Candidate source changed during invocation'
        record['state'] = 'COMPLETE' if code == 0 and not record['failure'] else 'FAILED'
        record_invocation(latest, record)
    print('FINISH', name, code, record['state'], flush=True)
    return 0 if record['state'] == 'COMPLETE' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--checks", nargs="+", default=["reliability"],
                        choices=["reliability", "scale", "latency", "clean_deploy",
                                 "user_tasks", "evidence_index", "video_rehearsal"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if not args.collect_only:
        for name in args.checks:
            invoke(name)
    result = collect()
    print("CP4", result["status"], "(all seven original requirements preserved)")
    return 0 if result["status"] == "MET" else 1


if __name__ == "__main__":
    raise SystemExit(main())
