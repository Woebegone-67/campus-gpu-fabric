"""Incomplete, failed or stale execution evidence must not grant A's CP3 gate."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("accept_cp3_a", ROOT / "scripts/accept_cp3_a.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize("changed", ["run_failed", "receipt_failed", "stale", "no_checks", "failed_check",
                                    "wrong_gate", "wrong_schema", "stale_finish"])
def test_incomplete_evidence_cannot_pass(changed):
    module = load_runner()
    build = {"git_commit": "example", "source_sha256": "a", "source_file_count": 1}
    current = dict(build)
    run = {"status": "PASS"}
    cid = "CP3-CPU-EQUIVALENCE"
    receipt = {"status": "PASS", "build": build, "build_at_finish": dict(build),
               "check": cid, "schema_version": "1.0.0", "checks": [{"id": "actual", "status": "PASS"}]}
    assert module.valid_receipt(run, receipt, build, current, cid)
    if changed == "run_failed":
        run["status"] = "FAIL"
    elif changed == "receipt_failed":
        receipt["status"] = "FAIL"
    elif changed == "stale":
        current["source_sha256"] = "different"
    elif changed == "no_checks":
        receipt["checks"] = []
    elif changed == "wrong_gate":
        receipt["check"] = "another_gate"
    elif changed == "wrong_schema":
        receipt["schema_version"] = "unknown"
    elif changed == "stale_finish":
        receipt["build_at_finish"]["source_sha256"] = "different"
    else:
        receipt["checks"][0]["status"] = "FAIL"
    assert not module.valid_receipt(run, receipt, build, current, cid)


@pytest.mark.parametrize("changed", ["omitted_core", "omitted_pending", "duplicate", "extra"])
def test_gate_registry_cannot_silently_reduce_scope(changed):
    module = load_runner()
    requirements = [{"id": cid} for cid in sorted(set(module.RECEIPTS) | module.PENDING)]
    assert module.valid_requirements(requirements)
    if changed == "omitted_core":
        requirements = [item for item in requirements if item["id"] != "CP3-CRASH-RECOVERY"]
    elif changed == "omitted_pending":
        requirements = [item for item in requirements if item["id"] != "CP3-PREFIX-REPLAY"]
    elif changed == "duplicate":
        requirements.append(requirements[0])
    else:
        requirements.append({"id": "OTHER"})
    assert not module.valid_requirements(requirements)


def test_outer_timeout_allows_validator_cleanup_but_cannot_pass(tmp_path):
    module = load_runner()
    marker = tmp_path / "cleanup.txt"
    code = "import pathlib,sys,time\ntry:\n time.sleep(60)\nfinally:\n pathlib.Path(sys.argv[1]).write_text('cleaned')\n"
    with (tmp_path / "log.txt").open("w") as stream:
        assert module.run_command([sys.executable, "-c", code, str(marker)], stream, 0.5) == 124
    assert marker.read_text() == "cleaned"
