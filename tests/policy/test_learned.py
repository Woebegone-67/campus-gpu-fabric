"""Pinned inference really changes legal placement; causal guards fail closed.

Hand-authored weights below are executable test fixtures, not trained accuracy
evidence. Offline training and evaluation are tested separately.
"""

from __future__ import annotations

import hashlib
import json
import math

import pytest

from campus_fabric.policies.availability_model import (
    FEATURE_NAMES,
    MODEL_VERSION,
    WINDOW_BINS_SECONDS,
)
from campus_fabric.policies.baselines import BaselinePolicy, make_policy
from campus_fabric.policies.candidate import CandidatePolicy
from campus_fabric.policies.learned import (
    LearnedAvailabilityPolicy,
    predicted_cost_matrix,
    probability_windows,
    task_gpu_rankings,
)
from campus_fabric.policies.renewal import RenewalReliabilityPolicy
from tests.policy.test_baselines import selected
from tests.policy.test_reliability import datum
from tests.policy.test_renewal import state_with_downtime


def artifact(tmp_path, *, reverse=False, trained_through=0., name="model.json"):
    # One tanh unit turns a signed observed hour feature into opposite window
    # distributions: gpu-a(+1)->128s and gpu-z(-1)->16s, or reverse them.
    w1, w2 = [[0.] * 8 for _ in range(16)], [[0.] * 16 for _ in range(8)]
    w1[0][0] = -5. if reverse else 5.
    w2[4][0], w2[1][0] = 20., -20.
    bias = [-80.] * 8
    bias[4] = bias[1] = 0.
    value = {"schema_version": 1, "model_version": MODEL_VERSION,
             "feature_names": list(FEATURE_NAMES), "window_bins_seconds": list(WINDOW_BINS_SECONDS),
             "feature_bounds": [[-1., 1.]] * 4 + [[0., 1.]] * 4,
             "trained_through": trained_through,
             "weights": {"w1": w1, "b1": [0.] * 16, "w2": w2, "b2": bias}}
    path = tmp_path / name
    path.write_text(json.dumps(value, allow_nan=False))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def neural_state():
    state = state_with_downtime()
    metadata = list(state.metadata)
    for physical, phase in (("gpu-a", 1.), ("gpu-z", -1.)):
        values = (phase, 0., 0., 0., .5, .1, .5, .5)
        metadata.extend(datum(name, value, physical, when=state.now, source="history")
                        for name, value in zip(FEATURE_NAMES, values, strict=True))
        metadata.append(datum("availability_window_age_seconds", 0., physical,
                              when=state.now, source="observed_event"))
    return state.model_copy(update={"metadata": tuple(metadata)})


def policy(tmp_path, **kwargs):
    path, sha = artifact(tmp_path, **kwargs)
    return LearnedAvailabilityPolicy(path, expected_sha256=sha)


def test_midpoint_quantiles_are_deterministic_and_preserve_probability_mass():
    probabilities = (.125,) * 8
    windows = probability_windows(probabilities, WINDOW_BINS_SECONDS)
    assert len(windows) == 32
    assert all(windows.count(window) == 4 for window in WINDOW_BINS_SECONDS)
    assert windows == probability_windows(probabilities, WINDOW_BINS_SECONDS)


@pytest.mark.parametrize("values", [(0.,) * 8, (math.nan,) + (0.,) * 7,
                                    (-1., 2., 0., 0., 0., 0., 0., 0.),
                                    (True,) + (0.,) * 7, (.25,) * 4])
def test_invalid_predicted_probabilities_are_rejected(values):
    with pytest.raises(ValueError):
        probability_windows(values, WINDOW_BINS_SECONDS)


