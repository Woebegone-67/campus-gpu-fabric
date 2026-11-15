"""Real gradients, causal features, trained weights and pinned inference."""
import hashlib
import json
import math

import pytest

from campus_fabric.policies import availability_model as model


def features(value=0.):
    return dict(zip(model.FEATURE_NAMES, [value, 0., 0., 0., .5, .1, .5, .5], strict=True))


@pytest.fixture(scope="module")
def artifact():
    return model.train_model([
        {"features": features(-1. if i % 2 else 1.), "label_bin": 0 if i % 2 else 7,
         "observed_at": float(i * 1000), "label_observed_at": float(i * 1000 + 900)}
        for i in range(128)
    ])


def write_model(tmp_path, artifact):
    path = tmp_path / "availability.json"
    raw = json.dumps(artifact).encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_learns_opposite_history_contexts_and_pinned_json_matches(artifact, tmp_path):
    path, digest = write_model(tmp_path, artifact)
    predictor = model.load_predictor(path, expected_sha256=digest)
    for value, label in ((-1., 0), (1., 7)):
        probabilities = predictor.predict(features(value))
        assert probabilities[label] > .8
        assert sum(probabilities) == pytest.approx(1.)
    assert artifact["training"]["loss_by_epoch"][-1] < artifact["training"]["loss_by_epoch"][0]
    assert predictor.trained_through == 127900
    with pytest.raises(ValueError, match="SHA-256"):
        model.load_predictor(path, expected_sha256="0" * 64)


@pytest.mark.parametrize("censored", [False, True])
def test_backprop_matches_independent_finite_difference(artifact, censored):
    weights = artifact["weights"]
    w1, b1, w2, b2 = (weights[key] for key in ("w1", "b1", "w2", "b2"))
    x = model.feature_vector(features(.3))
    allowed = [5, 6, 7] if censored else [3]
    analytic = model._gradients(x, allowed, w1, b1, w2, b2)
    for array, i, j, expected in ((w1, 2, 0, analytic[0][2][0]),
                                  (w2, 3, 2, analytic[2][3][2])):
        before = array[i][j]
        epsilon = 1e-5
        array[i][j] = before + epsilon
        plus = -math.log(sum(model._forward(x, w1, b1, w2, b2)[1][k] for k in allowed))
        array[i][j] = before - epsilon
        minus = -math.log(sum(model._forward(x, w1, b1, w2, b2)[1][k] for k in allowed))
        array[i][j] = before
        assert (plus - minus) / (2 * epsilon) == pytest.approx(expected, abs=1e-7)


def test_censored_labels_keep_all_surviving_bins_and_never_impute_recall():
    assert model._target({"censored_after_seconds": 64.}) == [4, 5, 6, 7]
    assert model._target({"censored_after_seconds": 0.}) == list(range(8))
    for value in ({"censored_after_seconds": 900.}, {"label_bin": True},
                  {"label_bin": 1, "censored_after_seconds": 64.}):
        with pytest.raises(ValueError):
            model._target(value)


def test_feature_extraction_excludes_future_and_late_observations():
    records = [{"started_at": i * 100., "ended_at": i * 100. + 32,
                "available_at": i * 100. + 33, "duration_seconds": 32.} for i in range(4)]
    baseline = model.historical_features(500., records)
    late = {"started_at": 400., "ended_at": 416., "available_at": 600., "duration_seconds": 16.}
    future = {"started_at": 700., "ended_at": 716., "available_at": 716., "duration_seconds": 16.}
    assert model.historical_features(500., [*records, late, future]) == baseline
    with pytest.raises(ValueError, match="four"):
        model.historical_features(200., records)
    with pytest.raises(ValueError, match="Duplicate"):
        model.historical_features(500., [*records, records[0]])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.01, True, "1"])
def test_inference_rejects_invalid_or_outside_schema_features(artifact, tmp_path, value):
    path, digest = write_model(tmp_path, artifact)
    predictor = model.load_predictor(path, expected_sha256=digest)
    bad = features()
    bad["hour_sin"] = value
    with pytest.raises(ValueError):
        predictor.predict(bad)


def test_training_rejects_label_known_before_features():
    rows = [{"features": features(), "label_bin": 1, "observed_at": 10.,
             "label_observed_at": 9.}] * 32
    with pytest.raises(ValueError, match="observable"):
        model.train_model(rows)


def test_malformed_model_dimensions_are_rejected_even_with_matching_hash(artifact, tmp_path):
    changed = json.loads(json.dumps(artifact))
    changed["weights"]["w1"][0] = [1.]
    path, digest = write_model(tmp_path, changed)
    with pytest.raises(ValueError, match="dimensions"):
        model.load_predictor(path, expected_sha256=digest)
