"""Synthetic CP3 receipt-parser controls, never evidence of product execution."""
from __future__ import annotations

import copy
import importlib
import json
import sys
from datetime import datetime, timezone
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
    reviewer = tmp_path / "tests/acceptance/cp3/cp3_human_handoff.py"
    reviewer.write_text((ROOT / "tests/acceptance/cp3/cp3_human_handoff.py").read_text())
    human_dir = out / "b"
    human_dir.mkdir()
    operators = []
    for role in ("A", "B"):
        sample = human_dir / f"synthetic-human-{role}.txt"
        sample.write_text(f"Synthetic {role} parser fixture only; never a real human acceptance record.\n")
        operators.append({"role": role, "human_id": f"synthetic-operator-{role}",
                          "observer_kind": "human",
                          "workspaces": ["run", "resources", "continuity", "decisions", "events", "acceptance"],
                          "roles": ["researcher", "contributor", "administrator"],
                          "checks": [{"id": identity, "status": "PASS",
                                      "actual": "Synthetic parser observation only, never real product execution.",
                                      "evidence": [{"path": sample.relative_to(tmp_path).as_posix(),
                                                    "sha256": gate.sha256_file(sample)}]}
                                     for identity in ("cp3-terminal-settlement", "cp3-checkpoint-cpu-artifact",
                                                      "cp3-decision-owner-isolation", "cp3-sealed-experiment-isolation")]})
    (human_dir / "human_handoff.json").write_text(json.dumps({
        "schema_version": "1.0.0", "checkpoint": "CP3", "build": copy.deepcopy(BUILD),
        "observed_at": datetime.now(timezone.utc).isoformat(), "operators": operators}))
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


@pytest.mark.parametrize("cid", list(assembly.B_CHECKS) + ["CP3-INDEPENDENT-MODEL"])
def test_only_the_two_legacy_a_receipts_may_omit_evidence_kind(cid):
    raw = observation(cid)
    raw.pop("evidence_kind")
    assert not assembly.valid_observation(raw, cid, BUILD, ["http_blackbox"])


@pytest.mark.parametrize("case", ["missing", "cp2-only", "wrong-round", "stale", "one-operator",
                                  "same-human", "agent", "missing-journey", "failed-journey",
                                  "missing-artifact", "changed-artifact", "cp2-artifact"])
def test_cp3_joint_gate_requires_its_own_current_human_observations(synthetic_repo, case):
    out, raw, _ = synthetic_repo
    path = out / "b/human_handoff.json"
    human = json.loads(path.read_text())
    if case in {"missing", "cp2-only"}:
        if case == "cp2-only":
            old = out.parent / "cp2/b/human_handoff.json"
            old.parent.mkdir(parents=True)
            old.write_text(path.read_text())
        path.unlink()
    else:
        if case == "wrong-round":
            human["checkpoint"] = "CP2"
        elif case == "stale":
            human["build"]["source_sha256"] = "c" * 64
        elif case == "one-operator":
            human["operators"].pop()
        elif case == "same-human":
            human["operators"][1]["human_id"] = human["operators"][0]["human_id"].upper()
        elif case == "agent":
            human["operators"][1]["observer_kind"] = "agent"
        elif case == "missing-journey":
            human["operators"][1]["checks"].pop()
        elif case == "failed-journey":
            human["operators"][1]["checks"][0]["status"] = "FAIL"
        elif case == "missing-artifact":
            (out / "b/synthetic-human-B.txt").unlink()
        elif case == "changed-artifact":
            (out / "b/synthetic-human-B.txt").write_text("Changed after observation")
        elif case == "cp2-artifact":
            old = out.parent / "cp2/old-observation.txt"
            old.parent.mkdir(parents=True)
            old.write_text("Old CP2 observations are not this CP3 evidence")
            human["operators"][1]["checks"][0]["evidence"] = [{
                "path": old.relative_to(out.parents[1]).as_posix(), "sha256": gate.sha256_file(old)}]
        path.write_text(json.dumps(human))
    raw["CP3-SETTLEMENT"].update(human_acceptance="NOT_RUN", browser_review="NOT_RUN")
    assert assembly.main() == 0  # B automatic validators still passed.
    receipt, rows = results(out)
    assert all(row["status"] == "MET" for row in rows.values())
    assert receipt["technical_status"] == "MET"
    assert receipt["status"] == "NOT_MET" and receipt["human_acceptance"] != "PASS"
    delivery = json.loads((out / "b_delivery_receipt.json").read_text())
    assert delivery["status"] == "PASS" and delivery["joint_cp3"] == "NOT_MET"