def test_task_conditioned_predictions_change_assignment_not_just_a_ui_score(tmp_path):
    state = neural_state()
    before = state.model_dump_json()
    base = BaselinePolicy("B2").plan(state)
    learned = policy(tmp_path)
    plan = learned.plan(state)
    assert "inactive" not in plan.rationale
    assert selected(base).placement.slices[0].node_id == "node-a"
    assert selected(plan).placement.slices[0].node_id == "node-z"
    assert selected(plan).demand_id == selected(base).demand_id == "first"
    original = next(candidate for candidate in base.candidates if candidate.candidate_id == plan.selected_candidate_id)
    assert selected(plan).placement == original.placement
    assert selected(plan).preconditions == original.preconditions
    assert selected(plan).feasibility == "legal"
    assert selected(plan).scores[-1].name == "learned_budgeted_expected_restricted_seconds"
    assert plan.model.sha256 == learned.predictor.sha256
    assert learned.plan(state) == plan
    assert state.model_dump_json() == before
    # Reverse only the pinned model, leaving current inventory/tasks/history
    # untouched: the returned actionable placement changes as well.
    reversed_plan = policy(tmp_path, reverse=True, name="reversed.json").plan(state)
    assert selected(reversed_plan).placement.slices[0].node_id == "node-a"
    assert plan.model.sha256 != reversed_plan.model.sha256


def test_long_expensive_work_gets_stable_window_while_short_cheap_work_fits_both(tmp_path):
    predictor = policy(tmp_path).predictor
    tasks = ({"estimated_compute_seconds": 10., "checkpoint_seconds": .1, "remaining_recoveries": 2},
             {"estimated_compute_seconds": 30., "checkpoint_seconds": 10., "remaining_recoveries": 2})
    devices = tuple({"features": dict(zip(FEATURE_NAMES, (phase, 0., 0., 0., .5, .1, .5, .5), strict=True)),
                     "notice_seconds": 1., "owner_use_seconds": 10.} for phase in (1., -1.))
    assert predicted_cost_matrix(tasks, devices, predictor) == ((10., 10.), (30., 900.))
    state = neural_state()
    only_long = state.demands[1].model_copy(update={"arrived_at": 0.})
    plan = policy(tmp_path, reverse=True, name="single.json").plan(
        state.model_copy(update={"demands": (only_long,)}))
    assert selected(plan).placement.slices[0].node_id == "node-z"


def test_rankings_are_task_conditioned_with_honest_ties_and_model_provenance(tmp_path):
    predictor = policy(tmp_path).predictor
    tasks = ({"task_id": "short", "estimated_compute_seconds": 10., "checkpoint_seconds": .1,
              "remaining_recoveries": 2},
             {"task_id": "long", "estimated_compute_seconds": 30., "checkpoint_seconds": 10.,
              "remaining_recoveries": 2})
    devices = tuple({"device_id": ident, "features": dict(zip(
                        FEATURE_NAMES, (phase, 0., 0., 0., .5, .1, .5, .5), strict=True)),
                     "notice_seconds": 1., "owner_use_seconds": 10.}
                    for ident, phase in (("stable", 1.), ("fragmented", -1.)))
    records = task_gpu_rankings(tasks, devices, predictor)
    short = [record for record in records if record["task_id"] == "short"]
    long = [record for record in records if record["task_id"] == "long"]
    assert [record["rank"] for record in short] == [1, 1]
    assert [(record["device_id"], record["rank"]) for record in long] == [("stable", 1), ("fragmented", 2)]
    assert all(record["model_sha256"] == predictor.sha256 for record in records)
    assert [record["predicted_restricted_seconds"] for record in long] == [30., 900.]


def test_missing_feature_and_out_of_domain_feature_keep_historical_assignment(tmp_path):
    state = neural_state()
    learned = policy(tmp_path)
    expected = RenewalReliabilityPolicy().plan(state)
    target = next(item for item in state.metadata if item.name == "hour_sin" and item.source_id == "gpu-z")
    for changed_fact in (None, target.model_copy(update={"value": 2.}),
                         target.model_copy(update={"value": True}),
                         target.model_copy(update={"source": "request"}),
                         target.model_copy(update={"observed_at": 11., "available_at": 11.})):
        facts = tuple(item for item in state.metadata if item != target)
        facts += () if changed_fact is None else (changed_fact,)
        plan = learned.plan(state.model_copy(update={"metadata": facts}))
        assert "history/B2 fallback" in plan.rationale
        assert plan.selected_candidate_id == expected.selected_candidate_id
        assert plan.candidates == expected.candidates


