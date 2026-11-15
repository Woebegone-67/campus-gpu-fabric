"""Independent guards against future leakage and misleading loss accounting."""
import copy
import json

import pytest

from experiments.research import reclaim_comparison as study


@pytest.fixture
def protocol():
    return json.loads((study.ROOT / "evidence/reclaim-aware-20261115/protocol.json").read_text())


def test_hidden_actual_change_cannot_change_online_decision(protocol):
    original = protocol["cases"][0]
    changed = copy.deepcopy(original)
    changed["actual"].update(rate=0.01, checkpoint_seconds=99., checkpoint_fails=True)
    changed["recall_count"] = 100
    for policy in protocol["policies"]:
        left = study.select(policy, study.online_inputs(original, .25))
        right = study.select(policy, study.online_inputs(changed, .25))
        assert left == right


def test_cold_restart_counts_lost_work_once(protocol):
    case = protocol["cases"][0]
    result = study.run_case(case, "cold_restart", "return_first", 1., protocol)
    assert result["repeated_compute_seconds"] == 30.
    assert result["completion_seconds"] == pytest.approx(5 + 10 + .2 + 120.)
    assert result["cost"] == pytest.approx(120 + .2 + .1)


def test_checkpoint_failure_remains_in_denominator(protocol):
    case = copy.deepcopy(next(c for c in protocol["cases"] if c["case"] == "save-failure"))
    case["observed"]["allow_cold_restart"] = False
    result = study.run_case(case, "immediate_save", "return_first", 1., protocol)
    assert result["status"] == "failed"
    assert result["completion_seconds"] is None
    assert result["repeated_compute_seconds"] is None
    assert result["checkpoint_failures"] == 1
    assert result["truncated_completion_seconds"] == 300
    assert result["cost"] >= 300


def test_failed_timing_prediction_reaches_stop_barrier(protocol):
    case = next(c for c in protocol["cases"] if c["case"] == "save-underestimate")
    result = study.run_case(case, "latest_save", "preserve_work", .25, protocol)
    assert result["status"] == "completed"
    assert result["checkpoint_failures"] == 1
    assert result["repeated_compute_seconds"] > 30
    assert result["episodes"][0]["outcome"]["owner_wait_seconds"] == pytest.approx(5.)
    assert result["deadline_misses"] == 0


def test_stop_cost_underestimate_is_not_hidden(protocol):
    case = next(c for c in protocol["cases"] if c["case"] == "stop-underestimate")
    result = study.run_case(case, "latest_save", "preserve_work", .25, protocol)
    assert result["deadline_misses"] == 1
    assert result["max_return_lateness_seconds"] == pytest.approx(.6)


def test_repeated_recall_counts_both_returns_and_cold_work(protocol):
    case = next(c for c in protocol["cases"] if c["case"] == "repeated-recall")
    result = study.run_case(case, "cold_restart", "return_first", 1., protocol)
    assert result["recalls_observed"] == 2
    assert result["owner_wait_seconds"] == pytest.approx(.2)
    assert result["repeated_compute_seconds"] == 32.
    assert result["completion_seconds"] == pytest.approx(10 + 20 + .4 + 2 + 120)


def test_restartable_failure_preserves_failure_and_real_rollback(protocol):
    case = next(c for c in protocol["cases"] if c["case"] == "save-failure")
    result = study.run_case(case, "immediate_save", "return_first", 1., protocol)
    assert result["status"] == "completed"
    assert result["checkpoint_failures"] == 1
    assert result["repeated_compute_seconds"] == 30.
    assert result["cost"] == pytest.approx(120 + .25 + .15)


def test_strict_mode_cannot_skip_required_save_for_economy(protocol):
    case = copy.deepcopy(next(c for c in protocol["cases"] if c["case"] == "little-progress-costly-save"))
    case["observed"]["allow_cold_restart"] = False
    result = study.run_case(case, "adaptive", "return_first", 1., protocol)
    assert result["action"] != "stop"
    assert result["status"] == "completed"


def test_early_return_does_not_advance_announced_resource_return(protocol):
    case = protocol["cases"][0]
    cold = study.run_case(case, "cold_restart", "return_first", 1., protocol)
    saved = study.run_case(case, "immediate_save", "return_first", 1., protocol)
    cold_outcome, saved_outcome = cold["episodes"][0]["outcome"], saved["episodes"][0]["outcome"]
    assert cold_outcome["resource_wait_seconds"] == pytest.approx(14.9)
    assert saved_outcome["resource_wait_seconds"] == pytest.approx(14.85)
    assert cold_outcome["owner_wait_seconds"] + cold_outcome["resource_wait_seconds"] == 15
    assert saved_outcome["owner_wait_seconds"] + saved_outcome["resource_wait_seconds"] == 15


def test_checkpoint_completion_at_stop_boundary_cannot_count_as_saved(protocol):
    case = copy.deepcopy(protocol["cases"][0])
    case["observed"]["allow_cold_restart"] = False
    inputs = study.online_inputs(case, 1.)
    choice = study.select("immediate_save", inputs)
    case["actual"]["checkpoint_seconds"] = choice["stop_at"]
    outcome = study.execute_episode(case, inputs, choice)
    assert outcome["status"] == "failed"
    assert outcome["checkpoint_failed"]
    assert outcome["retained_steps"] == 0
