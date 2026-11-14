"""Synthetic CP3 receipt-parser controls, never evidence of product execution."""
from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
assembly = importlib.import_module("accept_cp3_b")
gate = importlib.import_module("check_checkpoint")
BUILD = {"git_commit": "a" * 40, "source_sha256": "b" * 64, "source_file_count": 1}
DECISION = "CP3-DECISION-COVERAGE"


def observation(cid=DECISION, kind="http_blackbox"):
    return {"schema_version": "1.0.0", "check": cid, "status": "PASS",
            "build": copy.deepcopy(BUILD), "build_at_finish": copy.deepcopy(BUILD),
            "evidence_kind": kind, "unresolved_issues": [],
            "checks": [{"id": "synthetic-parser-check", "status": "PASS",
                        "observed": "Temporary parser fixture; no product execution."}]}


def invalid_raw(case):
    raw = observation()
    if case == "null":
        return None
    if case == "list":
        return []
    mutations = {
        "checks-null": {"checks": None}, "checks-string": {"checks": "PASS"},
        "checks-empty": {"checks": []}, "check-null": {"checks": [None]},
        "check-list": {"checks": [[]]}, "check-missing-id": {"checks": [{"status": "PASS"}]},
        "check-id-list": {"checks": [{"id": [], "status": "PASS"}]},
        "duplicate-id": {"checks": raw["checks"] * 2},
        "wrong-kind": {"evidence_kind": "static"},
        "issues-not-list": {"unresolved_issues": "unresolved"},
    }
    raw.update(mutations[case])
    return raw


INVALID_CASES = ("null", "list", "checks-null", "checks-string", "checks-empty", "check-null",
                 "check-list", "check-missing-id", "check-id-list", "duplicate-id", "wrong-kind", "issues-not-list")


@pytest.mark.parametrize("case", INVALID_CASES)
def test_malformed_or_wrong_kind_observation_is_rejected_without_exception(case):
    assert not assembly.valid_observation(invalid_raw(case), DECISION, BUILD, ["http_blackbox"])


@pytest.mark.parametrize("cid,kind,legacy", [
    ("CP3-CRASH-RECOVERY", "process_restart", True),
    ("CP3-CPU-EQUIVALENCE", "real_cpu", True),
    ("CP3-INDEPENDENT-MODEL", "independent_history", False),
])
def test_three_a_validator_receipt_layouts_are_compatible(cid, kind, legacy):
    # Crash and CPU validators omit evidence_kind/unresolved_issues; independent
    # model declares its kind and omits issues. Their current source shapes must
    # remain consumable without interpreting omission as an explicit wrong kind.
    raw = observation(cid, kind)
    raw.pop("unresolved_issues")
    if legacy:
        raw.pop("evidence_kind")
    assert assembly.valid_observation(raw, cid, BUILD, [kind])
    raw["evidence_kind"] = "static"
    assert not assembly.valid_observation(raw, cid, BUILD, [kind])


@pytest.fixture
def synthetic_repo(tmp_path, monkeypatch):
    config = json.loads((ROOT / "configs/checkpoints.json").read_text())
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/checkpoints.json").write_text(json.dumps(config))
    out = tmp_path / "evidence/cp3"
    out.mkdir(parents=True)
    raw = {}
    for requirement in config["checkpoints"]["CP3"]["required_checks"]:
        cid = requirement["id"]
        validator = tmp_path / requirement["validator_path"]
        validator.parent.mkdir(parents=True, exist_ok=True)
        validator.write_text("# Temporary parser fixture, not a product validator.\n")
        raw[cid] = observation(cid, requirement["allowed_evidence_kinds"][0])
        if cid in assembly.A_CHECKS:
            raw[cid].pop("unresolved_issues")
            if cid != "CP3-INDEPENDENT-MODEL":
                raw[cid].pop("evidence_kind")
            (out / assembly.A_CHECKS[cid]).write_text(json.dumps(raw[cid]))
    monkeypatch.setattr(assembly, "ROOT", tmp_path)
    monkeypatch.setattr(assembly, "OUT", out)
    monkeypatch.setattr(assembly, "current_build", lambda root: copy.deepcopy(BUILD))
    monkeypatch.setattr(gate, "current_build", lambda root: copy.deepcopy(BUILD))
    exit_codes = {}

    def fake_command(args, stream, timeout):
        # Only a unit-test subprocess double; no fixture leaves pytest tmp_path.
        destination = Path(args[-1])
        cid = next(key for key, filename in assembly.B_CHECKS.items() if filename == destination.name)
        destination.write_text(json.dumps(raw[cid]))
        stream.write("Synthetic receipt assembly test; not actual validator execution.\n")
        return exit_codes.get(cid, 0)

    monkeypatch.setattr(assembly, "run_command", fake_command)
    return out, raw, exit_codes


