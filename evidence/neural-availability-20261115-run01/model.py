"""Small auditable neural availability model; training is explicitly offline.

Inference is dependency-free, accepts only causal scalar features and loads
JSON weights pinned by SHA-256. It cannot mutate a resource, postpone a recall,
or turn a forecast into a contributor promise. Bins represent available-window
seconds, not hardware speed. This initial model predicts a newly opened window;
already-aged windows require a separate conditional model.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

FEATURE_NAMES = (
    "hour_sin", "hour_cos", "weekday_sin", "weekday_cos",
    "history_log_window_mean", "history_log_window_std",
    "recent_log_window_mean", "history_short_fraction",
)
WINDOW_BINS_SECONDS = (8., 16., 32., 64., 128., 256., 512., 900.)
MODEL_VERSION = "availability-mlp-v1"
TRAINING = {"hidden_units": 16, "epochs": 60, "batch_size": 32,
            "learning_rate": .03, "seed": 20261115}


def feature_vector(features: Mapping[str, float]) -> list[float]:
    if set(features) != set(FEATURE_NAMES):
        raise ValueError("Exactly the versioned causal feature names are required")
    values = []
    for index, name in enumerate(FEATURE_NAMES):
        value = features[name]
        low = -1. if index < 4 else 0.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Feature must be numeric: {name}")
        if not math.isfinite(value) or not low <= value <= 1.:
            raise ValueError(f"Feature outside trained schema bounds: {name}")
        values.append(float(value))
    return values


def historical_features(at: float, records: Sequence[Mapping], *, utc_offset_hours: float = 0.) -> dict:
    """Extract from completed past supply windows, with explicit observation times.

    Records: started_at, ended_at, available_at, duration_seconds. No inferred
    zero-length sample for an unfinished/censored window. The caller defines
    the contributor's timezone explicitly. Four completed records minimum.
    """
    if not math.isfinite(at) or not -14 <= utc_offset_hours <= 14:
        raise ValueError("Invalid decision time or timezone")
    past = []
    for record in records:
        start, end, available, duration = (record[key] for key in (
            "started_at", "ended_at", "available_at", "duration_seconds"))
        if not all(isinstance(v, (float, int)) and not isinstance(v, bool)
                   and math.isfinite(v) for v in (start, end, available, duration)):
            raise ValueError("Invalid historical observation")
        if start >= end or duration <= 0 or available < end:
            raise ValueError("Historical supply intervals must be complete and observable")
        if end <= at and available <= at:
            past.append((end, start, duration))
    past.sort()
    if len(past) < 4:
        raise ValueError("At least four completed historical windows are required")
    past = past[-32:]
    if len({(end, start) for end, start, _ in past}) != len(past):
        raise ValueError("Duplicate supply windows cannot increase evidence")
    logs = [math.log1p(min(duration, 900.)) / math.log(901.) for _, _, duration in past]
    average = sum(logs) / len(logs)
    phase = (at + utc_offset_hours * 3600.) / 86400.
    hour = 2 * math.pi * (phase % 1.)
    weekday = 2 * math.pi * (math.floor(phase) % 7) / 7
    values = [math.sin(hour), math.cos(hour), math.sin(weekday), math.cos(weekday),
              average, math.sqrt(sum((v - average) ** 2 for v in logs) / len(logs)),
              sum(logs[-4:]) / len(logs[-4:]),
              sum(duration < 64 for _, _, duration in past) / len(past)]
    return dict(zip(FEATURE_NAMES, values, strict=True))


def _softmax(logits):
    offset = max(logits)
    exps = [math.exp(value - offset) for value in logits]
    total = sum(exps)
    return [value / total for value in exps]


def _forward(x, w1, b1, w2, b2):
    hidden = [math.tanh(sum(a * b for a, b in zip(row, x, strict=True)) + bias)
              for row, bias in zip(w1, b1, strict=True)]
    probabilities = _softmax([sum(a * b for a, b in zip(row, hidden, strict=True)) + bias
                              for row, bias in zip(w2, b2, strict=True)])
    return hidden, probabilities


def _target(row):
    label = row.get("label_bin")
    censor = row.get("censored_after_seconds")
    if (label is None) == (censor is None):
        raise ValueError("Provide one exact bin or right-censored lower bound")
    if label is not None:
        if type(label) is not int or not 0 <= label < len(WINDOW_BINS_SECONDS):
            raise ValueError("Invalid observed availability bin")
        return [label]
    if isinstance(censor, bool) or not isinstance(censor, (int, float)) or not math.isfinite(censor):
        raise ValueError("Invalid censoring bound")
    allowed = [i for i, boundary in enumerate(WINDOW_BINS_SECONDS) if boundary > censor]
    if censor < 0 or not allowed:
        raise ValueError("Censoring outside the supported discrete horizon")
    return allowed


def _gradients(x, allowed, w1, b1, w2, b2):
    h, p = _forward(x, w1, b1, w2, b2)
    likelihood = sum(p[i] for i in allowed)
    # For a censored record this is -log P(window > observed lower bound).
    dz = [value - (value / likelihood if i in allowed else 0.) for i, value in enumerate(p)]
    dh = [(1. - h[j] ** 2) * sum(dz[i] * w2[i][j] for i in range(8)) for j in range(16)]
    return ([[v * t for t in x] for v in dh], dh,
            [[v * t for t in h] for v in dz], dz, -math.log(max(likelihood, 1e-300)))


def train_model(rows: Sequence[Mapping]) -> dict:
    """Fixed MLP 8 -> tanh(16) -> softmax(8), minibatch backpropagation.

    No validation/test rows or hyperparameter search. Caller must split by label
    availability, not merely submission time. Returned losses are training-only.
    Censored targets are valid for exact discrete representative durations;
    arbitrary continuous bins require a separate interval-censoring model.
    """
    if len(rows) < 32:
        raise ValueError("Offline training requires at least 32 observed rows")
    samples = []
    latest = 0.
    for row in rows:
        observed, known = row["observed_at"], row["label_observed_at"]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) and v >= 0 for v in (observed, known)) or known < observed:
            raise ValueError("Training label must be observable after its features")
        latest = max(latest, known)
        samples.append((feature_vector(row["features"]), _target(row)))
    rng = random.Random(TRAINING["seed"])
    w1 = [[rng.uniform(-math.sqrt(6 / 24), math.sqrt(6 / 24)) for _ in range(8)] for _ in range(16)]
    w2 = [[rng.uniform(-.5, .5) for _ in range(16)] for _ in range(8)]
    b1, b2 = [0.] * 16, [0.] * 8
    losses = []
    indices = list(range(len(samples)))
    for _ in range(TRAINING["epochs"]):
        rng.shuffle(indices)
        loss = 0.
        for start in range(0, len(indices), TRAINING["batch_size"]):
            batch = indices[start:start + TRAINING["batch_size"]]
            gw1, gb1 = [[0.] * 8 for _ in range(16)], [0.] * 16
            gw2, gb2 = [[0.] * 16 for _ in range(8)], [0.] * 8
            for index in batch:
                x, allowed = samples[index]
                a, b, c, d, sample_loss = _gradients(x, allowed, w1, b1, w2, b2)
                loss += sample_loss
                for j in range(16):
                    gb1[j] += b[j]
                    for k in range(8):
                        gw1[j][k] += a[j][k]
                for j in range(8):
                    gb2[j] += d[j]
                    for k in range(16):
                        gw2[j][k] += c[j][k]
            step = TRAINING["learning_rate"] / len(batch)
            for j in range(16):
                b1[j] -= step * gb1[j]
                for k in range(8):
                    w1[j][k] -= step * gw1[j][k]
            for j in range(8):
                b2[j] -= step * gb2[j]
                for k in range(16):
                    w2[j][k] -= step * gw2[j][k]
        losses.append(loss / len(samples))
    encoded = json.dumps(list(rows), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"schema_version": 1, "model_version": MODEL_VERSION,
            "feature_names": list(FEATURE_NAMES), "window_bins_seconds": list(WINDOW_BINS_SECONDS),
            "feature_bounds": [[-1., 1.]] * 4 + [[0., 1.]] * 4,
            "trained_through": latest, "training": {**TRAINING, "rows": len(rows),
                "data_sha256": hashlib.sha256(encoded).hexdigest(), "loss_by_epoch": losses},
            "weights": {"w1": w1, "b1": b1, "w2": w2, "b2": b2},
            "scope": "newly_opened_window; discrete horizon <=900; not a calibrated guarantee"}


class AvailabilityPredictor:
    def __init__(self, artifact: Mapping, sha256: str):
        if not isinstance(artifact, Mapping):
            raise ValueError("Availability artifact must be a JSON object")
        if (artifact.get("schema_version") != 1 or artifact.get("model_version") != MODEL_VERSION
                or artifact.get("feature_names") != list(FEATURE_NAMES)
                or artifact.get("window_bins_seconds") != list(WINDOW_BINS_SECONDS)
                or artifact.get("feature_bounds") != [[-1., 1.]] * 4 + [[0., 1.]] * 4):
            raise ValueError("Unsupported availability model schema")
        self.model_version, self.sha256 = MODEL_VERSION, sha256
        self.feature_names, self.window_bins_seconds = FEATURE_NAMES, WINDOW_BINS_SECONDS
        self.trained_through = artifact["trained_through"]
        if (isinstance(self.trained_through, bool) or not isinstance(self.trained_through, (int, float))
                or not math.isfinite(self.trained_through) or self.trained_through < 0):
            raise ValueError("Invalid model training cutoff")
        weights = artifact["weights"]
        for key, outer, inner in (("w1", 16, 8), ("b1", 16, 0), ("w2", 8, 16), ("b2", 8, 0)):
            value = weights[key]
            if not isinstance(value, list) or len(value) != outer:
                raise ValueError("Wrong neural weight dimensions")
            flat = []
            for part in value:
                if inner:
                    if not isinstance(part, list) or len(part) != inner:
                        raise ValueError("Wrong neural weight dimensions")
                    flat.extend(part)
                else:
                    flat.append(part)
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                       and math.isfinite(x) and abs(x) <= 100 for x in flat):
                raise ValueError("Invalid neural weights")
        self._weights = weights

    def predict(self, features: Mapping[str, float]) -> tuple[float, ...]:
        x = feature_vector(features)
        w = self._weights
        return tuple(_forward(x, w["w1"], w["b1"], w["w2"], w["b2"])[1])


def load_predictor(path: Path | str, *, expected_sha256: str) -> AvailabilityPredictor:
    path = Path(path)
    if path.stat().st_size > 1_000_000:
        raise ValueError("Availability model exceeds size limit")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("Availability model SHA-256 does not match pinned artifact")
    return AvailabilityPredictor(json.loads(raw), digest)
