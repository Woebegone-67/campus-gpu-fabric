"""Executable CP1 checks; UI observations are evidence review, not simulated humans."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
current_build = import_module("check_checkpoint").current_build


def require(condition, message):
    if not condition:
        raise ValueError(message)


def artifact(entry):
    path = (ROOT / entry["path"]).resolve()
    require(path.is_relative_to((ROOT / "evidence/cp1").resolve()), "Evidence must be inside evidence/cp1")
    require(path.is_file() and path.stat().st_size > 0, f"Missing evidence: {path}")
    require(hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], f"Changed evidence: {path}")


def observed_record(record):
    require(record.get("build") == current_build(ROOT), "Observation belongs to a different build")
    when = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
    require(when.tzinfo is not None and (when - datetime.now(timezone.utc)).total_seconds() <= 300,
            "Observation time needs a timezone and cannot be in the future")


def observation(check):
    require(check.get("status") == "PASS", "Required observation is not PASS")
    require(len(check.get("actual", "").strip()) > 20, "Concrete actual observation required")
    require(bool(check.get("evidence")), "Observation requires actual artifacts")
    for entry in check["evidence"]:
        artifact(entry)


def ui(check_id):
    path = ROOT / "evidence/cp1/joint/ui_receipt.json"
    require(path.is_file(), f"NOT_RUN: missing {path.relative_to(ROOT)}")
    record = json.loads(path.read_text())
    observed_record(record)
    require(bool(record.get("observer_kind")) and bool(record.get("observer_ids")), "Identify actual observer")
    matches = [c for c in record.get("checks", []) if c.get("id") == check_id]
    require(len(matches) == 1, f"One observed UI record required for {check_id}")
    observation(matches[0])
    return record


def humans():
    path = ROOT / "evidence/cp1/joint/human_handoff.json"
    require(path.is_file(), "NOT_RUN: A/B human handoff has not been recorded")
    record = json.loads(path.read_text())
    observed_record(record)
    people = record.get("operators", [])
    require(len(people) == 2 and {p.get("role") for p in people} == {"A", "B"}, "Distinct A and B records required")
    identities = [p.get("human_id", "").strip() for p in people]
    require(all(identities) and len(set(identities)) == 2, "Two distinct human identities required")
    for person in people:
        require(person.get("observer_kind") == "human", "Automated role switching is not a human handoff")
        require(set(person.get("workspaces", [])) == {"run", "resources", "continuity", "decisions", "events", "acceptance"},
                "Each operator must record all six workspaces")
        observation(person)
    # Identity claims and artifact hashes are reviewable attestations, not an
    # automated proof that a named human personally operated the application.


def suite(filename, receipt_name, ids):
    subprocess.run([sys.executable, str(ROOT / "scripts" / filename)], cwd=ROOT, check=True)
    receipt = json.loads((ROOT / "evidence/cp1" / receipt_name).read_text())
    require(receipt.get("status") == "PASS", "Runtime suite failed")
    require(receipt.get("build") == current_build(ROOT) == receipt.get("build_after"), "Runtime build mismatch")
    checks = {c["id"]: c for c in receipt["checks"]}
    require(all(checks.get(i, {}).get("status") == "PASS" for i in ids), "Required runtime assertion missing")
    artifact(receipt["evidence"])


def capacity():
    from accept_cp1_a import Harness, allocation_body, inventory_node
    with tempfile.TemporaryDirectory(prefix="cp1-capacity-") as directory:
        h = Harness(directory, app_target="campus_fabric.workflows.devhost:create_app")
        node = inventory_node()
        whole = node["devices"][0]
        node["devices"].append({**whole, "device_id": "slice-4gb", "profile": "slice-4gb", "memory_mib": 4096})
        Path(h.env["FABRIC_INVENTORY_FILE"]).write_text(json.dumps({"nodes": [node]}))
        before = current_build(ROOT)
        try:
            h.start()
            for field, value in (("cpu_millicores", 4001), ("memory_mib", 8193)):
                body = allocation_body()
                body["request"][field] = value
                assert h.request("POST", "/v1/allocations", body=body, expected=202)[1]["state"] == "Pending"
            for patch in ({"memory_mib": 8193}, {"profile": "wrong-profile"}, {"count": 2}):
                body = allocation_body()
                body["request"]["gpu"].update(patch)
                assert h.request("POST", "/v1/allocations", body=body, expected=202)[1]["state"] == "Pending"
            body = allocation_body(cpu=2000)
            body["request"]["memory_mib"] = 4096
            body["request"]["gpu"]["profile"] = "slice-4gb"
            allocation = h.request("POST", "/v1/allocations", body=body, expected=201)[1]
            totals = h.get("/v1/capacity")["totals"]["available"]
            assert totals["cpu_millicores"] == 2000 and totals["memory_mib"] == 4096
            assert "slice-4gb" in json.dumps(allocation)
            whole_request = allocation_body()
            assert h.request("POST", "/v1/allocations", body=whole_request, expected=202)[1]["state"] == "Pending"
            h.check("assembled-capacity-and-profile", "CPU/memory/VRAM/profile/count mismatches wait; valid slice allocates; physical parent cannot double-allocate")
        finally:
            h.stop()
            output = ROOT / "evidence/cp1/capacity_validator_history.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"build": before, "history": h.history}, indent=2) + "\n")
        require(before == current_build(ROOT), "Source changed during capacity check")


def main(check_id):
    try:
        if check_id == "CP1-CAPACITY":
            capacity()
        elif check_id == "CP1-CONCURRENCY":
            suite("accept_cp1_a.py", "a_core_receipt.json", ["32-network-idempotency", "32-network-last-resource"])
            suite("accept_cp1_joint.py", "joint_backend_receipt.json", ["32-network-last-resource"])
        elif check_id == "CP1-OWNER-ISOLATION":
            suite("accept_cp1_a.py", "a_core_receipt.json", ["owner-isolation"])
            suite("accept_cp1_joint.py", "joint_backend_receipt.json", ["joint-owner-isolation"])
            subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/blackbox/test_queries_kernel.py"], cwd=ROOT, check=True)
        elif check_id == "CP1-LEASES":
            suite("accept_cp1_a.py", "a_core_receipt.json", ["leases-and-once-only-return"])
            suite("accept_cp1_joint.py", "joint_backend_receipt.json", ["failed-job-keeps-lease-explicit-release", "failure-terminal-settlement"])
        elif check_id == "CP1-PERSISTENCE":
            suite("accept_cp1_joint.py", "joint_backend_receipt.json", ["lost-register-sigkill-recovery-and-stage-replay"])
            ui(check_id)
        elif check_id == "CP1-PARTIAL-SUCCESS":
            suite("accept_cp1_joint.py", "joint_backend_receipt.json", ["partial-success-stale-stage-safe-retry"])
            ui(check_id)
        elif check_id == "CP1-NORMAL-JOURNEY":
            ui(check_id)
            humans()
        else:
            raise ValueError("Unknown check")
    except (ValueError, KeyError, OSError, AssertionError, subprocess.CalledProcessError) as error:
        print(json.dumps({"check": check_id, "status": "NOT_MET", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"check": check_id, "status": "PASS", "scope": "Individual check only; no top-level CP1 certification"}))
    return 0
