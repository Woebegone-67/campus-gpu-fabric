"""Causal longitudinal design and forecast-to-executor contracts, no formal fitting."""
import copy
import json
from types import SimpleNamespace

import pytest

from campus_fabric.policies import availability_model as model
from campus_fabric.policies import learned
from experiments.research import neural_availability_study as study


@pytest.fixture(scope="module")
def inputs():
    return study.generate_inputs()


def fake_predictor():
    return SimpleNamespace(trained_through=study.VALIDATION_START * 86400 - 1,
                           window_bins_seconds=model.WINDOW_BINS_SECONDS,
                           predict=lambda features: (.03, .04, .05, .08, .15, .2, .25, .2))


def test_temporal_split_and_unseen_identities_are_sealed(inputs):
    assert len(inputs["training"]) == 896
    assert len(inputs["validation"]) == 224
    assert len(inputs["cases"]) == 64
    assert max(row["label_observed_at"] for row in inputs["training"]) < study.VALIDATION_START * 86400
    assert min(row["observed_at"] for row in inputs["validation"]) >= study.VALIDATION_START * 86400
    assert max(row["label_observed_at"] for row in inputs["validation"]) < study.TEST_START * 86400
    trained_ids = {row["provider_id"] for row in inputs["training"] + inputs["validation"]}
    assert trained_ids == {f"provider-{index:02}" for index in range(8)}
    for case in inputs["cases"]:
        assert len(case["observed"]["tasks"]) == len(case["observed"]["devices"]) == 4
        for device in case["observed"]["devices"]:
            assert all(row["label_observed_at"] <= case["observed"]["at"] for row in device["labels"])
            assert all(row["available_at"] <= case["observed"]["at"] for row in device["completed_history"])
            if case["group"] == "unseen_provider":
                assert device["device_id"] not in trained_ids
                assert len(device["completed_history"]) >= 4


def test_late_label_and_unseen_training_provider_are_rejected(inputs):
    crossed = copy.deepcopy(inputs)
    crossed["training"][-1]["label_observed_at"] = study.VALIDATION_START * 86400
    with pytest.raises(ValueError, match="temporal boundary"):
        study.validate_inputs(crossed)
    unseen = copy.deepcopy(inputs)
    unseen["training"][0]["provider_id"] = "provider-11"
    with pytest.raises(ValueError, match="Unseen providers"):
        study.validate_inputs(unseen)


def test_completed_features_ignore_unobserved_records_and_never_include_censoring(inputs):
    observed = inputs["cases"][0]["observed"]
    device, at = observed["devices"][0], observed["at"]
    augmented = copy.deepcopy(device["completed_history"])
    augmented.append({"started_at": at - 1, "ended_at": at + 4,
                      "available_at": at + 4, "duration_seconds": 5.})
    augmented.append({"started_at": at - 100, "ended_at": at - 1,
                      "available_at": at + 1, "duration_seconds": 99.})
    assert model.historical_features(at, augmented) == device["features"]
    completed_starts = {row["started_at"] for row in device["completed_history"]}
    censored_starts = {row["observed_at"] for row in device["labels"] if "censored_after_seconds" in row}
    assert censored_starts
    assert completed_starts.isdisjoint(censored_starts)
    assert 0 < sum("censored_after_seconds" in row for row in inputs["training"])
    assert sum("censored_after_seconds" in row for row in inputs["training"]) < inputs["observation_cap_selected"]["training"]


def test_shift_is_future_only_and_uninformative_truth_has_no_provider_time_signal():
    for day in (0, study.TEST_START - 1):
        assert study.probe(0, day, 0, "predictable") == study.probe(0, day, 0, "abrupt_shift")
        assert study.probe(0, day, 0, "predictable") == study.probe(0, day, 0, "uninformative")
    assert study.distribution(0, study.TEST_START, 0, "predictable") != study.distribution(0, study.TEST_START, 0, "abrupt_shift")
    assert study.distribution(0, study.TEST_START, 0, "uninformative") == (1 / 8,) * 8
    assert study.distribution(11, study.TEST_START + 3, 3, "uninformative") == (1 / 8,) * 8