def test_human_gate_rechecks_artifacts_after_initial_pass(synthetic_repo):
    out, _, _ = synthetic_repo
    assembly.main()
    assert results(out)[0]["status"] == "MET"
    (out / "b/synthetic-human-A.txt").write_text("Changed after the joint receipt passed")
    report = gate.evaluate_checkpoint(out.parents[1], "CP3")
    assert report["status"] == "NOT_MET" and report["human_acceptance"] == "NOT_MET"


@pytest.mark.parametrize("location", ["raw", "check", "transitive"])
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_nested_evidence_does_not_pass_when_underlying_artifact_changes(synthetic_repo, location, damage):
    out, raw, _ = synthetic_repo
    transcript = out / "synthetic-transcript.json"
    transcript.write_text(json.dumps({"fixture": "No real product execution"}))
    ref = {"path": transcript.relative_to(out.parents[1]).as_posix(), "sha256": gate.sha256_file(transcript)}
    if location == "raw":
        raw[DECISION]["evidence"] = [ref]
    elif location == "check":
        raw[DECISION]["checks"][0]["evidence"] = ref
    else:
        middle = out / "synthetic-middle.json"
        middle.write_text(json.dumps({"evidence": [ref]}))
        raw[DECISION]["evidence"] = [{"path": middle.relative_to(out.parents[1]).as_posix(),
                                      "sha256": gate.sha256_file(middle)}]
    assembly.main()
    assert results(out)[0]["status"] == "MET"
    if damage == "missing":
        transcript.unlink()
    else:
        transcript.write_text(json.dumps({"fixture": "Modified after a passing receipt"}))
    report = gate.evaluate_checkpoint(out.parents[1], "CP3")
    assert report["status"] == "NOT_MET"
    assert any("nested" in message for message in report["errors"])


def test_review_existing_does_not_execute_validators_and_can_finish_human_gate(synthetic_repo, monkeypatch):
    out, _, _ = synthetic_repo
    path = out / "b/human_handoff.json"
    saved = path.read_bytes()
    path.unlink()
    assembly.main()
    assert results(out)[0]["status"] == "NOT_MET"
    path.write_bytes(saved)
    monkeypatch.setattr(assembly, "run_command", lambda *args, **kwargs: pytest.fail("Review must not rerun validators"))
    assert assembly.main(review_existing=True) == 0
    assert results(out)[0]["status"] == "MET"
    delivery = json.loads((out / "b_delivery_receipt.json").read_text())
    assert delivery["runs"] == [] and delivery["operation"] == "review_existing"


def test_b_technical_delivery_rejects_missing_nested_artifact(synthetic_repo):
    out, raw, _ = synthetic_repo
    raw[DECISION]["evidence"] = [{"path": "evidence/cp3/missing-history.json", "sha256": "a" * 64}]
    assert assembly.main() == 1
    receipt, rows = results(out)
    assert receipt["status"] == "NOT_MET" and rows[DECISION]["status"] == "NOT_MET"
    assert json.loads((out / "b_delivery_receipt.json").read_text())["status"] == "NOT_MET"


@pytest.mark.parametrize("case", ["failed-exit", "raw-changed", "missing-provenance"])
def test_review_existing_cannot_promote_failed_or_unbound_execution(synthetic_repo, case):
    out, raw, codes = synthetic_repo
    if case == "failed-exit":
        codes[DECISION] = 1
    assembly.main()
    if case == "raw-changed":
        path = out / assembly.B_CHECKS[DECISION]
        value = json.loads(path.read_text())
        value["checks"][0]["observed"] = "Changed after the subprocess receipt was assembled"
        path.write_text(json.dumps(value))
    elif case == "missing-provenance":
        (out / "b_delivery_receipt.json").unlink()
    assert assembly.main(review_existing=True) == 1
    assert results(out)[0]["status"] == "NOT_MET"


def test_repeated_human_review_retains_original_execution_provenance(synthetic_repo):
    out, _, _ = synthetic_repo
    assert assembly.main() == 0
    for _ in range(2):
        assert assembly.main(review_existing=True) == 0
        assert results(out)[0]["status"] == "MET"
    delivery = json.loads((out / "b_delivery_receipt.json").read_text())
    assert len(delivery["retained_runs"]) == len(assembly.B_CHECKS)
    assert delivery["runs"] == []
