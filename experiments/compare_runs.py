"""Compare complete recorded work cohorts without dropping failed or unfinished work.

Usage: python -m experiments.compare_runs baseline.json candidate.json --output comparison.json
This calculator checks declared pairing, not the authenticity of upstream execution.
It never produces a competition acceptance receipt or a simulated execution trace.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return float(value)


def validate_run(run: dict) -> dict[str, dict]:
    """Require the same declared cohort including failed allocation/job submissions."""
    for field in ("run_id", "policy", "scenario_family", "input_sha256", "model_version"):
        if not isinstance(run.get(field), str) or not run[field].strip():
            raise ValueError(f"{field} is required")
    digest = run["input_sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
    if type(run.get("seed")) is not int or run["seed"] < 0:
        raise ValueError("seed must be a nonnegative integer")
    horizon = number(run.get("horizon"), "horizon")
    if run.get("cancellation_policy") != "count_until_horizon":
        raise ValueError("Declare cancellation_policy=count_until_horizon for calculator v1")
    if not isinstance(run.get("works"), list):
        raise ValueError("works must include the full input cohort as a list")
    records: dict[str, dict] = {}
    for item in run["works"]:
        if not isinstance(item, dict):
            raise ValueError("each work must be an object")
        identity = item.get("intent_id")
        if not isinstance(identity, str) or not identity or identity in records:
            raise ValueError("each work must have a unique nonempty intent_id")
        if not isinstance(item.get("owner_id"), str) or not item["owner_id"]:
            raise ValueError(f"{identity}: owner_id is required")
        arrival = number(item.get("arrived_at"), f"{identity}.arrived_at")
        state = item.get("status")
        if state not in {"completed", "failed", "unfinished", "cancelled"}:
            raise ValueError(f"{identity}: invalid outcome status")
        completion = item.get("completed_at")
        if state == "completed":
            completion = number(completion, f"{identity}.completed_at")
            if completion < arrival:
                raise ValueError(f"{identity}: completion precedes external arrival")
        elif completion is not None:
            raise ValueError(f"{identity}: only completed work can have completed_at")
        cancellation = item.get("cancelled_at")
        if state == "cancelled":
            cancellation = number(cancellation, f"{identity}.cancelled_at")
            if not arrival <= cancellation <= horizon:
                raise ValueError(f"{identity}: cancellation must be observed inside the window")
        elif cancellation is not None:
            raise ValueError(f"{identity}: cancelled_at requires cancelled status")
        number(item.get("effective_service"), f"{identity}.effective_service")
        records[identity] = item
    if not isinstance(run.get("service_unit"), str) or not run["service_unit"]:
        raise ValueError("service_unit must declare the model's common normalized service unit")
    return records


def time_to_window_end(item: dict, horizon: float) -> float:
    completion = item["completed_at"] if item["status"] == "completed" else horizon
    return max(0.0, min(completion, horizon) - item["arrived_at"])


def summarize(records: dict[str, dict], horizon: float) -> dict:
    cohort = [item for item in records.values() if item["arrived_at"] <= horizon]
    completed = sum(
        item["status"] == "completed" and item["completed_at"] <= horizon for item in cohort
    )
    return {
        "arrived": len(cohort),
        "completed": completed,
        "failed": sum(item["status"] == "failed" for item in cohort),
        "cancelled": sum(item["status"] == "cancelled" for item in cohort),
        "not_completed_at_horizon": len(cohort) - completed,
        "unfulfilled_demand_area": sum(time_to_window_end(item, horizon) for item in cohort),
    }


def compare_runs(baseline: dict, candidate: dict) -> dict:
    base = validate_run(baseline)
    other = validate_run(candidate)
    for key in (
        "scenario_family", "seed", "input_sha256", "model_version", "horizon",
        "service_unit", "cancellation_policy",
    ):
        if baseline[key] != candidate[key]:
            raise ValueError(f"paired runs disagree on {key}")
    if base.keys() != other.keys():
        raise ValueError("paired runs must contain the same full work cohort")
    horizon = float(baseline["horizon"])
    per_work = []
    owners: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for identity in sorted(base):
        left, right = base[identity], other[identity]
        for key in ("owner_id", "arrived_at", "cancelled_at"):
            if left.get(key) != right.get(key):
                raise ValueError(f"{identity}: paired runs disagree on {key}")
        if left["arrived_at"] > horizon:
            continue
        delta = time_to_window_end(right, horizon) - time_to_window_end(left, horizon)
        per_work.append({
            "intent_id": identity, "owner_id": left["owner_id"],
            "baseline_status": left["status"], "candidate_status": right["status"],
            "delta_truncated_completion_seconds": delta,
            "severe_harm_gt_300_seconds": delta > 300.0,
        })
        owners[left["owner_id"]][0] += left["effective_service"]
        owners[left["owner_id"]][1] += right["effective_service"]
    b = summarize(base, horizon)
    c = summarize(other, horizon)
    area_b = b["unfulfilled_demand_area"]
    area_c = c["unfulfilled_demand_area"]
    per_owner = [
        {
            "owner_id": owner, "baseline_service": service[0], "candidate_service": service[1],
            "service_unit": baseline["service_unit"],
            "service_ratio": service[1] / service[0] if service[0] else None,
            "absolute_service_change": service[1] - service[0],
            "severe_harm_below_80_percent": service[0] > 0 and service[1] < .8 * service[0],
        }
        for owner, service in sorted(owners.items())
    ]
    return {
        "calculator": "work-cohort-v1",
        "scope": "offline_calculation_of_supplied_records_not_runtime_verification",
        "baseline_run_id": baseline["run_id"], "candidate_run_id": candidate["run_id"],
        "scenario_family": baseline["scenario_family"], "seed": baseline["seed"],
        "input_sha256": baseline["input_sha256"], "horizon": horizon,
        "baseline": b, "candidate": c,
        "area_ratio": area_c / area_b if area_b else None,
        "area_absolute_change": area_c - area_b,
        "zero_baseline_regression": area_b == 0 and area_c > 0,
        "completion_count_not_lower": c["completed"] >= b["completed"],
        "per_work": per_work, "per_owner": per_owner,
        "thresholds": {"work_harm_seconds": 300, "owner_service_fraction": .8},
    }


def aggregate_pairs(pairs: list[dict]) -> dict:
    """Equal weight for scenario families; never silently omit zero denominators."""
    families: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for pair in pairs:
        identity = (pair["scenario_family"], pair["seed"])
        if identity in seen:
            raise ValueError("duplicate scenario family/seed in aggregation")
        seen.add(identity)
        families[pair["scenario_family"]].append(pair)
    if not pairs:
        raise ValueError("no paired runs supplied")
    rows = []
    for family, results in sorted(families.items()):
        ratios = [p["area_ratio"] for p in results]
        defined = all(r is not None for r in ratios)
        rows.append({
            "scenario_family": family, "pair_count": len(results),
            "mean_area_ratio": sum(ratios) / len(ratios) if defined else None,
            "worst_area_ratio": max(ratios) if defined else None,
            "zero_baseline_runs": [p["seed"] for p in results if p["area_ratio"] is None],
            "absolute_area_changes": [p["area_absolute_change"] for p in results],
        })
    means = [row["mean_area_ratio"] for row in rows]
    return {
        "families": rows,
        "macro_area_ratio": sum(means) / len(means) if all(m is not None for m in means) else None,
        "note": "Undefined zero-denominator ratios are reported, not dropped or called a win.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = compare_runs(
            json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text())
        )
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
