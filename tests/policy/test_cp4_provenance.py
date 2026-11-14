"""Real development traces and adversarial source edits; no formal policy claims."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from campus_fabric.workflows.acceptance_evidence import _gate_module
from experiments.cp3_suite import (
    analyze_suite,
    digest,
    freeze_suite,
    read_artifact,
    run_suite,
    write_json,
)
from experiments.cp4_provenance import audit_provenance, main

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "source"
    for label in ("configs/cp1_inventory.json", "scripts/check_checkpoint.py"):
        target = root / label
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / label, target)
    return root


@pytest.fixture
def executed(source_root, monkeypatch):
    import experiments.cp3_suite as suite_module

    original = suite_module.scenario_input

    def small_development(root, family, seed, *, development=False):
        assert development, "This test fixture must never execute final validation inputs"
        value = original(root, family, seed, development=True)
        value["works"] = value["works"][:3]
        value["horizon"] = 20
        return value

    monkeypatch.setattr(suite_module, "scenario_input", small_development)
    suite = source_root / "evidence/cp3/policy-suite"
    freeze_suite(source_root, suite, "development")
    index = run_suite(source_root, suite, limit=1)
    assert index["runs"][0]["status"] == "complete"
    return source_root, suite, index


def register_result(root, suite, index):
    row = index["runs"][0]
    summary_ref = row["artifacts"]["summary"]
    summary = read_artifact(suite, summary_ref["path"], summary_ref["sha256"])
    output = root / "evidence/cp4/results/completed.json"
    write_json(output, {"run_id": row["run_id"], "works": summary["works"]})
    figure_index = root / "evidence/cp4/figure-index.json"
    document = {"schema": "cp4-figure-index-v1", "build": _gate_module(root).current_build(root),
                "figures": [{"id": "first-result", "kind": "result",
                             "path": output.relative_to(root).as_posix(), "sha256": digest(output.read_bytes()),
                             "sources": [{"run_id": row["run_id"], "artifact": "summary",
                                          "sha256": summary_ref["sha256"], "json_pointer": "/works",
                                          "value": summary["works"]}]}]}
    write_json(figure_index, document)
    return figure_index, document


def test_absent_suite_and_empty_figure_index_cannot_pass(source_root):
    suite = source_root / "evidence/cp3/policy-suite"
    report = audit_provenance(source_root, suite)
    assert report["status"] == report["suite_sources"]["status"] == "NOT_RUN"
    assert report["figures"]["status"] == "NOT_RUN"
    assert report["coverage"]["verified_runs"] == report["coverage"]["verified_figures"] == 0
    freeze_suite(source_root, suite, "development")
    figure_index = source_root / "evidence/cp4/figure-index.json"
    write_json(figure_index, {"schema": "cp4-figure-index-v1", "build": _gate_module(source_root).current_build(source_root), "figures": []})
    report = audit_provenance(source_root, suite, figure_index)
    assert report["status"] == "NOT_RUN"
    assert report["coverage"]["expected_runs"] == 50
    assert report["coverage"]["verified_runs"] == 0
    assert report["figures"]["status"] == "NOT_RUN"


def test_real_partial_chain_and_result_keep_missing_coverage_visible(executed):
    root, suite, index = executed
    figure_index, _ = register_result(root, suite, index)
    report = audit_provenance(root, suite, figure_index)
    assert report["status"] == "NOT_RUN"
    assert report["coverage"]["expected_runs"] == 50
    assert report["coverage"]["verified_runs"] == 1
    assert report["coverage"]["run_status_counts"] == {"complete": 1, "not_run": 49}
    assert report["suite_sources"]["runs"][0]["status"] == "PASS"
    assert report["figures"]["status"] == "PASS"
    assert report["coverage"]["verified_figures"] == 1
    assert report["algorithm_target"]["status"] == "NOT_APPLICABLE"
    assert report["build_at_finish"] == report["build"]


@pytest.mark.parametrize("field", ["run_id", "input_sha256", "model"])
def test_rehashed_metadata_forgery_cannot_change_run_identity_or_model(executed, field):
    root, suite, index = executed
    ref = index["runs"][0]["artifacts"]["run-metadata"]
    metadata = read_artifact(suite, ref["path"], ref["sha256"])
    metadata[field] = {"name": "unregistered-model"} if field == "model" else "forged"
    write_json(suite / ref["path"], metadata)
    ref["sha256"] = digest((suite / ref["path"]).read_bytes())
    write_json(suite / "suite-index.json", index)
    report = audit_provenance(root, suite)
    assert report["status"] == "FAIL"
    assert report["coverage"]["verified_runs"] == 0
    assert "chain differs" in report["suite_sources"]["runs"][0]["reason"]


def test_source_change_and_raw_file_damage_fail_closed(executed):
    root, suite, index = executed
    ref = index["runs"][0]["artifacts"]["summary"]
    path = suite / ref["path"]
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    report = audit_provenance(root, suite)
    assert report["status"] == "FAIL"
    assert "hash mismatch" in report["suite_sources"]["reason"]
    path.write_bytes(original)
    (root / "scripts/candidate-change.py").write_text("# New candidate source\n")
    report = audit_provenance(root, suite)
    assert report["status"] == "FAIL"
    assert "current checkout" in report["suite_sources"]["reason"]


def test_rehashed_cached_analysis_cannot_override_recalculated_values(executed):
    root, suite, _ = executed
    analysis = analyze_suite(root, suite)
    import json

    index = json.loads((suite / "suite-index.json").read_text())
    analysis["target_met"] = True
    write_json(suite / "analysis.json", analysis)
    index["analysis"]["sha256"] = digest((suite / "analysis.json").read_bytes())
    write_json(suite / "suite-index.json", index)
    report = audit_provenance(root, suite)
    assert report["status"] == "FAIL"
    assert "raw-run recalculation" in report["suite_sources"]["reason"]


@pytest.mark.parametrize("damage", ["value", "hash", "unknown-run", "output-bytes", "path", "symlink"])
def test_figure_sources_reject_forged_values_files_and_unsafe_paths(executed, damage):
    root, suite, index = executed
    figure_index, document = register_result(root, suite, index)
    figure = document["figures"][0]
    source = figure["sources"][0]
    if damage == "value":
        source["value"] = []
    elif damage == "hash":
        source["sha256"] = "0" * 64
    elif damage == "unknown-run":
        source["run_id"] = index["runs"][1]["run_id"]
    elif damage == "output-bytes":
        (root / figure["path"]).write_bytes(b"changed\n")
    elif damage == "path":
        figure["path"] = "evidence/cp4/../../../outside.json"
    else:
        output = root / figure["path"]
        target = root / "outside.json"
        target.write_bytes(output.read_bytes())
        output.unlink()
        output.symlink_to(target)
    write_json(figure_index, document)
    report = audit_provenance(root, suite, figure_index)
    assert report["status"] == "FAIL"
    assert report["figures"]["status"] == "FAIL"
    assert report["coverage"]["verified_figures"] == 0


def test_cli_writes_not_run_receipt_and_preserves_existing_output(source_root):
    output = source_root / "evidence/cp4/provenance.json"
    assert main(["--root", str(source_root), "--output", str(output)]) == 1
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        main(["--root", str(source_root), "--output", str(output)])
    assert output.read_bytes() == before


@pytest.mark.parametrize("changed", ["artifact", "candidate"])
def test_sources_and_candidate_must_remain_stable_through_figure_audit(executed, monkeypatch, changed):
    import experiments.cp4_provenance as module

    root, suite, index = executed
    figure_index, _ = register_result(root, suite, index)
    actual_figures = module._figures

    def concurrent_change(*args):
        result = actual_figures(*args)
        if changed == "artifact":
            ref = index["runs"][0]["artifacts"]["events"]
            path = suite / ref["path"]
            path.write_bytes(path.read_bytes() + b" ")
        else:
            (root / "scripts/concurrent-change.py").write_text("# Concurrent source edit\n")
        return result

    monkeypatch.setattr(module, "_figures", concurrent_change)
    report = audit_provenance(root, suite, figure_index)
    assert report["status"] == ("FAIL" if changed == "artifact" else "STALE")
    if changed == "artifact":
        assert any(row["id"] == "source-snapshot-stable" and row["status"] == "FAIL" for row in report["checks"])
    else:
        assert report["build_at_finish"] != report["build"]
