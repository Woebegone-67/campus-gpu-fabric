"""Calculator examples are synthetic unit-test inputs, never runtime acceptance."""

import copy
import runpy
from pathlib import Path

import pytest

CALCULATOR = runpy.run_path(str(Path(__file__).resolve().parents[2] / "experiments/compare_runs.py"))
aggregate_pairs = CALCULATOR["aggregate_pairs"]
compare_runs = CALCULATOR["compare_runs"]


def run(**changes):
    value = {
        "run_id": "synthetic-unit-test", "policy": "B2", "scenario_family": "test-only",
        "seed": 1, "input_sha256": "1" * 64, "model_version": "test-only",
        "horizon": 1000, "service_unit": "test-normalized-work",
        "cancellation_policy": "count_until_horizon",
        "works": [
            {"intent_id": "a", "owner_id": "owner", "arrived_at": 0,
             "completed_at": 110, "status": "completed", "effective_service": 10},
            {"intent_id": "b", "owner_id": "owner", "arrived_at": 20,
             "completed_at": None, "status": "failed", "effective_service": 0},
        ],
    }
    value.update(changes)
    return value


def test_initial_allocation_wait_and_failed_jobs_remain_in_denominator():
    original = run()
    assert compare_runs(original, original)["baseline"]["unfulfilled_demand_area"] == 1090
    assert compare_runs(original, original)["baseline"]["not_completed_at_horizon"] == 1


def test_harm_threshold_and_completed_count_compare_same_identities():
    base = run()
    candidate = copy.deepcopy(base)
    candidate["works"][0].update(completed_at=411, effective_service=7)
    result = compare_runs(base, candidate)
    assert result["per_work"][0]["severe_harm_gt_300_seconds"] is True
    assert result["per_owner"][0]["severe_harm_below_80_percent"] is True
    assert result["completion_count_not_lower"] is True


@pytest.mark.parametrize("field,value", [("input_sha256", "2" * 64), ("horizon", 2000)])
def test_mismatched_input_or_window_is_not_a_paired_experiment(field, value):
    with pytest.raises(ValueError, match=field):
        compare_runs(run(), run(**{field: value}))


def test_missing_failed_job_cannot_improve_candidate_metric():
    candidate = run()
    candidate["works"].pop()
    with pytest.raises(ValueError, match="cohort"):
        compare_runs(run(), candidate)


def test_zero_denominator_regression_is_visible_instead_of_excluded():
    base = run()
    base["works"] = [base["works"][0]]
    base["works"][0]["completed_at"] = 0
    candidate = copy.deepcopy(base)
    candidate["works"][0]["completed_at"] = 10
    pair = compare_runs(base, candidate)
    assert pair["area_ratio"] is None
    assert pair["zero_baseline_regression"] is True
    assert aggregate_pairs([pair])["macro_area_ratio"] is None


def test_strategy_specific_cancellation_rejected():
    candidate = run()
    candidate["works"][1].update(status="cancelled", cancelled_at=50)
    with pytest.raises(ValueError, match="cancelled_at"):
        compare_runs(run(), candidate)


def test_complete_after_horizon_is_not_a_window_completion():
    value = run()
    value["works"][0]["completed_at"] = 2000
    result = compare_runs(value, value)
    assert result["baseline"]["completed"] == 0
    assert result["baseline"]["unfulfilled_demand_area"] == 1980


def test_duplicate_seed_cannot_reweight_family():
    pair = compare_runs(run(), run())
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_pairs([pair, pair])
