#!/usr/bin/env python3
"""Fresh CP4 reliability checks; no policy effect experiments or human substitution."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

from _reliability_review import (
    check_set,
    validate_binding,
    validate_cpu,
    validate_decisions,
    validate_official,
    validate_overview,
    validate_prefix,
)

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, allocation_body, run_checks  # noqa: E402
from accept_cp3_a import run_command  # noqa: E402
from check_checkpoint import current_build  # noqa: E402

SEED = 2026111404
CHILDREN = (
    ("official-http", "cp2/cp2_all_operations.py", ()),
    ("preemption", "cp2/cp2_preemption.py", ()),
    ("node-failure", "cp2/cp2_node_failure.py", ()),
    ("clock-boundaries", "cp2/cp2_clock_boundaries.py", ()),
    ("reset-isolation", "cp2/cp2_reset_isolation.py", ()),
    ("crash-recovery", "cp3/cp3_crash_recovery.py", ()),
    ("cpu-equivalence", "cp3/cp3_cpu_equivalence.py", ()),
    ("independent-model", "cp3/cp3_independent_model.py",
     ("--seeds", "8", "--steps", "80", "--concurrent", "8")),
    ("prefix-replay", "cp3/cp3_prefix_replay.py", ()),
    ("decision-coverage", "cp3/cp3_decision_coverage.py", ()),
    ("overview", "cp3/cp3_overview_snapshot.py", ()),
)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def ref(path):
    return {"path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def attacks(h):
    """New legal boundary combinations and malformed/foreign-owner requests."""
    h.reset()
    rng = random.Random(SEED)
    for index in range(12):
        body = allocation_body(ttl=rng.choice((30, 31, 3599, 3600)),
                               cpu=rng.randint(1, 4000))
        body["request"]["memory_mib"] = rng.randint(1, 8192)
        body["request"]["ephemeral_storage_mib"] = rng.randint(0, 1024)
        body["request"]["gpu"]["memory_mib"] = rng.randint(0, 8192)
        aid = h.request("POST", "/v1/allocations", body=body,
                        key=f"cp4-boundary-{index}", expected=201)[1]["id"]
        jid = h.request("POST", "/v1/jobs", body={"allocation_id": aid,
            "image": f"unknown-image-{rng.getrandbits(32)}", "command": ["unexecuted-model"],
            "timeout_seconds": rng.choice((1, 9, 10, 11, 100)), "max_retries": 0},
            expected=201)[1]["id"]
        before = h.get(f"/v1/allocations/{aid}")
        for method, path, foreign_body, expected in (
            ("GET", f"/v1/jobs/{jid}", None, 404),
            ("DELETE", f"/v1/jobs/{jid}", None, 404),
            # Frozen public API specifies 409 for foreign-owner renewal.
            ("POST", f"/v1/allocations/{aid}/renew", {"ttl_seconds": 30}, 409),
            ("DELETE", f"/v1/allocations/{aid}", None, 404),
        ):
            h.request(method, path, body=foreign_body, actor="b", expected=expected)
        assert h.get(f"/v1/allocations/{aid}") == before
        h.request("DELETE", f"/v1/jobs/{jid}", expected=202)
        h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
        h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
        totals = h.get("/v1/capacity")["totals"]
        assert totals["available"] == totals["capacity"]
    h.check("new-legal-boundary-combinations", "12 seeded TTL/resource/timeout combinations; foreign mutations denied; duplicate release returns no extra capacity")
    baseline = h.get("/v1/allocations")["allocations"]
    for field, value in (("ttl_seconds", 29), ("ttl_seconds", 3601),
                         ("ttl_seconds", "bad"), ("preemptible", "bad"),
                         ("preemption_policy", []), ("node_selector", []),
                         ("unregistered_field", True)):
        body = allocation_body()
        body[field] = value
        h.request("POST", "/v1/allocations", body=body, expected=400)
    for name in ("cpu_millicores", "memory_mib", "ephemeral_storage_mib"):
        body = copy.deepcopy(allocation_body())
        body["request"][name] = -1
        h.request("POST", "/v1/allocations", body=body, expected=400)
    body = allocation_body()
    body["owner"] = "bob"
    h.request("POST", "/v1/allocations", body=body, expected=403)
    assert h.get("/v1/allocations")["allocations"] == baseline
    assert h.get("/v1/allocations", "b")["allocations"] == []
    h.check("unknown-input-rejection", "11 malformed or foreign-owner inputs reject with 400/403; allocation records unchanged; no 500 accepted")


def review(root=ROOT):
    """Read-only audit of the immutable run record and every nested artifact."""
    root = Path(root)
    path = root / "evidence/cp4/reliability_run.json"
    result = {"check": "CP4-RELIABILITY", "status": "NOT_RUN", "build": current_build(root),
              "evidence": [], "unresolved_issues": [], "actual": "No CP4 reliability run yet"}
    if not path.exists():
        return result
    try:
        original = path.read_bytes()
        data = json.loads(original)
        assert data["build"] == data["build_at_finish"] == result["build"], "Stale run"
        assert data["status"] == "PASS" and not data.get("failure"), "Run failed or incomplete"
        assert {x["id"] for x in data["children"]} == {x[0] for x in CHILDREN}
        assert len(data["children"]) == len(CHILDREN)
        seen, watched, receipt_paths = set(), {path: hashlib.sha256(original).hexdigest()}, set()

        def inspect(entry):
            p = (root / entry["path"]).resolve()
            assert p.is_relative_to((root / "evidence/cp4").resolve()), "Evidence escapes CP4"
            assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == entry["sha256"]
            watched[p] = entry["sha256"]
            if p in seen or p.suffix != ".json":
                return
            seen.add(p)
            walk(json.loads(p.read_text()))

        def walk(value):
            if isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key == "evidence":
                        for entry in item if isinstance(item, list) else [item]:
                            inspect(entry)
                    else:
                        walk(item)
        for row in data["children"]:
            assert row["receipt"]["path"] not in receipt_paths, "A receipt was reused for another inherited check"
            receipt_paths.add(row["receipt"]["path"])
            inspect(row["receipt"])
            child = json.loads((root / row["receipt"]["path"]).read_text())
            _, relative, options = next(entry for entry in CHILDREN if entry[0] == row["id"])
            validate_binding(row, child, result["build"], relative, options, root=root)
            if row["id"] == "official-http":
                validate_official(root, child)
            elif row["id"] == "cpu-equivalence":
                validate_cpu(child)
            elif row["id"] == "prefix-replay":
                validate_prefix(root, child)
            elif row["id"] == "decision-coverage":
                validate_decisions(root, child)
            elif row["id"] == "overview":
                validate_overview(child)
        walk(data)
        assert len(data["evidence"]) == 1 and data["evidence"][0]["path"] == "evidence/cp4/reliability_http_history.json"
        raw = json.loads((root / data["evidence"][0]["path"]).read_text())
        assert raw["build"] == result["build"] and raw["seed"] == SEED
        check_set(raw["checks"], {"execution-disclosure", "capacity-read-only", "32-network-last-resource",
            "32-network-idempotency", "owner-isolation", "leases-and-once-only-return",
            "sigkill-after-unread-response", "normal-model-and-keep-lease", "partial-success-and-timeout",
            "explicit-settlement", "new-legal-boundary-combinations", "unknown-input-rejection"})
        assert sum(x.get("kind") == "http" for x in raw["history"]) > 200
        assert current_build(root) == result["build"], "Source changed during review"
        assert all(hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in watched.items()), "Evidence changed during review"
        result.update(status="PASS", actual=f"Fresh TCP lifecycle/32-way/unknown-input checks and {len(CHILDREN)} inherited validators passed; raw histories verified",
                      observed_at=data["observed_at"], evidence=[{"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}])
    except (AssertionError, OSError, ValueError, KeyError, TypeError) as exc:
        result.update(status="NOT_MET", actual="Reliability evidence failed review",
                      unresolved_issues=[str(exc)])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp4/reliability_receipt.json")
    parser.add_argument("--attacks-only", action="store_true", help="Development diagnostic, cannot pass the complete gate")
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    out = ROOT / "evidence/cp4"
    out.mkdir(parents=True, exist_ok=True)
    if not args.review_only:
        data = {"build": current_build(ROOT), "observed_at": datetime.now(timezone.utc).isoformat(),
                "status": "RUNNING", "children": [], "evidence": [], "failure": None}
        if not args.attacks_only:
            write(out / "reliability_run.json", data)
        with tempfile.TemporaryDirectory(prefix="fabric-cp4-reliability-") as temporary:
            h = Harness(temporary)
            try:
                run_checks(h)
                attacks(h)
            except Exception:
                data["failure"] = h.redact(traceback.format_exc())
            finally:
                h.stop()
                history = out / ("reliability_attacks_development_history.json" if args.attacks_only else "reliability_http_history.json")
                write(history, {"build": data["build"], "seed": SEED, "history": h.history, "checks": h.checks})
                data["evidence"].append(ref(history))
        if not args.attacks_only and not data["failure"]:
            for name, relative, options in CHILDREN:
                receipt = out / f"reliability-{name}.json"
                log = out / f"reliability-{name}.txt"
                command = [sys.executable, str(ROOT / "tests/acceptance" / relative), "--output", str(receipt), *options]
                print("RUN", name, flush=True)
                with log.open("w") as stream:
                    code = run_command(command, stream, timeout=600)
                row = {"id": name, "exit_code": code, "command": command, "evidence": [ref(log)]}
                if receipt.exists():
                    row["receipt"] = ref(receipt)
                    row["evidence"].append(ref(receipt))
                data["children"].append(row)
                write(out / "reliability_run.json", data)
                print("DONE", name, code, flush=True)
        data["build_at_finish"] = current_build(ROOT)
        data["status"] = "PASS" if not args.attacks_only and not data["failure"] and len(data["children"]) == len(CHILDREN) and all(r["exit_code"] == 0 for r in data["children"]) and data["build"] == data["build_at_finish"] else "NOT_MET"
        write(out / ("reliability_attacks_development.json" if args.attacks_only else "reliability_run.json"), data)
    result = review()
    if args.attacks_only and not args.review_only:
        result.update(status="NOT_MET", evidence=data["evidence"],
                      actual=f"Development TCP checks only: {len(h.checks)} assertions; full inherited suite not run",
                      development_failure=data["failure"])
    result["build_at_finish"] = current_build(ROOT)
    write(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