def test_histories_and_task_cohort_are_identical_at_first_shift_decision(inputs):
    first = {case["group"]: case for case in inputs["cases"] if case["index"] == 0}
    assert first["predictable"]["observed"] == first["abrupt_shift"]["observed"]
    assert first["predictable"]["observed"] == first["uninformative"]["observed"]
    for case in inputs["cases"]:
        assert case["observed"]["tasks"] == study.task_specs(case["index"])
        for calendar, label in zip(case["actual"]["devices"], case["actual"]["forecast_labels"], strict=True):
            assert len(calendar["windows"]) == 64
            assert sum(calendar["windows"]) + 64 * (12 + 10) >= 900
            if "label_bin" in label:
                assert calendar["windows"][0] == model.WINDOW_BINS_SECONDS[label["label_bin"]]
            else:
                assert calendar["windows"][0] > label["censored_after_seconds"]


def test_empirical_forecasts_handle_censored_tail_instead_of_fabricating_recalls():
    exact = study.empirical_distribution([{"label_bin": 0}] * 12)
    censored = study.empirical_distribution([{"censored_after_seconds": 256.}] * 12)
    assert sum(exact) == pytest.approx(1.)
    assert exact[0] > .7
    assert censored[6] + censored[7] > .8
    assert censored[0] == pytest.approx(censored[5])
    with pytest.raises(ValueError, match="no support"):
        study.empirical_distribution([{"censored_after_seconds": 900.}])


def test_unseen_future_cannot_change_any_prediction(inputs):
    case = copy.deepcopy(inputs["cases"][0])
    expected = {name: study.forecast(name, case["observed"], fake_predictor()) for name in study.FORECASTERS}
    case["actual"]["devices"][0]["windows"] = [8.] * 64
    case["actual"]["tasks"][0]["compute_seconds"] = 100000.
    assert expected == {name: study.forecast(name, case["observed"], fake_predictor()) for name in study.FORECASTERS}
    delayed = copy.deepcopy(case["observed"])
    delayed["devices"][0]["labels"].append({"label_observed_at": delayed["at"] + 1,
        "observed_at": delayed["at"], "label_bin": 0, "slot": 0, "weekday_class": "weekday"})
    for name in study.FORECASTERS:
        assert study.forecast(name, delayed, fake_predictor()) == expected[name]
    future_model = fake_predictor()
    future_model.trained_through = case["observed"]["at"] + 1
    with pytest.raises(ValueError, match="before the decision"):
        study.forecast("neural", case["observed"], future_model)


def test_neural_cost_path_matches_frozen_adapter_and_matching_is_same_cost(inputs):
    observed = inputs["cases"][0]["observed"]
    predictor = fake_predictor()
    masses = study.forecast("neural", observed, predictor)
    matrix = study.matrix_from_forecasts(observed, masses)
    assert matrix == learned.predicted_cost_matrix(observed["tasks"], observed["devices"], predictor)
    greedy, matching = study.assign(matrix, "greedy"), study.assign(matrix, "batch")
    assert sorted(greedy) == sorted(matching) == list(range(4))
    assert sum(matrix[i][j] for i, j in enumerate(matching)) <= sum(matrix[i][j] for i, j in enumerate(greedy)) + 1e-8
    assert study.assign(((1., 1.), (1., 1.)), "batch") == (0, 1)


def test_timebucket_uses_only_causal_matching_bucket_and_falls_back(inputs):
    observed = copy.deepcopy(inputs["cases"][0]["observed"])
    for device in observed["devices"]:
        device["labels"] = device["labels"][-3:]
    assert study.forecast("timebucket", observed) == study.forecast("provider", observed)
    original = inputs["cases"][0]["observed"]
    for device, masses in zip(original["devices"], study.forecast("timebucket", original), strict=True):
        bucket = [row for row in device["labels"] if row["slot"] == original["slot"]
                  and row["weekday_class"] == ("weekend" if original["day"] % 7 >= 5 else "weekday")][-32:]
        assert len(bucket) >= 4
        assert masses == study.empirical_distribution(bucket)


