"""Synthetic negative controls for the evidence reviewer, not runtime acceptance."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/acceptance/cp4"))
from _reliability_review import (  # noqa: E402
    CHECK_IDS,
    CP2_SCOPE,
    CPU_CASES,
    SCOPES,
    SELECTIONS,
    validate_binding,
    validate_cpu,
    validate_decision_record,
    validate_decisions,
    validate_official,
)

BUILD = {"git_commit": "synthetic-control", "source_sha256": "not-runtime-evidence", "source_file_count": 1}


def fixture(name="preemption"):
    relative = "cp2/cp2_preemption.py"
    options = ()
    child = {"check_id": "CP2-PREEMPTION", "scope": CP2_SCOPE,
             "status": "PASS", "build": BUILD, "build_at_finish": BUILD,
             "checks": [{"id": key, "status": "PASS"} for key in sorted(CHECK_IDS["preemption"])]}
    if name == "independent-model":
        relative = "cp3/cp3_independent_model.py"
        options = ("--seeds", "8", "--steps", "80", "--concurrent", "8")
        child.update(check="CP3-INDEPENDENT-MODEL", scope=copy.deepcopy(SCOPES[name]),
                     evidence_kind="independent_history",
                     configuration={"seeds": list(range(8)), "steps_per_seed": 80,
                                    "concurrent_seeds": list(range(8)), "concurrent_operations_per_history": 6},
                     checks=[{"id": key, "status": "PASS"} for key in sorted(CHECK_IDS[name])])
        for row in child["checks"]:
            if row["id"] == "generated-sequential-histories":
                row.update(commands=640, evidence=[{}] * 8)
            if row["id"] == "real-concurrent-linearizable-histories":
                row.update(commands=48, evidence=[{}] * 8, overlapping_pairs=1)
    elif name in SELECTIONS:
        identifier = "CP2-" + name.upper()
        relative = "cp2/cp2_" + name.replace("-", "_") + ".py"
        selection = ["tests/domain/test_cp2_adversarial.py::" + name for name in SELECTIONS[name]]
        child.update(check_id=identifier, checks=[], selected_tests=selection,
                     command=[sys.executable, "-m", "pytest", "-q", *selection],
                     returncode=0, stdout=f"{len(selection)} passed in 0.5s")
    elif name == "decision-coverage":
        relative = "cp3/cp3_decision_coverage.py"
        child.update(check="CP3-DECISION-COVERAGE", scope=copy.deepcopy(SCOPES[name]),
                     evidence_kind="http_blackbox",
                     checks=[{"id": key, "status": "PASS"} for key in sorted(CHECK_IDS[name])])
    path = f"evidence/cp4/reliability-{name}.json"
    row = {"id": name, "receipt": {"path": path}, "exit_code": 0,
           "command": [sys.executable, str(ROOT / "tests/acceptance" / relative),
                       "--output", str(ROOT / path), *options]}
    return row, child, relative, options


@pytest.mark.parametrize("name", ["preemption", "independent-model", "clock-boundaries", "reset-isolation", "decision-coverage"])
def test_registered_child_binding_accepts_complete_synthetic_shape(name):
    row, child, relative, options = fixture(name)
    validate_binding(row, child, BUILD, relative, options)


@pytest.mark.parametrize("damage", ["substitute_receipt", "substitute_identity", "omit_check",
                                    "duplicate_check", "wrong_scope", "unresolved", "stale", "omit_command", "foreign_source"])
def test_binding_rejects_substitution_and_missing_checks(damage):
    row, child, relative, options = fixture()
    if damage == "substitute_receipt":
        row["receipt"]["path"] = "evidence/cp4/reliability-cpu-equivalence.json"
    elif damage == "substitute_identity":
        child["check_id"] = "CP2-NODE-FAILURE"
    elif damage == "omit_check":
        child["checks"].pop()
    elif damage == "duplicate_check":
        child["checks"][-1] = child["checks"][0]
    elif damage == "wrong_scope":
        child["scope"] = "unrelated summary"
    elif damage == "unresolved":
        child["unresolved_issues"] = ["Known intermittent failure"]
    elif damage == "stale":
        child["build_at_finish"] = {**BUILD, "git_commit": "other"}
    elif damage == "foreign_source":
        row["command"][1] = "/other/tests/acceptance/" + relative
    else:
        row.pop("command")
    with pytest.raises(AssertionError):
        validate_binding(row, child, BUILD, relative, options)


@pytest.mark.parametrize("damage", ["omit_options", "reduced_config", "missing_history", "zero_overlap"])
def test_independent_history_workload_cannot_be_silently_reduced(damage):
    row, child, relative, options = fixture("independent-model")
    if damage == "omit_options":
        row["command"] = row["command"][:4]
    elif damage == "reduced_config":
        child["configuration"]["seeds"] = [0]
    elif damage == "missing_history":
        next(c for c in child["checks"] if c["id"] == "generated-sequential-histories")["evidence"].pop()
    else:
        next(c for c in child["checks"] if c["id"] == "real-concurrent-linearizable-histories")["overlapping_pairs"] = 0
    with pytest.raises(AssertionError):
        validate_binding(row, child, BUILD, relative, options)


@pytest.mark.parametrize("name", ["clock-boundaries", "reset-isolation"])
def test_same_length_different_pytest_selection_is_rejected(name):
    row, child, relative, options = fixture(name)
    child["selected_tests"][-1] = "tests/domain/test_cp2_adversarial.py::unrelated_test"
    child["command"][-1] = child["selected_tests"][-1]
    with pytest.raises(AssertionError, match="selection"):
        validate_binding(row, child, BUILD, relative, options)


def test_official_coverage_recomputes_actual_http_instead_of_trusting_21_passes():
    import re
    operations = json.loads((ROOT / "contracts/api_ownership.json").read_text())["operations"]
    child = {"operations": [], "history": []}
    for index, operation in enumerate(operations):
        request_id = str(index)
        child["operations"].append({"operation_id": operation["operation_id"], "status": "PASS",
                                    "successful_request_ids": [request_id]})
        child["history"].append({"kind": "http", "request_id": request_id,
                                 "method": operation["method"], "status": 200,
                                 "path": re.sub(r"\{[^}]+\}", "synthetic-object", operation["path"])})
    validate_official(ROOT, child)
    child["history"].pop(0)
    with pytest.raises(AssertionError):
        validate_official(ROOT, child)


def cpu_fixture():
    cases = []
    for name, config in CPU_CASES.items():
        values, value = {}, config["seed"]
        for step in range(config["total_steps"]):
            value = (1664525 * value + 1013904223 + step) % 2**64
            if step + 1 in {10, 20, config["total_steps"]}:
                values[step + 1] = value
        def payload(step):
            return {"algorithm": "lcg64-indexed-v1", "state": {"step": step, "value": values[step]}}
        saves = [{"public_artifact": payload(step), "ready_sequence": i * 2,
                  "stop_sequence": i * 2 + 1, "physical_exit_observed": True,
                  "source_process": {"stop_confirmed": True, "exit_code": 0},
                  "checkpoint": {"verification_status": "verified"}} for i, step in enumerate((10, 20), 1)]
        result = payload(config["total_steps"])
        uninterrupted = copy.deepcopy(result)
        checks = [
            {"id": name + ":two-real-resumes", "status": "PASS", "observed": {
                "saves": saves, "result": result, "lease": {"state": "Released", "restart_count": 2},
                "final_attempt": {"checkpoint": {"continuation_status": "applied"}}}},
            {"id": name + ":three-way-equality", "status": "PASS", "observed": {
                "oracle": result["state"], "resumed": result, "uninterrupted": uninterrupted,
                "distinct_actual_processes": [1, 2, 3, 4]}},
            {"id": name + ":settled-persistence-isolation", "status": "PASS", "observed": {"bob_status": 404}},
        ]
        history = [{"kind": "http", "status": 200, "path": "/x/v1/artifacts?uri=synthetic",
                    "response_body": artifact} for artifact in [*(s["public_artifact"] for s in saves), result, uninterrupted]]
        cases.append({"name": name, "configuration": {**config, "batch_pause_seconds": .03},
                      "checks": checks, "history": history})
    return {"cases": cases}


def test_cpu_arithmetic_is_recomputed_even_if_summary_and_http_agree_on_wrong_value():
    child = cpu_fixture()
    validate_cpu(child)
    # Aliased synthetic observation and HTTP both contain the same bad result.
    child["cases"][0]["checks"][0]["observed"]["saves"][0]["public_artifact"]["state"]["value"] += 1
    with pytest.raises(AssertionError, match="arithmetic"):
        validate_cpu(child)


def test_development_run_cannot_reuse_pass_or_overwrite_formal_history(tmp_path, monkeypatch):
    import cp4_reliability as reviewer

    output = tmp_path / "evidence/cp4"
    output.mkdir(parents=True)
    formal = output / "reliability_http_history.json"
    formal.write_text("retained formal observations")
    run = output / "reliability_run.json"
    run.write_text("retained formal run")

    class SyntheticHarness:
        def __init__(self, directory):
            self.history, self.checks = [], []

        def stop(self):
            pass

    monkeypatch.setattr(reviewer, "ROOT", tmp_path)
    monkeypatch.setattr(reviewer, "Harness", SyntheticHarness)
    monkeypatch.setattr(reviewer, "current_build", lambda root: BUILD)
    monkeypatch.setattr(reviewer, "run_checks", lambda harness: None)
    monkeypatch.setattr(reviewer, "attacks", lambda harness: None)
    monkeypatch.setattr(reviewer, "review", lambda: {"status": "PASS", "build": BUILD})
    monkeypatch.setattr(sys, "argv", ["reviewer", "--attacks-only"])
    assert reviewer.main() == 1
    assert json.loads((output / "reliability_receipt.json").read_text())["status"] == "NOT_MET"
    assert formal.read_text() == "retained formal observations"
    assert run.read_text() == "retained formal run"


def decision_fixture(policy="B0", case="accepted"):
    selected = {"candidate_id": "selected", "feasibility": "legal", "allocation_id": "lease",
                "owner_id": "alice", "scores": [], "placement": {"slices": [{"device_ids": ["gpu"],
                    "resources": {"cpu_millicores": 1000, "memory_mib": 4096, "ephemeral_storage_mib": 0}}]}}
    candidates = [selected] if case == "budget" else [selected, {"candidate_id": "excluded", "feasibility": "infeasible", "owner_id": "alice", "scores": []}]
    plan = {"policy": {"name": policy}, "decision_id": "decision", "snapshot_version": {"epoch": 0, "revision": 1},
            "candidates": candidates, "selected_candidate_id": "selected", "asserts_pool_infeasible": False,
            "coverage": {"examined_count": len(candidates), "scope": "partial", "truncation_reason": "budget or scoped search"}}
    rejected = case in {"stale", "illegal-placement"}
    receipt = {"outcome": "rejected" if rejected else "committed", "event_ids": ["grant"],
               "error": {"code": "stale_version" if case == "stale" else "insufficient_resources"}}
    attempt = {"command": {"operation": "decision.commit", "parameters": {"plan": plan}},
               "receipt": receipt, "before": {"value": 1}, "after": {"value": 1}}
    trace = {"case": case, "plans": [{"plan": copy.deepcopy(plan)}], "submissions": [attempt]}
    name = f"coverage-{policy.lower()}-{case}"
    history = []

    def response(path, body, *, actor="a", status=200, method="GET", request=None):
        history.append({"kind": "http", "path": path, "method": method, "actor_alias": actor,
                        "status": status, "response_body": body, "request_body": request})

    response("/x/v1/work-intents", {}, status=202, method="POST", request={"intent_id": name})
    response(f"/x/v1/work-intents/{name}", {"allocations": [{"id": "lease", "state": "Pending" if rejected else "Running"}], "events": []})
    response("/v1/capacity", {"totals": {"available": {"gpu_count": 2 if rejected else 1}}})
    if rejected:
        response("/x/v1/decisions/decision", {}, actor="admin", status=404)
    else:
        grant = {"event_type": "allocation.granted", "event_id": "grant", "decision_id": "decision",
                 "decision_snapshot_version": plan["snapshot_version"], "resource_deltas": [{
                     "allocation_id": "lease", "device_ids_added": ["gpu"],
                     "cpu_millicores": 1000, "memory_mib": 4096, "ephemeral_storage_mib": 0}]}
        response("/x/v1/decisions/decision", {"item": copy.deepcopy(plan), "commit_status": "committed", "actual_events": [grant]}, actor="admin")
        response("/x/v1/decisions/decision", {"item": copy.deepcopy(plan), "commit_status": "committed"})
        response("/x/v1/decisions/decision", {}, actor="b", status=404)
    return {"server_capture": trace, "http_history": history}


@pytest.mark.parametrize("name", sorted(CHECK_IDS["decision-coverage"]))
def test_decision_review_checks_all_registered_shapes(name):
    policy, case = name.split("-", 1)
    validate_decision_record(name, decision_fixture(policy, case))


@pytest.mark.parametrize("damage", ["wrong_policy", "missing_http", "explanation", "grant", "owner_leak", "bob_allowed", "rejection_mutation", "rejection_visible", "budget_complete"])
def test_decision_review_rejects_inconsistent_raw_evidence(damage):
    case = "stale" if damage.startswith("rejection") else "budget" if damage == "budget_complete" else "accepted"
    record = decision_fixture(case=case)
    attempted = record["server_capture"]["submissions"][0]
    history = record["http_history"]
    if damage == "wrong_policy":
        attempted["command"]["parameters"]["plan"]["policy"]["name"] = "P"
    elif damage == "missing_http":
        history.pop(0)
    elif damage == "explanation":
        history[3]["response_body"]["item"]["decision_id"] = "different"
    elif damage == "grant":
        history[3]["response_body"]["actual_events"][0]["resource_deltas"][0]["cpu_millicores"] += 1
    elif damage == "owner_leak":
        history[4]["response_body"]["item"]["candidates"][0]["owner_id"] = "bob"
    elif damage == "bob_allowed":
        history[5]["status"] = 200
    elif damage == "rejection_mutation":
        attempted["after"]["value"] = 2
    elif damage == "rejection_visible":
        history[3]["status"] = 200
    else:
        attempted["command"]["parameters"]["plan"]["coverage"]["scope"] = "complete"
        record["server_capture"]["plans"][0]["plan"]["coverage"]["scope"] = "complete"
    with pytest.raises(AssertionError):
        validate_decision_record("B0-" + case, record)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "replaced_path"])
def test_decision_review_requires_all_sixteen_unique_histories(tmp_path, damage):
    child = {"evidence": []}
    for name in sorted(CHECK_IDS["decision-coverage"]):
        relative = f"evidence/cp4/reliability-decision-coverage_histories/{name}.json"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(decision_fixture(*name.split("-", 1))))
        child["evidence"].append({"path": relative})
    validate_decisions(tmp_path, child)
    if damage == "missing":
        child["evidence"].pop()
    elif damage == "duplicate":
        child["evidence"][-1] = child["evidence"][0]
    else:
        child["evidence"][0]["path"] = child["evidence"][0]["path"].replace("reliability-decision-coverage_histories", "other_histories")
    with pytest.raises(AssertionError):
        validate_decisions(tmp_path, child)