def results(out):
    receipt = json.loads((out / "receipt.json").read_text())
    return receipt, {row["id"]: row for row in receipt["checks"]}


def test_all_valid_parser_fixtures_can_reach_joint_met(synthetic_repo):
    out, _, _ = synthetic_repo
    assert assembly.main() == 0
    receipt, rows = results(out)
    assert receipt["status"] == "MET"
    assert len(rows) == 8 and all(row["status"] == "MET" for row in rows.values())


@pytest.mark.parametrize("case", INVALID_CASES)
def test_malformed_component_does_not_abort_or_promote_joint_receipt(synthetic_repo, case):
    out, raw, _ = synthetic_repo
    raw[DECISION] = invalid_raw(case)
    assert assembly.main() == 1
    receipt, rows = results(out)
    assert len(rows) == 8
    assert rows[DECISION]["status"] == "NOT_RUN"
    assert receipt["status"] == "NOT_MET"


@pytest.mark.parametrize("case", ["issues", "not-met", "failed-check", "bad-exit", "stale-build"])
def test_unmet_or_stale_component_cannot_be_promoted(synthetic_repo, case):
    out, raw, codes = synthetic_repo
    component = raw[DECISION]
    if case == "issues":
        component["unresolved_issues"] = ["Full candidate domain remains untested."]
    elif case == "not-met":
        component["status"] = "NOT_MET"
    elif case == "failed-check":
        component["checks"][0]["status"] = "FAIL"
    elif case == "bad-exit":
        codes[DECISION] = 1
    else:
        component["build_at_finish"]["source_sha256"] = "c" * 64
    assert assembly.main() == 1
    receipt, rows = results(out)
    assert rows[DECISION]["status"] != "MET"
    assert receipt["status"] == "NOT_MET"
    if case == "issues":
        assert rows[DECISION]["unresolved_issues"] == component["unresolved_issues"]


def test_missing_a_is_not_inferred_from_complete_b(synthetic_repo):
    out, _, _ = synthetic_repo
    for filename in assembly.A_CHECKS.values():
        (out / filename).unlink()
    # Command exit is explicitly B-only; joint evidence must remain unmet.
    assert assembly.main() == 0
    receipt, rows = results(out)
    assert receipt["status"] == "NOT_MET"
    assert all(rows[cid]["status"] == "NOT_RUN" for cid in assembly.A_CHECKS)
    delivery = json.loads((out / "b_delivery_receipt.json").read_text())
    assert delivery["status"] == "PASS" and delivery["joint_cp3"] == "NOT_MET"


def test_independent_gate_rejection_is_written_back_to_joint_status(synthetic_repo, monkeypatch):
    out, _, _ = synthetic_repo
    monkeypatch.setattr(assembly, "evaluate_checkpoint", lambda root, checkpoint: {
        "status": "NOT_MET", "errors": ["Synthetic independent gate rejection"]})
    assembly.main()
    receipt, _ = results(out)
    assert receipt["status"] == "NOT_MET"
    assert json.loads((out / "b_delivery_receipt.json").read_text())["joint_cp3"] == "NOT_MET"