def test_restricted_endpoint_keeps_budget_exhausted_jobs_and_full_denominator():
    task = {"task_id": "task-0", "estimated_compute_seconds": 320., "checkpoint_seconds": 18.,
            "restart_seconds": .5, "stop_seconds": .1, "remaining_recoveries": 2}
    device = {"device_id": "device-0", "notice_seconds": 12., "owner_use_seconds": 10.}
    case = {"group": "uninformative", "index": 0, "day": study.TEST_START,
            "observed": {"tasks": [task], "devices": [device]},
            "actual": {"tasks": [{"task_id": "task-0", "compute_seconds": 320.,
                                    "checkpoint_seconds": 18., "checkpoint_fails": False}],
                       "devices": [{"windows": [8.] * 64, "notice_seconds": 12.,
                                    "owner_use_seconds": 10., "stop_seconds": .1}]}}
    row = study.execute_assignment(case, "neural_batch", (0,), ((900.,),))
    assert row["admitted"] == 1 and row["completed"] == 0
    assert row["budget_exhausted"] == 1 and row["censored"] == 0
    assert row["restricted_mean_completion_seconds"] == 900.
    assert row["jobs"][0]["recoveries"] == 2


def test_forecast_scores_keep_exact_and_censored_semantics_separate():
    masses = (.125,) * 8
    exact = study.score_forecast(masses, {"label_bin": 3})
    censored = study.score_forecast(masses, {"censored_after_seconds": 256.})
    assert exact["exact_label_brier"] == pytest.approx(.875)
    assert not exact["censored"] and censored["censored"]
    assert censored["exact_label_brier"] is None
    assert censored["negative_log_likelihood"] < exact["negative_log_likelihood"]


def test_freeze_is_training_free_and_tampering_or_second_run_is_blocked(tmp_path, monkeypatch):
    source = tmp_path / "fixture-source.py"
    source.write_text("fixture = 1\n")
    monkeypatch.setattr(study, "sources", lambda: {"fixture-source.py": source})
    monkeypatch.setattr(study, "generate_inputs", lambda: {"fixture": "no formal inputs"})
    monkeypatch.setattr(study, "validate_inputs", lambda data: None)
    monkeypatch.setattr(model, "train_model", lambda *a, **k: pytest.fail("Freeze must not train"))
    output = tmp_path / "temporary-test-freeze"
    study.freeze(output)
    manifest, data = study.verify_frozen(output)
    assert manifest["status"] == "FROZEN_NOT_RUN"
    assert data == {"fixture": "no formal inputs"}
    assert not (output / "model.json").exists()
    with pytest.raises(FileExistsError, match="reuse"):
        study.freeze(output)
    (output / "inputs.json").write_text(json.dumps({"changed": True}))
    with pytest.raises(ValueError, match="Frozen artifact changed"):
        study.run_frozen(output)
    assert not (output / "RUN_STARTED").exists()
    (output / "RUN_STARTED").write_text("prior failed attempt\n")
    with pytest.raises(FileExistsError, match="rerun"):
        study.run_frozen(output)


def test_live_source_change_is_rejected_before_model_training(tmp_path, monkeypatch):
    source = tmp_path / "fixture-source.py"
    source.write_text("before\n")
    monkeypatch.setattr(study, "sources", lambda: {"fixture-source.py": source})
    monkeypatch.setattr(study, "generate_inputs", lambda: {})
    monkeypatch.setattr(study, "validate_inputs", lambda data: None)
    output = tmp_path / "temporary-test-freeze"
    study.freeze(output)
    source.write_text("after\n")
    with pytest.raises(ValueError, match="Source changed after freeze"):
        study.run_frozen(output)
    assert not (output / "RUN_STARTED").exists()
