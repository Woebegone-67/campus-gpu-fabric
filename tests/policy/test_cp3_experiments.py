"""Behavioral tests use development inputs only, never final validation outcomes."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from experiments.cp3_inputs import DEVELOPMENT_SEEDS, FAMILIES, VALIDATION_SEEDS, scenario_input
from experiments.cp3_runner import execute_scenario, summarize_run
from experiments.cp3_suite import (
    ARTIFACT_IDS,
    audit_suite,
    digest,
    encoded,
    freeze_suite,
    load_suite_index,
    read_artifact,
    run_suite,
    write_json,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def stable_root(tmp_path):
    root = tmp_path / "source"
    for label in ("configs/cp1_inventory.json", "scripts/check_checkpoint.py"):
        target = root / label
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / label, target)
    return root


def small_input(family="compatibility-fragmentation"):
    data = scenario_input(ROOT, family, DEVELOPMENT_SEEDS[0], development=True)
    data["works"] = data["works"][:4]
    data["horizon"] = 40
    return data


def test_frozen_partitions_and_adversarial_input_shapes(stable_root, tmp_path):
    assert not set(DEVELOPMENT_SEEDS) & set(VALIDATION_SEEDS)
    for family in FAMILIES:
        left = scenario_input(stable_root, family, 0, development=True)
        assert left == scenario_input(stable_root, family, 0, development=True)
        assert len(left["works"]) == 32 and left["horizon"] > 300
        with pytest.raises(ValueError):
            scenario_input(stable_root, family, 0)
    suite = tmp_path / "suite"
    index = freeze_suite(stable_root, suite, "development")
    assert index["expected_runs"] == 50
    assert all(r["status"] == "not_run" for r in index["runs"])
    with pytest.raises(ValueError, match="re-seal"):
        freeze_suite(stable_root, suite, "development")
    assert load_suite_index(suite, root=stable_root) == index


def test_real_kernel_retains_failures_unfinished_and_ablation_identity(tmp_path):
    data = small_input()
    data["works"][1].update(gpu_model="H100", gpu_count=1)
    data["works"][0].update(timeout_seconds=3, max_retries=1)
    results = {}
    for policy in ("B2", "P-no-opportunity", "P"):
        result = execute_scenario(data, policy, db_path=tmp_path / f"{policy}.sqlite")
        assert not result["failures"]
        summary = summarize_run(data, result, policy, policy, digest(encoded(data)))
        assert result["snapshot"].now == 40
        assert any(w["status"] == "failed" for w in summary["works"])
        assert any(w["status"] == "unfinished" for w in summary["works"])
        assert any(r["outcome"] == "committed" for r in result["receipts"])
        assert summary["instrumentation"]["baseline_ablation_identity_violations"] == 0
        results[policy] = summary
    assert results["B2"]["works"] == results["P-no-opportunity"]["works"]
    with pytest.raises(ValueError, match="reuse"):
        execute_scenario(data, "B2", db_path=tmp_path / "B2.sqlite")


def test_future_input_does_not_change_observed_decision_prefix(tmp_path):
    left = small_input()
    right = copy.deepcopy(left)
    right["works"][-1].update(arrived_at=35, gpu_model="H100")
    # Preserve exactly the same visible prefix: the changed work originally arrives after zero.
    assert left["works"][-1]["arrived_at"] > 0
    first = execute_scenario(left, "P", db_path=tmp_path / "left.sqlite")
    second = execute_scenario(right, "P", db_path=tmp_path / "right.sqlite")
    def prefix(result):
        return [d["plan"] for d in result["decisions"] if d["snapshot"]["now"] == 0]
    assert prefix(first) == prefix(second)


def test_unsafe_artifact_paths_hashes_and_index_claims_rejected(stable_root, tmp_path):
    suite = tmp_path / "suite"
    index = freeze_suite(stable_root, suite, "development")
    with pytest.raises(ValueError):
        read_artifact(suite, "../outside.json", "0"*64)
    with pytest.raises(ValueError):
        read_artifact(suite, "manifest.json", "0"*64)
    external = tmp_path / "external.json"
    external.write_bytes(b"{}")
    (suite / "symlink.json").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        read_artifact(suite, "symlink.json", digest(b"{}"))
    index["runs"].pop()
    write_json(suite / "suite-index.json", index)
    with pytest.raises(ValueError, match="omits"):
        load_suite_index(suite)


def test_audit_recalculates_and_rejects_a_forged_summary(stable_root, tmp_path, monkeypatch):
    # The small fixture is explicitly DEVELOPMENT and is not a validation receipt.
    import experiments.cp3_suite as module
    real_generator = module.scenario_input
    def development_fixture(root, family, seed, *, development=False):
        data = real_generator(root, family, seed, development=development)
        data["works"] = data["works"][:3]
        data["horizon"] = 20
        return data
    monkeypatch.setattr(module, "scenario_input", development_fixture)
    suite = tmp_path / "suite"
    freeze_suite(stable_root, suite, "development")
    index = run_suite(stable_root, suite, limit=1)
    row = index["runs"][0]
    assert row["status"] == "complete"
    assert set(row["artifacts"]) == set(ARTIFACT_IDS)
    report = audit_suite(stable_root, suite)
    assert not report["evidence_complete"] and not report["target_met"]
    assert not any(r["run_id"] == row["run_id"] for r in report["run_failures"])
    for label in ("commands", "receipts", "decisions"):
        recorded = row["artifacts"][label]
        original = (suite / recorded["path"]).read_bytes()
        original_sha = recorded["sha256"]
        write_json(suite / recorded["path"], [])
        recorded["sha256"] = digest((suite / recorded["path"]).read_bytes())
        write_json(suite / "suite-index.json", index)
        changed = audit_suite(stable_root, suite)
        assert any(r["run_id"] == row["run_id"] for r in changed["run_failures"]), label
        (suite / recorded["path"]).write_bytes(original)
        recorded["sha256"] = original_sha
        write_json(suite / "suite-index.json", index)
    ref = row["artifacts"]["summary"]
    summary = read_artifact(suite, ref["path"], ref["sha256"])
    summary["works"][0]["effective_service"] = 999
    write_json(suite / ref["path"], summary)
    ref["sha256"] = digest((suite / ref["path"]).read_bytes())
    write_json(suite / "suite-index.json", index)
    report = audit_suite(stable_root, suite)
    assert any("recalculation" in r["reason"] for r in report["run_failures"])


def test_recall_and_fault_inputs_generate_real_reports_retries_and_stale_rejection(tmp_path):
    recall = small_input("recall-save")
    recall["works"] = scenario_input(ROOT, "recall-save", 0, development=True)["works"][:6]
    recall["horizon"] = 80
    result = execute_scenario(recall, "B2", db_path=tmp_path / "recall.sqlite")
    assert not result["failures"]
    types = {e.event_type for e in result["snapshot"].events}
    assert {"checkpoint.reported", "allocation.preempted", "allocation.evicted", "job.restarted"} <= types
    facts = [a.checkpoint for a in result["snapshot"].allocations if a.checkpoint]
    assert {f.report_status for f in facts} == {"ready", "failed"}
    node = small_input("node-interleavings")
    node["works"] = scenario_input(ROOT, "node-interleavings", 0, development=True)["works"][:6]
    node["horizon"] = 80
    result = execute_scenario(node, "B2", db_path=tmp_path / "node.sqlite")
    assert not result["failures"]
    types = {e.event_type for e in result["snapshot"].events}
    assert {"job.retry_waiting", "job.retried", "node.state_changed", "job.failed"} <= types
    late = next(e for e in result["external_injections"] if e["input"]["kind"] == "late_callback")
    assert late["receipt"]["outcome"] == "rejected"
    assert late["receipt"]["error"]["code"] == "attempt_stale"


def test_each_seed_changes_material_input_not_only_metadata():
    for family in FAMILIES:
        left = scenario_input(ROOT, family, 0, development=True)
        right = scenario_input(ROOT, family, 1, development=True)
        left.pop("seed")
        right.pop("seed")
        assert left != right, family


def test_idle_fast_forward_does_not_skip_existing_recall_end(tmp_path):
    data = small_input("recall-save")
    data["works"] = data["works"][:2]
    for work in data["works"]:
        work.update(checkpoint_behavior="ready", timeout_seconds=100, max_restarts=2)
    data["events"] = data["events"][:1]
    data["horizon"] = 60
    result = execute_scenario(data, "B2", db_path=tmp_path / "recall-idle.sqlite")
    assert not result["failures"]
    end = data["events"][0]["at"] + data["events"][0]["notice_seconds"] + data["events"][0]["duration_seconds"]
    assert any(s["logical_time"] == end for s in result["resource_samples"])
    assert all(i.state == "completed" for i in result["snapshot"].intents)


def test_spawn_workers_keep_independent_runs_and_parent_index(stable_root, tmp_path, monkeypatch):
    import experiments.cp3_suite as module
    real_generator = module.scenario_input
    def fixture(root, family, seed, *, development=False):
        data = real_generator(root, family, seed, development=development)
        data["works"] = data["works"][:2]
        data["horizon"] = 15
        return data
    monkeypatch.setattr(module, "scenario_input", fixture)
    suite = tmp_path / "parallel"
    freeze_suite(stable_root, suite, "development")
    index = run_suite(stable_root, suite, limit=2, workers=2)
    assert [r["status"] for r in index["runs"][:2]] == ["complete", "complete"]
    assert all(r["status"] == "not_run" for r in index["runs"][2:])
    checked = audit_suite(stable_root, suite)
    executed_ids = {r["run_id"] for r in index["runs"][:2]}
    assert not any(r["run_id"] in executed_ids for r in checked["run_failures"])
