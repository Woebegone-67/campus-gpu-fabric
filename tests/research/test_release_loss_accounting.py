"""Guard the research accounting against false benefit and dropped failures."""
import json
from dataclasses import replace
from types import SimpleNamespace

from experiments.research import release_loss_accounting as study


def test_completed_work_counts_saved_progress_once_not_entire_stopped_attempt(tmp_path, monkeypatch):
    # First computes 100, saves 80; next computes 80..200. Only 20 are redundant.
    monkeypatch.setattr(study, "CONFIG", replace(study.CONFIG, total_steps=200))
    records = []
    for generation, initial, final in ((1, 0, 100), (2, 80, 200)):
        key = str(generation)
        directory = tmp_path / "attempts" / key
        directory.mkdir(parents=True)
        (directory / "research-measurements.json").write_text(json.dumps({
            "compute": [{"start_step": initial, "end_step": final,
                         "start_ns": 0, "end_ns": 100, "cpu_ns": 90}],
            "writes": [{"filename": "started.json", "kind": "started", "step": initial,
                        "start_ns": 0, "end_ns": 10, "cpu_ns": 1, "bytes": 20}],
            "worker_start_ns": 0, "worker_end_ns": 100, "worker_cpu_ns": 95,
        }))
        records.append({"key": key, "attempt": {"generation": generation},
                        "initial": {"step": initial}, "resumed_from": "cp" if initial else None,
                        "stop_confirmed": True, "exit_code": 0})
    executor = SimpleNamespace(root=tmp_path, inspect=lambda: {"attempts": records}, verifications=[])
    output = {"status": "completed", "artifacts": [{"payload": {
        "kind": "checkpoint", "state": {"step": 80}}}], "finished_ns": 100,
        "stop_confirmed_ns": 50, "notice_ns": 0,
        "dispatches": [{"generation": 2, "start_ns": 60}]}
    result = study.finalize(executor, output, 0)
    assert result["metrics"]["executed_steps"] == 220
    assert result["metrics"]["discarded_first_attempt_steps"] == 20
    assert result["metrics"]["redundant_steps_if_completed"] == 20
    assert result["measurements"][1]["initial_step"] == 80


def test_failure_remains_in_common_horizon_denominator(tmp_path):
    executor = SimpleNamespace(root=tmp_path, inspect=lambda: {"attempts": []}, verifications=[])
    result = study.finalize(executor, {"status": "failed", "artifacts": []}, 0)
    assert result["metrics"]["truncated_completion_cost_seconds"] == study.HORIZON
    assert result["metrics"]["end_to_end_seconds"] is None
    assert result["metrics"]["redundant_steps_if_completed"] is None


def test_baseline_requires_causal_progress_and_preserves_stop_margin():
    assert study.strong_baseline([])["action"] == "checkpoint"
    near_end = [{"step": 8_000_000, "observed_ns": 0},
                {"step": 9_000_000, "observed_ns": 100_000_000}]
    assert study.strong_baseline(near_end)["action"] == "finish"
    slow = [{"step": 0, "observed_ns": 0}, {"step": 1_000_000, "observed_ns": 1_000_000_000}]
    assert study.strong_baseline(slow)["action"] == "checkpoint"


def test_cold_restart_has_new_generation_and_no_checkpoint():
    command = study.command("start", 2)
    assert command.attempt.generation == 2
    assert command.attempt.intent_id == study.command("start", 1).attempt.intent_id
    assert command.checkpoint is None
