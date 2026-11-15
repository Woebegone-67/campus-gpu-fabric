"""Exhaustive hindsight diagnostics for the sealed four-device synthetic study.

These are separate future-aware upper bounds, never deployable baselines. Each
objective has its own assignment; no composite 'oracle policy' joins their wins.
The same frozen outcome executor is used, without predicted-cost shortcuts.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from campus_fabric.policies import reliability

OBJECTIVES = ("waste_and_overhead_seconds", "mean_completion_seconds", "makespan_seconds")
COMPARATORS = ("edge_greedy", "batch")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen(root: Path):
    freeze = json.loads((root / "freeze.json").read_text())
    manifest = json.loads((root / "result-manifest.json").read_text())
    for name, expected in freeze["sha256"].items():
        if digest(root / name) != expected:
            raise ValueError(f"Frozen input/source has changed: {name}")
    for name in ("report", "freeze"):
        if digest(root / f"{name}.json") != manifest[f"{name}_sha256"]:
            raise ValueError(f"Frozen {name} manifest does not match")
    if digest(Path(reliability.__file__)) != freeze["sha256"]["planner.py"]:
        raise ValueError("Live helper differs from the frozen helper")
    spec = importlib.util.spec_from_file_location("sealed_placement_outcomes", root / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = json.loads((root / "inputs.json").read_text())["cases"]
    report = json.loads((root / "report.json").read_text())
    return module, cases, report, freeze, manifest


def replay(module, case: dict, assignment: tuple[int, ...], *, strict=False) -> dict:
    outcomes = [module.execute_task(
        task, case["observed"]["devices"][device_index], case["actual"]["tasks"][index],
        case["actual"]["devices"][device_index], allow_cold_restart=not strict,
    ) for index, (task, device_index) in enumerate(zip(
        case["observed"]["tasks"], assignment, strict=True))]
    return {
        "assignment": list(assignment),
        "waste_and_overhead_seconds": sum(
            row["compute_waste_seconds"] + row["overhead_seconds"] for row in outcomes),
        "mean_completion_seconds": mean(row["truncated_completion_seconds"] for row in outcomes),
        "makespan_seconds": max(row["truncated_completion_seconds"] for row in outcomes),
        "completed": sum(row["completed"] for row in outcomes),
        "failed": sum(not row["completed"] for row in outcomes),
        "owner_wait_seconds": sum(row["owner_wait_seconds"] for row in outcomes),
        "deadline_misses": sum(row["return_lateness_seconds"] > 1e-8 for row in outcomes),
        "completed_useful_compute_seconds": sum(
            case["actual"]["tasks"][index]["compute_seconds"]
            for index, row in enumerate(outcomes) if row["completed"]),
    }


def verify_existing(module, case: dict, stored: dict, *, strict=False) -> None:
    reproduced = replay(module, case, tuple(stored["assignment"]), strict=strict)
    for key, value in reproduced.items():
        if key == "assignment":
            assert value == stored[key]
        else:
            # Stored run_case sums waste and overhead separately; floating point
            # associativity may differ from per-job summation by a few ulps.
            assert abs(value - stored[key]) < 1e-8, (case["regime"], case["seed"], key)


def analyze(frozen: Path) -> dict:
    module, cases, original, freeze, manifest = load_frozen(frozen)
    indexed = {
        label: {(r["regime"], r["seed"], r["policy"]): r for r in original[label]}
        for label in ("main_runs", "strict_controls")
    }
    rows = []
    verified = 0
    for case in cases:
        for label in indexed:
            for policy in module.POLICIES:
                verify_existing(module, case, indexed[label][(case["regime"], case["seed"], policy)],
                                strict=label == "strict_controls")
                verified += 1
        permutations = list(itertools.permutations(range(4)))
        main = [replay(module, case, assignment) for assignment in permutations]
        strict = [replay(module, case, assignment, strict=True) for assignment in permutations]
        oracles = {objective: min(main, key=lambda value: (value[objective], value["assignment"]))
                   for objective in OBJECTIVES}
        max_completed = min(strict, key=lambda value: (-value["completed"], value["assignment"]))
        baselines = {policy: {key: indexed["main_runs"][(case["regime"], case["seed"], policy)][key]
                             for key in main[0]} for policy in COMPARATORS}
        strict_baselines = {
            policy: {key: indexed["strict_controls"][(case["regime"], case["seed"], policy)][key]
                     for key in strict[0]} for policy in module.POLICIES
        }
        for objective, oracle in oracles.items():
            assert all(oracle[objective] <= value[objective] + 1e-8 for value in baselines.values())
        assert all(max_completed["completed"] >= value["completed"] for value in strict_baselines.values())
        rows.append({"regime": case["regime"], "seed": case["seed"], "assignments_enumerated": 24,
                     "total_useful_actual_compute_seconds": sum(
                         task["compute_seconds"] for task in case["actual"]["tasks"]),
                     "separate_future_aware_oracles": oracles, "observed_policies": baselines,
                     "strict_max_completed_oracle": max_completed,
                     "strict_observed_policies": strict_baselines})
    summaries = []
    for group in ["all_regimes", *module.REGIMES]:
        selected = [row for row in rows if group == "all_regimes" or row["regime"] == group]
        useful = sum(row["total_useful_actual_compute_seconds"] for row in selected)
        objective_summaries = []
        for objective in OBJECTIVES:
            oracle_mean = mean(row["separate_future_aware_oracles"][objective][objective] for row in selected)
            comparisons = {}
            for policy in COMPARATORS:
                baseline_mean = mean(row["observed_policies"][policy][objective] for row in selected)
                maximum_saving = sum(row["observed_policies"][policy][objective]
                                     - row["separate_future_aware_oracles"][objective][objective]
                                     for row in selected)
                comparisons[policy] = {
                    "observed_mean_seconds": baseline_mean,
                    "maximum_mean_reduction_seconds": baseline_mean - oracle_mean,
                    "maximum_relative_reduction": (baseline_mean - oracle_mean) / baseline_mean
                    if baseline_mean else None,
                    "maximum_summed_reduction_seconds": maximum_saving,
                    "maximum_compute_overhead_saving_divided_by_total_useful_actual_compute":
                    maximum_saving / useful if objective == "waste_and_overhead_seconds" else None,
                }
            objective_summaries.append({"objective": objective, "separate_oracle_mean_seconds": oracle_mean,
                                        "comparisons": comparisons})
        actual_saving = sum(row["observed_policies"]["edge_greedy"]["waste_and_overhead_seconds"]
                            - row["observed_policies"]["batch"]["waste_and_overhead_seconds"]
                            for row in selected)
        count_oracle = sum(row["strict_max_completed_oracle"]["completed"] for row in selected)
        strict_counts = {policy: {
            "completed": sum(row["strict_observed_policies"][policy]["completed"] for row in selected),
            "failed": sum(row["strict_observed_policies"][policy]["failed"] for row in selected),
        } for policy in module.POLICIES}
        summaries.append({"group": group, "cases": len(selected), "admitted_jobs": 4 * len(selected),
                          "total_useful_actual_compute_seconds": useful,
                          "batch_vs_greedy_actual_overhead_saved_seconds": actual_saving,
                          "batch_vs_greedy_actual_overhead_saved_fraction_of_useful_compute": actual_saving / useful,
                          "separate_objective_oracles": objective_summaries,
                          "strict_max_completed_oracle_count": count_oracle,
                          "strict_minimum_failed_count": 4 * len(selected) - count_oracle,
                          "strict_observed_policy_counts": strict_counts})
    return {
        "schema_version": 1, "analysis": "post_hoc_headroom_only_not_a_new_comparative_trial",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_script_sha256": digest(Path(__file__)), "frozen_source": str(frozen.resolve()),
        "source_result_manifest": manifest, "source_freeze": freeze,
        "prior_rows_replayed_and_verified": verified,
        "assignment_outcomes_evaluated": len(cases) * 24 * 2,
        "summaries": summaries, "per_case": rows,
        "limitations": [
            "All oracles see hidden future windows and actual runtime: they are impossible online policies.",
            "Each objective chooses a separate assignment; their wins cannot be combined into a single policy.",
            "Bounds are exact only over 24 initial assignments and the frozen common recall responder.",
            "They do not bound other workloads, queues, multiple recalls or new recovery mechanisms.",
            "Useful compute denominator is fixed actual task demand, not measured GPU throughput.",
            "Strict max-completed oracle optimizes task count, not useful compute or completion time.",
            "Failures retain the frozen 400-second reporting horizon; it is not an observed completion.",
            "Original negative regimes and every input remain unchanged; this is an after-outcome diagnostic.",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Do not overwrite prior headroom evidence")
    result = analyze(args.frozen)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "sha256": digest(args.output),
                      "verified_prior_rows": result["prior_rows_replayed_and_verified"],
                      "assignment_outcomes": result["assignment_outcomes_evaluated"]}))


if __name__ == "__main__":
    main()
