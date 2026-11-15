"""Prospective synthetic neural availability research; never a production runner."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from campus_fabric.policies import availability_model as model
from campus_fabric.policies import learned, reliability, renewal
from experiments.research import renewal_comparison as executor

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = Path(__file__).with_name("NEURAL_AVAILABILITY_PROTOCOL.md")
GROUPS = ("predictable", "abrupt_shift", "uninformative", "unseen_provider")
FORECASTERS = ("pooled", "provider", "timebucket", "neural")
POLICIES = ("pooled_batch", "provider_batch", "timebucket_batch",
            "neural_greedy", "neural_batch")
CONFIG = {
    "version": "neural-availability-longitudinal-v1", "seed": 20261115,
    "warmup_days": 7, "train_days": 28, "validation_days": 7, "test_days": 4,
    "slots_hours": [0, 6, 12, 18], "seen_providers": 8, "unseen_providers": 4,
    "history_limit": 32, "minimum_history": 4, "calendar_windows": 64,
    "observation_cap_probability": .20, "observation_caps_seconds": [64., 128., 256.],
    "empirical_iterations": 20, "empirical_pseudocount": .5,
    "horizon_seconds": 900., "lease_ttl_seconds": 900., "max_recoveries": 2,
    "owner_use_seconds": 10., "notice_seconds": 12., "restart_seconds": .5,
    "stop_seconds": .1, "declared_compute_seconds": [24., 72., 160., 320.],
    "checkpoint_seconds": [.5, 3., 10., 18.], "utc_offset_hours": 0.,
    "provider_offsets": [-.65, -.4, -.15, .1, .3, .5, .7, .9, -.5, -.05, .4, .8],
    "category_center": 3.7, "daily_amplitude": 1.1, "daily_phase": -.7,
    "weekday_amplitude": .55, "category_noise_scale": .8,
}
TRAIN_START = CONFIG["warmup_days"]
VALIDATION_START = TRAIN_START + CONFIG["train_days"]
TEST_START = VALIDATION_START + CONFIG["validation_days"]
END_DAY = TEST_START + CONFIG["test_days"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def random_stream(*parts) -> random.Random:
    return random.Random(":".join(map(str, (CONFIG["version"], CONFIG["seed"], *parts))))


def timestamp(day: int, slot: int) -> float:
    return float(day * 86400 + CONFIG["slots_hours"][slot] * 3600)


def distribution(provider: int, day: int, slot: int, group: str) -> tuple[float, ...]:
    """Private synthetic truth, called only by input generation."""
    if group == "uninformative" and day >= TEST_START:
        return (1 / 8,) * 8
    pattern = (CONFIG["provider_offsets"][provider]
               + CONFIG["daily_amplitude"] * math.sin(
                   2 * math.pi * CONFIG["slots_hours"][slot] / 24 + CONFIG["daily_phase"])
               + CONFIG["weekday_amplitude"] * math.cos(2 * math.pi * (day % 7) / 7))
    if group == "abrupt_shift" and day >= TEST_START:
        pattern *= -1
    center = CONFIG["category_center"] + pattern
    masses = [math.exp(-.5 * ((index - center) / CONFIG["category_noise_scale"]) ** 2)
              for index in range(8)]
    return tuple(value / sum(masses) for value in masses)


def draw_bin(probabilities, rng) -> int:
    threshold, cumulative = rng.random(), 0.
    for index, probability in enumerate(probabilities):
        cumulative += probability
        if threshold <= cumulative:
            return index
    return len(probabilities) - 1


def probe(provider: int, day: int, slot: int, group: str) -> tuple[dict, dict | None, dict]:
    """Return an observable label, a completed record (if any), and private truth."""
    context = group if day >= TEST_START else "shared-past"
    probabilities = distribution(provider, day, slot, group)
    category = draw_bin(probabilities, random_stream("probe", context, provider, day, slot))
    duration, started = model.WINDOW_BINS_SECONDS[category], timestamp(day, slot)
    censor_rng = random_stream("observation-cap", context, provider, day, slot)
    cap = (censor_rng.choice(CONFIG["observation_caps_seconds"])
           if censor_rng.random() < CONFIG["observation_cap_probability"] else None)
    row = {"provider_id": f"provider-{provider:02}", "observed_at": started,
           "label_observed_at": started + (min(duration, cap) if cap is not None else duration),
           "day": day, "slot": slot, "weekday_class": "weekend" if day % 7 >= 5 else "weekday"}
    completed = None
    if cap is not None and duration > cap:
        row["censored_after_seconds"] = cap
    else:
        row["label_bin"] = category
        completed = {"started_at": started, "ended_at": started + duration,
                     "available_at": started + duration, "duration_seconds": duration}
    return row, completed, {"label_bin": category, "probabilities": probabilities,
                            "observation_cap_selected": cap is not None}


def visible_labels(records: list[dict], at: float) -> list[dict]:
    return sorted((record for record in records if record["label_observed_at"] <= at),
                  key=lambda record: (record["label_observed_at"], record["observed_at"]))


def device_observation(provider, at, labels, completed) -> dict:
    past_labels = visible_labels(labels, at)
    past_complete = [record for record in completed if record["available_at"] <= at]
    return {"device_id": f"provider-{provider:02}", "features": model.historical_features(
                at, past_complete, utc_offset_hours=CONFIG["utc_offset_hours"]),
            "labels": copy.deepcopy(past_labels), "completed_history": copy.deepcopy(past_complete),
            "notice_seconds": CONFIG["notice_seconds"],
            "owner_use_seconds": CONFIG["owner_use_seconds"]}


def task_specs(index: int) -> list[dict]:
    durations, checkpoints = CONFIG["declared_compute_seconds"][:], CONFIG["checkpoint_seconds"][:]
    random_stream("task-durations", index).shuffle(durations)
    random_stream("task-checkpoints", index).shuffle(checkpoints)
    return [{"task_id": f"task-{i}", "estimated_compute_seconds": duration,
             "checkpoint_seconds": checkpoints[i], "restart_seconds": CONFIG["restart_seconds"],
             "stop_seconds": CONFIG["stop_seconds"], "remaining_recoveries": CONFIG["max_recoveries"]}
            for i, duration in enumerate(durations)]


def generate_inputs() -> dict:
    """Generate all frozen inputs without training, prediction, or execution."""
    training, validation, cases = [], [], []
    label_history = {provider: [] for provider in range(12)}
    completed_history = {provider: [] for provider in range(12)}
    observation_caps = {"training": 0, "validation": 0}
    for day in range(TEST_START):
        for slot in range(4):
            at = timestamp(day, slot)
            for provider in range(12):
                row, complete, truth = probe(provider, day, slot, "predictable")
                if day >= TRAIN_START and provider < CONFIG["seen_providers"]:
                    observed = device_observation(provider, at, label_history[provider], completed_history[provider])
                    sample = {**row, "features": observed["features"]}
                    target = training if day < VALIDATION_START else validation
                    target.append(sample)
                    split = "training" if day < VALIDATION_START else "validation"
                    observation_caps[split] += truth["observation_cap_selected"]
                label_history[provider].append(row)
                if complete is not None:
                    completed_history[provider].append(complete)
    for group in GROUPS:
        labels, histories = copy.deepcopy(label_history), copy.deepcopy(completed_history)
        for day in range(TEST_START, END_DAY):
            for slot in range(4):
                at, index = timestamp(day, slot), (day - TEST_START) * 4 + slot
                cohort = (list(range(8, 12)) if group == "unseen_provider"
                          else sorted((index + offset) % 8 for offset in range(4)))
                tasks = task_specs(index)
                devices = [device_observation(p, at, labels[p], histories[p]) for p in cohort]
                actual_devices, forecast_labels = [], []
                for provider in range(12):
                    row, complete, truth = probe(provider, day, slot, group)
                    if provider in cohort:
                        rng = random_stream("future-calendar", group, provider, day, slot)
                        categories = [truth["label_bin"], *[
                            draw_bin(truth["probabilities"], rng) for _ in range(CONFIG["calendar_windows"] - 1)]]
                        windows = [model.WINDOW_BINS_SECONDS[value] for value in categories]
                        actual_devices.append({"device_id": f"provider-{provider:02}", "windows": windows,
                            "notice_seconds": CONFIG["notice_seconds"], "stop_seconds": CONFIG["stop_seconds"],
                            "owner_use_seconds": CONFIG["owner_use_seconds"]})
                        forecast_labels.append(row)
                    labels[provider].append(row)
                    if complete is not None:
                        histories[provider].append(complete)
                actual_tasks = [{"task_id": task["task_id"], "compute_seconds": task["estimated_compute_seconds"],
                                 "checkpoint_seconds": task["checkpoint_seconds"], "checkpoint_fails": False}
                                for task in tasks]
                cases.append({"group": group, "index": index, "day": day, "slot": slot,
                    "observed": {"at": at, "day": day, "slot": slot, "tasks": tasks, "devices": devices},
                    "actual": {"tasks": actual_tasks, "devices": actual_devices, "forecast_labels": forecast_labels}})
    data = {"config": CONFIG, "training": training, "validation": validation, "cases": cases,
            "observation_cap_selected": observation_caps}
    validate_inputs(data)
    return data


def validate_inputs(data: dict) -> None:
    """Fail before fitting if split, availability or fixed-cohort assumptions drift."""
    if data["config"] != CONFIG or len(data["training"]) != 896 or len(data["validation"]) != 224:
        raise ValueError("The fixed longitudinal training/validation design changed")
    for split, start, stop in (("training", TRAIN_START, VALIDATION_START),
                              ("validation", VALIDATION_START, TEST_START)):
        for row in data[split]:
            if not (start * 86400 <= row["observed_at"] < row["label_observed_at"] < stop * 86400):
                raise ValueError("A training/validation label crosses its temporal boundary")
            if row["provider_id"] not in {f"provider-{i:02}" for i in range(8)}:
                raise ValueError("Unseen providers cannot enter training or validation")
            model.feature_vector(row["features"])
    expected = {(group, index) for group in GROUPS for index in range(16)}
    if len(data["cases"]) != 64 or {(case["group"], case["index"]) for case in data["cases"]} != expected:
        raise ValueError("Exactly 64 fixed paired test cases are required")
    for case in data["cases"]:
        observed, actual = case["observed"], case["actual"]
        if len(observed["tasks"]) != 4 or len(observed["devices"]) != 4:
            raise ValueError("Every arm must receive the same four-task/four-device cohort")
        for device, calendar, label in zip(observed["devices"], actual["devices"], actual["forecast_labels"], strict=True):
            if device["device_id"] != calendar["device_id"] or device["device_id"] != label["provider_id"]:
                raise ValueError("Observed devices and hidden calendars must retain order")
            if any(row["label_observed_at"] > observed["at"] for row in device["labels"]):
                raise ValueError("A future label leaked into device history")
            extracted = model.historical_features(observed["at"], device["completed_history"])
            if extracted != device["features"]:
                raise ValueError("Features must derive only from the saved causal completed history")
            coverage = sum(calendar["windows"]) + len(calendar["windows"]) * (
                calendar["notice_seconds"] + calendar["owner_use_seconds"])
            if coverage < CONFIG["horizon_seconds"]:
                raise ValueError("Hidden calendar cannot cover the full fixed horizon")


def empirical_distribution(labels: list[dict]) -> tuple[float, ...]:
    """Fixed categorical tail EM: completed and censored observations both count."""
    if not labels:
        raise ValueError("Empirical prediction requires observable history")
    allowed = []
    for label in labels:
        if "label_bin" in label:
            allowed.append([label["label_bin"]])
        else:
            support = [i for i, duration in enumerate(model.WINDOW_BINS_SECONDS)
                       if duration > label["censored_after_seconds"]]
            if not support:
                raise ValueError("Censoring has no support in the fixed duration categories")
            allowed.append(support)
    probabilities = [1 / 8] * 8
    for _ in range(CONFIG["empirical_iterations"]):
        counts = [CONFIG["empirical_pseudocount"]] * 8
        for support in allowed:
            mass = sum(probabilities[i] for i in support)
            for i in support:
                counts[i] += probabilities[i] / mass
        total = sum(counts)
        probabilities = [value / total for value in counts]
    return tuple(probabilities)


def forecast(name: str, observed: dict, predictor=None) -> tuple[tuple[float, ...], ...]:
    """Only the observable dictionary crosses the prediction boundary."""
    if name not in FORECASTERS:
        raise ValueError("Unknown fixed forecasting arm")
    if name == "neural":
        if predictor is None or predictor.trained_through > observed["at"]:
            raise ValueError("A pinned model trained before the decision is required")
        return tuple(predictor.predict(device["features"]) for device in observed["devices"])
    histories = [visible_labels(device["labels"], observed["at"]) for device in observed["devices"]]
    if name == "pooled":
        pooled = empirical_distribution([label for labels in histories for label in labels[-32:]])
        return tuple(pooled for _ in histories)
    distributions = []
    weekday_class = "weekend" if observed["day"] % 7 >= 5 else "weekday"
    for labels in histories:
        selected = labels[-32:]
        if name == "timebucket":
            bucket = [label for label in labels if label["slot"] == observed["slot"]
                      and label["weekday_class"] == weekday_class][-32:]
            if len(bucket) >= CONFIG["minimum_history"]:
                selected = bucket
        distributions.append(empirical_distribution(selected))
    return tuple(distributions)


def matrix_from_forecasts(observed: dict, probabilities) -> tuple[tuple[float, ...], ...]:
    devices = [{"windows": learned.probability_windows(masses, model.WINDOW_BINS_SECONDS),
                "notice_seconds": device["notice_seconds"], "owner_use_seconds": device["owner_use_seconds"]}
               for device, masses in zip(observed["devices"], probabilities, strict=True)]
    return renewal.budgeted_cost_matrix(observed["tasks"], devices)


def assign(matrix, method: str) -> tuple[int, ...]:
    if method == "batch":
        selected = reliability.batch_assign(matrix)
        if selected is None:
            raise ValueError("Fixed admitted cohort has no full injective assignment")
        return selected
    if method != "greedy":
        raise ValueError("Unknown fixed assignment method")
    available, selected = set(range(len(matrix[0]))), []
    for row in matrix:
        chosen = min(available, key=lambda j: (row[j], j))
        selected.append(chosen)
        available.remove(chosen)
    return tuple(selected)


def execute_assignment(case: dict, policy: str, assignment, matrix) -> dict:
    observed, actual = case["observed"], case["actual"]
    jobs = [executor.execute_task(task, observed["devices"][j], actual["tasks"][i], actual["devices"][j],
            horizon=CONFIG["horizon_seconds"], allow_cold_restart=True, max_recoveries=CONFIG["max_recoveries"])
            for i, (task, j) in enumerate(zip(observed["tasks"], assignment, strict=True))]
    return {"group": case["group"], "index": case["index"], "day": case["day"], "policy": policy,
        "assignment": list(assignment), "predicted_matrix": matrix,
        "predicted_total_seconds": sum(matrix[i][j] for i, j in enumerate(assignment)), "jobs": jobs,
        "admitted": len(jobs), "completed": sum(job["completed"] for job in jobs),
        "censored": sum(job["censored"] for job in jobs),
        "budget_exhausted": sum(job["budget_exhausted"] for job in jobs),
        "restricted_mean_completion_seconds": mean(job["restricted_completion_seconds"] for job in jobs),
        **{name: sum(job[name] for job in jobs) for name in (
            "compute_waste_seconds", "checkpoint_seconds", "restart_seconds", "owner_wait_seconds", "deadline_misses")},
        "max_return_lateness_seconds": max(job["max_return_lateness_seconds"] for job in jobs)}


def score_forecast(probabilities, label) -> dict:
    exact = "label_bin" in label
    allowed = ([label["label_bin"]] if exact else [i for i, duration in enumerate(model.WINDOW_BINS_SECONDS)
                                                if duration > label["censored_after_seconds"]])
    likelihood = sum(probabilities[i] for i in allowed)
    return {"negative_log_likelihood": -math.log(max(likelihood, 1e-300)), "censored": not exact,
            "exact_label_brier": (sum((p - (i == label["label_bin"])) ** 2
                                      for i, p in enumerate(probabilities)) if exact else None)}


def summarize(rows: list[dict]) -> dict:
    tables, comparisons = [], []
    for group in ("all_groups", *GROUPS):
        selected = [row for row in rows if group == "all_groups" or row["group"] == group]
        for policy in POLICIES:
            chosen = [row for row in selected if row["policy"] == policy]
            tables.append({"group": group, "policy": policy, "cases": len(chosen),
                "mean_restricted_completion_seconds": mean(row["restricted_mean_completion_seconds"] for row in chosen),
                **{name + "_total": sum(row[name] for row in chosen) for name in (
                    "admitted", "completed", "censored", "budget_exhausted", "compute_waste_seconds",
                    "checkpoint_seconds", "restart_seconds", "owner_wait_seconds", "deadline_misses")},
                "max_return_lateness_seconds": max(row["max_return_lateness_seconds"] for row in chosen)})
        target = {(row["group"], row["index"]): row for row in selected if row["policy"] == "neural_batch"}
        for baseline in POLICIES[:-1]:
            pairs = [(row, target[row["group"], row["index"]]) for row in selected if row["policy"] == baseline]
            deltas = [{"group": old["group"], "index": old["index"], "day": old["day"],
                       "restricted_completion_reduction_seconds": old["restricted_mean_completion_seconds"]
                           - new["restricted_mean_completion_seconds"],
                       "completed_difference": new["completed"] - old["completed"]} for old, new in pairs]
            harmed = [{"group": old["group"], "index": old["index"], "task_id": oj["task_id"],
                       "slowdown_seconds": nj["restricted_completion_seconds"] - oj["restricted_completion_seconds"],
                       "baseline_completed": oj["completed"], "neural_completed": nj["completed"]}
                      for old, new in pairs for oj, nj in zip(old["jobs"], new["jobs"], strict=True)
                      if nj["restricted_completion_seconds"] > oj["restricted_completion_seconds"] + 1e-8]
            comparisons.append({"group": group, "baseline": baseline, "target": "neural_batch",
                "paired_mean_restricted_completion_reduction_seconds": mean(d["restricted_completion_reduction_seconds"] for d in deltas),
                "completed_difference": sum(d["completed_difference"] for d in deltas),
                "harmed_jobs": harmed, "harmed_job_count": len(harmed),
                "worst_slowdown_seconds": max((job["slowdown_seconds"] for job in harmed), default=0.),
                "paired_case_deltas": deltas,
                "chronological_day_deltas": [{"day": day,
                    "mean_restricted_completion_reduction_seconds": mean(d["restricted_completion_reduction_seconds"] for d in deltas if d["day"] == day),
                    "completed_difference": sum(d["completed_difference"] for d in deltas if d["day"] == day)}
                    for day in range(TEST_START, END_DAY)]})
    return {"tables": tables, "comparisons": comparisons,
            "uncertainty": "Descriptive synthetic four-day paired results; no IID confidence interval or field claim."}


def diagnostic_summary(rows: list[dict]) -> list[dict]:
    result = []
    for split, forecaster in sorted({(row["split"], row["forecaster"]) for row in rows}):
        selected = [row for row in rows if row["split"] == split and row["forecaster"] == forecaster]
        exact = [row["exact_label_brier"] for row in selected if row["exact_label_brier"] is not None]
        result.append({"split": split, "forecaster": forecaster, "observations": len(selected),
            "censored_observations": sum(row["censored"] for row in selected),
            "mean_negative_log_likelihood": mean(row["negative_log_likelihood"] for row in selected),
            "exact_observations": len(exact), "mean_exact_label_brier": mean(exact) if exact else None})
    return result


def sources() -> dict[str, Path]:
    return {"protocol.md": PROTOCOL, "runner.py": Path(__file__), "model.py": Path(model.__file__),
            "adapter.py": Path(learned.__file__), "renewal.py": Path(renewal.__file__),
            "matching.py": Path(reliability.__file__), "executor.py": Path(executor.__file__)}


def freeze(output: Path) -> None:
    """No model training or measured outcomes may occur in this stage."""
    if output.exists():
        raise FileExistsError("Never reuse an existing research evidence directory")
    inputs = generate_inputs()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "inputs.json", inputs)
    for name, path in sources().items():
        (output / name).write_bytes(path.read_bytes())
    write_json(output / "freeze.json", {"schema_version": 1, "status": "FROZEN_NOT_RUN",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(), "config": CONFIG,
        "groups": GROUPS, "policies": POLICIES, "training": model.TRAINING,
        "sha256": {name: sha256(output / name) for name in ("inputs.json", *sources())}})


def verify_frozen(output: Path) -> tuple[dict, dict]:
    manifest = json.loads((output / "freeze.json").read_text())
    if (manifest.get("config") != CONFIG or manifest.get("groups") != list(GROUPS)
            or manifest.get("policies") != list(POLICIES) or manifest.get("training") != model.TRAINING):
        raise ValueError("Frozen design differs from the fixed source protocol")
    expected_names = {"inputs.json", *sources()}
    if set(manifest["sha256"]) != expected_names:
        raise ValueError("Freeze must pin all research sources and inputs")
    for name, expected in manifest["sha256"].items():
        if sha256(output / name) != expected:
            raise ValueError(f"Frozen artifact changed: {name}")
    for name, path in sources().items():
        if sha256(path) != manifest["sha256"][name]:
            raise ValueError(f"Source changed after freeze: {name}")
    inputs = json.loads((output / "inputs.json").read_text())
    validate_inputs(inputs)
    return manifest, inputs


def run_frozen(output: Path) -> dict:
    """Explicit research execution only, once after a checked prospective freeze."""
    if (output / "RUN_STARTED").exists() or (output / "report.json").exists():
        raise FileExistsError("Never overwrite or rerun sealed comparative evidence")
    manifest, data = verify_frozen(output)
    with (output / "RUN_STARTED").open("x") as marker:
        marker.write(datetime.now(timezone.utc).isoformat() + "\n")
    artifact = model.train_model(data["training"])
    write_json(output / "model.json", artifact)
    artifact_hash = sha256(output / "model.json")
    predictor = model.load_predictor(output / "model.json", expected_sha256=artifact_hash)
    if predictor.trained_through >= VALIDATION_START * 86400:
        raise ValueError("Model cutoff crossed the predeclared validation boundary")
    diagnostics = [{"split": "validation", "forecaster": "neural", "provider_id": row["provider_id"],
                    "observed_at": row["observed_at"],
                    **score_forecast(predictor.predict(row["features"]), row)} for row in data["validation"]]
    runs = []
    for case in data["cases"]:
        matrices = {}
        for name in FORECASTERS:
            probabilities = forecast(name, case["observed"], predictor)
            matrices[name] = matrix_from_forecasts(case["observed"], probabilities)
            for device, masses, label in zip(case["observed"]["devices"], probabilities,
                                            case["actual"]["forecast_labels"], strict=True):
                diagnostics.append({"split": case["group"], "forecaster": name, "index": case["index"],
                    "provider_id": device["device_id"], "probabilities": masses, **score_forecast(masses, label)})
        for policy in POLICIES:
            name, method = policy.rsplit("_", 1)
            matrix = matrices[name]
            runs.append(execute_assignment(case, policy, assign(matrix, method), matrix))
    report = {"schema_version": 1, "model_only": True, "freeze": manifest, "model_sha256": artifact_hash,
        "model_training_cutoff": predictor.trained_through, "raw_runs": runs, "summary": summarize(runs),
        "forecast_diagnostics": diagnostics, "forecast_summary": diagnostic_summary(diagnostics),
        "training_rows": len(data["training"]), "validation_rows": len(data["validation"]),
        "training_censored_rows": sum("censored_after_seconds" in row for row in data["training"]),
        "validation_censored_rows": sum("censored_after_seconds" in row for row in data["validation"]),
        "observation_cap_selected": data["observation_cap_selected"],
        "limitations": [
            "Synthetic longitudinal probes, no real contributor trace or real GPU measurement.",
            "Third study chosen after two previous studies; no independent field confirmation.",
            "One fixed trained model; validation is diagnostic, not model selection.",
            "Unseen identities have legal completed history; zero-history cold start is not measured.",
            "Already-aged windows, queue admission, migration, hardware speeds and runtime-estimate errors are excluded.",
            "CDF quantization and stationary-within-probe renewal assumptions may misestimate risk.",
            "Historical helper matching ties differ from wrapper B2-preserving ties; not a live policy score.",
            "Restricted 900-second endpoints retain unfinished jobs; they are not successful completion times.",
            "Exact-label Brier is conditioned on uncensored observations and may have selection bias.",
            "Four chronological days per group; descriptive paired differences do not establish population uncertainty.",
        ]}
    write_json(output / "report.json", report)
    write_json(output / "result-manifest.json", {"report_sha256": sha256(output / "report.json"),
        "model_sha256": artifact_hash, "freeze_sha256": sha256(output / "freeze.json")})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--freeze-only", action="store_true")
    group.add_argument("--run-frozen", action="store_true")
    args = parser.parse_args()
    if args.freeze_only:
        freeze(args.output)
        print(json.dumps({"status": "FROZEN_NOT_RUN", "output": str(args.output)}))
    else:
        result = run_frozen(args.output)
        print(json.dumps({"status": "COMPLETED_SYNTHETIC_ONLY", "cases": len(result["raw_runs"]),
                          "output": str(args.output)}))


if __name__ == "__main__":
    main()