def test_conflicting_features_are_rejected_but_unrelated_future_keys_are_never_consumed(tmp_path):
    state = neural_state()
    learned = policy(tmp_path)
    extra = datum("actual_future_window", 900., "gpu-z", when=11.)
    assert selected(learned.plan(state.model_copy(update={"metadata": (*state.metadata, extra)}))) == selected(
        learned.plan(state))
    conflict = datum("hour_sin", .5, "gpu-z", when=state.now, source="history")
    plan = learned.plan(state.model_copy(update={"metadata": (*state.metadata, conflict)}))
    assert "Conflicting simultaneous learned feature" in plan.rationale


def test_new_window_age_must_be_authoritative_zero_at_snapshot_time(tmp_path):
    state = neural_state()
    learned = policy(tmp_path)
    target = next(item for item in state.metadata if item.name == "availability_window_age_seconds"
                  and item.source_id == "gpu-z")
    for fact in (None, target.model_copy(update={"value": 1.}),
                 target.model_copy(update={"source": "declared_profile"}),
                 target.model_copy(update={"observed_at": 9., "available_at": 9.})):
        facts = tuple(item for item in state.metadata if item != target)
        facts += () if fact is None else (fact,)
        plan = learned.plan(state.model_copy(update={"metadata": facts}))
        assert "history/B2 fallback" in plan.rationale


def test_model_cutoff_hash_and_schema_fail_closed(tmp_path):
    state = neural_state()
    future = policy(tmp_path, trained_through=11.)
    assert "training cutoff" in future.plan(state).rationale
    path, sha = artifact(tmp_path, name="tampered.json")
    path.write_text(path.read_text() + " ")
    assert "pinned model unavailable" in LearnedAvailabilityPolicy(path, expected_sha256=sha).plan(state).rationale
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    assert "pinned model unavailable" in LearnedAvailabilityPolicy(invalid,
        expected_sha256=hashlib.sha256(invalid.read_bytes()).hexdigest()).plan(state).rationale
    assert "pinned model unavailable" in LearnedAvailabilityPolicy(tmp_path / "missing.json",
        expected_sha256="0" * 64).plan(state).rationale


def test_missing_history_never_uses_model_only_cold_start(tmp_path):
    state = neural_state().model_copy(update={"history": ()})
    plan = policy(tmp_path).plan(state)
    assert "history/B2 fallback" in plan.rationale
    assert plan.selected_candidate_id == BaselinePolicy("B2").plan(state).selected_candidate_id


def test_unconfigured_factory_preserves_candidate_policy_and_non_p_baselines(monkeypatch):
    monkeypatch.delenv("FABRIC_AVAILABILITY_MODEL_PATH", raising=False)
    monkeypatch.delenv("FABRIC_AVAILABILITY_MODEL_SHA256", raising=False)
    assert isinstance(make_policy("P"), CandidatePolicy)
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_PATH", "/nonexistent")
    assert isinstance(make_policy("B2"), BaselinePolicy)


@pytest.mark.parametrize("variable", ["FABRIC_AVAILABILITY_MODEL_PATH", "FABRIC_AVAILABILITY_MODEL_SHA256"])
def test_factory_rejects_partial_model_configuration(monkeypatch, variable):
    monkeypatch.delenv("FABRIC_AVAILABILITY_MODEL_PATH", raising=False)
    monkeypatch.delenv("FABRIC_AVAILABILITY_MODEL_SHA256", raising=False)
    monkeypatch.setenv(variable, "configured")
    with pytest.raises(ValueError, match="both"):
        make_policy("P")


def test_pinned_factory_routes_p_to_real_learned_plan(tmp_path, monkeypatch):
    path, sha = artifact(tmp_path)
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_PATH", str(path))
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_SHA256", sha)
    actual = make_policy("P")
    assert isinstance(actual, LearnedAvailabilityPolicy)
    assert "inactive" not in actual.plan(neural_state()).rationale
