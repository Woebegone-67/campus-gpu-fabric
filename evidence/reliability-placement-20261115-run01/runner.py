"""Frozen, paired *synthetic* placement study; no GPU or product mutations.

The online assignment sees only declarations and past completed availability
windows. An independent RNG generates held-out future windows and runtimes.
All methods admit the same cohort and use the same causal recall responder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from campus_fabric.policies import reliability

ROOT = Path(__file__).resolve().parents[2]
REGIMES = (
    "baseline_controls", "predictable_mixed", "homogeneous_devices",
    "homogeneous_tasks", "uninformative_history", "reversed_history",
    "runtime_estimate_error", "short_notice", "mixed_checkpoint",
)
POLICIES = ("id_order", "risk_fcfs", "duration_sort", "loss_sort", "edge_greedy", "batch")
GENERATOR = {
    "version": "reliability-paired-v1", "tasks": 4, "devices": 4,
    "declared_compute_seconds": [16., 38., 72., 110.],
    "checkpoint_seconds": [.1, 1., 4., 8.],
    "device_window_means": [10., 30., 60., 140.],
    "history_count": 16, "uniform_factor": [.75, 1.25],
    "normal_notice_seconds": 12., "short_notice_seconds": 2.,
    "restart_seconds": .5, "stop_seconds": .1, "report_margin_seconds": .1,
    "owner_use_seconds": 10., "failure_completion_horizon_seconds": 400.,
    "runtime_error_factor": [.5, 2.], "bootstrap_resamples": 2000,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def generate_case(seed: int, regime: str) -> dict:
    if regime not in REGIMES:
        raise ValueError(f"Unknown regime: {regime}")
    durations = list(GENERATOR["declared_compute_seconds"])
    costs = list(GENERATOR["checkpoint_seconds"])
    random.Random(f"duration-order:{seed}").shuffle(durations)
    random.Random(f"checkpoint-order:{seed}").shuffle(costs)
    if regime == "homogeneous_tasks":
        durations, costs = [60.] * 4, [2.] * 4
    means = list(GENERATOR["device_window_means"])
    random.Random(f"device-order:{seed}").shuffle(means)
    history_means, future_means = list(means), list(means)
    if regime == "baseline_controls":
        history_means = future_means = [300.] * 4
    elif regime == "homogeneous_devices":
        history_means = future_means = [45.] * 4
    elif regime == "uninformative_history":
        history_means = [45.] * 4
    elif regime == "reversed_history":
        inverse = dict(zip(sorted(means), reversed(sorted(means)), strict=True))
        future_means = [inverse[value] for value in means]
    tasks, devices, actual_tasks, actual_devices = [], [], [], []
    for i, (duration, cost) in enumerate(zip(durations, costs, strict=True)):
        tasks.append({"task_id": f"task-{i}", "estimated_compute_seconds": duration,
                      "checkpoint_seconds": cost, "restart_seconds": .5, "stop_seconds": .1})
        factor = random.Random(f"future-runtime:{seed}:{i}").uniform(.5, 2.)
        actual_tasks.append({"task_id": f"task-{i}", "compute_seconds": duration * factor
                             if regime == "runtime_estimate_error" else duration,
                             "checkpoint_seconds": cost, "checkpoint_fails": False})
    for i, (past_mean, future_mean) in enumerate(zip(history_means, future_means, strict=True)):
        history_rng = random.Random(f"past-windows:{seed}:{i}")
        windows = [past_mean * history_rng.uniform(.75, 1.25) for _ in range(16)]
        if regime == "baseline_controls":
            windows = [300.] * 16
        notice = 2. if regime == "short_notice" else 12.
        if regime == "mixed_checkpoint" and i % 2 == 0:
            notice = 2.
        devices.append({"device_id": f"device-{i}", "windows": windows,
                        "notice_seconds": notice})
        future = future_mean * random.Random(f"future-window:{seed}:{i}").uniform(.75, 1.25)
        actual_devices.append({"device_id": f"device-{i}", "window_seconds":
                               300. if regime == "baseline_controls" else future,
                               "notice_seconds": notice, "stop_seconds": .1})
    return {"seed": seed, "regime": regime,
            "observed": {"tasks": tasks, "devices": devices},
            "actual": {"tasks": actual_tasks, "devices": actual_devices}}


def select(policy: str, observed: dict) -> tuple[int, ...]:
    """This interface deliberately cannot access outcomes or future recalls."""
    tasks, devices = observed["tasks"], observed["devices"]
    if len(tasks) > len(devices):
        raise ValueError("Study admits a fixed cohort that fits the available devices")
    stable = sorted(range(len(devices)), key=lambda j: (-mean(devices[j]["windows"]), j))
    order = list(range(len(tasks)))
    if policy == "id_order":
        return tuple(order)
    if policy == "duration_sort":
        order.sort(key=lambda i: (-tasks[i]["estimated_compute_seconds"], i))
    elif policy == "loss_sort":
        matrix = reliability.cost_matrix(tasks, devices)
        order.sort(key=lambda i: (-max(matrix[i]), i))
    elif policy in {"edge_greedy", "batch"}:
        matrix = reliability.cost_matrix(tasks, devices)
        if policy == "batch":
            result = reliability.batch_assign(matrix)
            if result is None:
                raise ValueError("Fixed admitted cohort unexpectedly infeasible")
            return result
        available = set(range(len(devices)))
        result = []
        for row in matrix:
            selected = min(available, key=lambda j: (row[j], j))
            result.append(selected)
            available.remove(selected)
        return tuple(result)
    elif policy != "risk_fcfs":
        raise ValueError(f"Unknown policy: {policy}")
    result = [0] * len(tasks)
    for i, j in zip(order, stable, strict=False):
        result[i] = j
    return tuple(result)


def execute_task(task: dict, device: dict, actual_task: dict, actual_device: dict,
                 *, allow_cold_restart: bool = True) -> dict:
    """One actual recall, then stable continuation, common to every assignment.

    The decision uses declared runtime and checkpoint duration. Actual completion
    may occur before a planned action, but hidden runtime never selects an action.
    Saving blocks compute; a save completing on the stop boundary fails.
    """
    total = actual_task["compute_seconds"]
    window = actual_device["window_seconds"]
    # The actual deadline becomes public when this notification is delivered;
    # it was not known to the placement selector.
    notice = actual_device["notice_seconds"]
    reserve = task["stop_seconds"]
    stop = actual_device["stop_seconds"]
    checkpoint = actual_task["checkpoint_seconds"]
    restart = task["restart_seconds"]
    if total <= window:
        return {"task_id": task["task_id"], "device_id": device["device_id"],
                "action": "completed_before_notice", "completed": True,
                "completion_seconds": total, "truncated_completion_seconds": total,
                "compute_waste_seconds": 0., "checkpoint_seconds": 0.,
                "restart_seconds": 0., "overhead_seconds": 0., "owner_wait_seconds": 0.,
                "return_lateness_seconds": 0., "checkpoint_failed": False, "recalls": 0}
    stop_at = max(0., notice - reserve)
    latest = stop_at - task["checkpoint_seconds"] - GENERATOR["report_margin_seconds"]
    estimated_remaining = max(0., task["estimated_compute_seconds"] - window)
    if latest <= 0.:
        action, dispatch = "stop", 0.
    elif estimated_remaining <= latest:
        action, dispatch = "finish_or_save", latest
    else:
        action, dispatch = "save_now", 0.
    remaining = total - window
    if action != "stop" and remaining <= dispatch:
        completion = window + remaining
        wait = remaining + stop
        checkpoint_used = restart_used = waste = 0.
        failed = False
        completed = True
    else:
        checkpoint_used = 0. if action == "stop" else min(checkpoint, stop_at - dispatch)
        saved = (action != "stop" and not actual_task["checkpoint_fails"]
                 and dispatch + checkpoint < stop_at
                 and not math.isclose(dispatch + checkpoint, stop_at, abs_tol=1e-9))
        failed = action != "stop" and not saved
        progress = min(total, window + dispatch)
        retained = progress if saved else 0.
        waste = 0. if saved else progress
        wait = (0. if action == "stop" else dispatch + checkpoint_used) + stop
        completed = saved or allow_cold_restart
        restart_used = restart if completed else 0.
        completion = (window + max(notice + GENERATOR["owner_use_seconds"], wait)
                      + restart_used + total - retained) if completed else None
    return {"task_id": task["task_id"], "device_id": device["device_id"],
            "action": action, "completed": completed, "completion_seconds": completion,
            "truncated_completion_seconds": completion if completed else
            GENERATOR["failure_completion_horizon_seconds"],
            "compute_waste_seconds": waste, "checkpoint_seconds": checkpoint_used,
            "restart_seconds": restart_used, "overhead_seconds": checkpoint_used + restart_used,
            "owner_wait_seconds": wait, "return_lateness_seconds": max(0., wait - notice),
            "checkpoint_failed": failed, "recalls": 1}


def run_case(case: dict, policy: str, *, allow_cold_restart: bool = True) -> dict:
    assignment = select(policy, case["observed"])
    outcomes = [execute_task(task, case["observed"]["devices"][j],
                             case["actual"]["tasks"][i], case["actual"]["devices"][j],
                             allow_cold_restart=allow_cold_restart)
                for i, (task, j) in enumerate(zip(case["observed"]["tasks"], assignment, strict=True))]
    waste = sum(x["compute_waste_seconds"] for x in outcomes)
    overhead = sum(x["overhead_seconds"] for x in outcomes)
    return {"seed": case["seed"], "regime": case["regime"], "policy": policy,
            "allow_cold_restart": allow_cold_restart, "assignment": list(assignment),
            "completed": sum(x["completed"] for x in outcomes),
            "failed": sum(not x["completed"] for x in outcomes), "jobs": outcomes,
            "admitted": len(outcomes),
            "completed_useful_compute_seconds": sum(
                case["actual"]["tasks"][i]["compute_seconds"]
                for i, outcome in enumerate(outcomes) if outcome["completed"]),
            "compute_waste_seconds": waste, "overhead_seconds": overhead,
            "waste_and_overhead_seconds": waste + overhead,
            "checkpoint_seconds": sum(x["checkpoint_seconds"] for x in outcomes),
            "mean_completion_seconds": mean(x["truncated_completion_seconds"] for x in outcomes),
            "makespan_seconds": max(x["truncated_completion_seconds"] for x in outcomes),
            "owner_wait_seconds": sum(x["owner_wait_seconds"] for x in outcomes),
            "deadline_misses": sum(x["return_lateness_seconds"] > 1e-8 for x in outcomes),
            "checkpoint_failures": sum(x["checkpoint_failed"] for x in outcomes),
            "recalls": sum(x["recalls"] for x in outcomes)}


METRICS = ("waste_and_overhead_seconds", "compute_waste_seconds", "overhead_seconds",
           "checkpoint_seconds", "mean_completion_seconds", "makespan_seconds",
           "owner_wait_seconds", "deadline_misses", "checkpoint_failures", "completed",
           "failed", "admitted", "completed_useful_compute_seconds")


def paired_interval(values: list[float], resamples: int = 2000) -> list[float]:
    """Seed bootstrap describes only this synthetic generator, not campus demand."""
    rng = random.Random(20261115)
    boot = sorted(mean(rng.choices(values, k=len(values))) for _ in range(resamples))
    return [boot[int(.025 * (resamples - 1))], boot[int(.975 * (resamples - 1))]]


def summarize(rows: list[dict], regimes: list[str]) -> dict:
    tables, comparisons = [], []
    for group in ["all_regimes", *regimes]:
        subset = [r for r in rows if group == "all_regimes" or r["regime"] == group]
        for policy in POLICIES:
            selected = [r for r in subset if r["policy"] == policy]
            tables.append({"group": group, "policy": policy, "cases": len(selected),
                           **{metric: mean(r[metric] for r in selected) for metric in METRICS}})
        target = {(r["regime"], r["seed"]): r for r in subset if r["policy"] == "batch"}
        for baseline in POLICIES[:-1]:
            paired = [(r, target[(r["regime"], r["seed"])]) for r in subset
                      if r["policy"] == baseline]
            seeds = sorted({b["seed"] for b, _ in paired})
            deltas = [mean(b["waste_and_overhead_seconds"] - a["waste_and_overhead_seconds"]
                           for b, a in paired if b["seed"] == seed) for seed in seeds]
            harms = [{"seed": a["seed"], "regime": a["regime"], "task_id": aj["task_id"],
                      "completion_delta_seconds": aj["truncated_completion_seconds"]
                      - bj["truncated_completion_seconds"],
                      "waste_delta_seconds": aj["compute_waste_seconds"] - bj["compute_waste_seconds"],
                      "baseline_completed": bj["completed"], "batch_completed": aj["completed"]}
                     for b, a in paired for bj, aj in zip(b["jobs"], a["jobs"], strict=True)]
            baseline_mean = mean(b["waste_and_overhead_seconds"] for b, _ in paired)
            comparisons.append({"group": group, "baseline": baseline,
                                "paired_mean_waste_overhead_reduction_seconds": mean(deltas),
                                "relative_reduction": mean(deltas) / baseline_mean if baseline_mean else None,
                                "synthetic_seed_bootstrap_95_percent": paired_interval(deltas),
                                "completion_harmed_jobs": sum(h["completion_delta_seconds"] > 1e-8 for h in harms),
                                "completion_helped_jobs": sum(h["completion_delta_seconds"] < -1e-8 for h in harms),
                                "worst_completion_harm_seconds": max(h["completion_delta_seconds"] for h in harms),
                                "per_job_differences": harms})
    return {"tables": tables, "comparisons": comparisons}


def freeze(protocol_path: Path, output: Path) -> None:
    protocol = json.loads(protocol_path.read_text())
    if protocol["seeds"] != list(range(20)) or protocol["regimes"] != list(REGIMES):
        raise ValueError("This frozen generator requires exactly seeds 0..19 and all nine regimes")
    if protocol["generator"] != GENERATOR or protocol["policies"] != list(POLICIES):
        raise ValueError("Protocol does not match the fixed generator and six comparators")
    protocol_markdown = ROOT / protocol["protocol_markdown"]
    if sha256(protocol_markdown) != protocol["protocol_markdown_sha256"]:
        raise ValueError("Markdown protocol changed before freeze")
    output.mkdir(parents=True, exist_ok=False)
    inputs = {"generator": GENERATOR, "cases": [generate_case(s, r)
              for r in REGIMES for s in protocol["seeds"]]}
    write_json(output / "inputs.json", inputs)
    (output / "protocol.json").write_bytes(protocol_path.read_bytes())
    (output / "protocol.md").write_bytes(protocol_markdown.read_bytes())
    (output / "runner.py").write_bytes(Path(__file__).read_bytes())
    helper = Path(reliability.__file__)
    (output / "planner.py").write_bytes(helper.read_bytes())
    write_json(output / "freeze.json", {"schema_version": 1, "outcomes_started": False,
               "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
               "sha256": {name: sha256(output / name) for name in
                          ("inputs.json", "protocol.json", "protocol.md", "runner.py", "planner.py")}})


def run_frozen(output: Path) -> dict:
    manifest = json.loads((output / "freeze.json").read_text())
    if (output / "report.json").exists() or (output / "RUN_STARTED").exists():
        raise FileExistsError("Never overwrite or silently rerun a frozen evaluation")
    for name, digest in manifest["sha256"].items():
        if sha256(output / name) != digest:
            raise ValueError(f"Frozen file changed: {name}")
    if sha256(Path(__file__)) != manifest["sha256"]["runner.py"]:
        raise ValueError("Runner changed after freeze")
    if sha256(Path(reliability.__file__)) != manifest["sha256"]["planner.py"]:
        raise ValueError("Planner changed after freeze")
    (output / "RUN_STARTED").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    cases = json.loads((output / "inputs.json").read_text())["cases"]
    rows = [run_case(case, policy) for case in cases for policy in POLICIES]
    strict = [run_case(case, policy, allow_cold_restart=False)
              for case in cases for policy in POLICIES]
    report = {"schema_version": 1, "model_only": True, "source_commit": "0217e45",
              "freeze": manifest, "main_runs": rows, "strict_controls": strict,
              "summary": summarize(rows, list(REGIMES)),
              "strict_summary": summarize(strict, list(REGIMES)),
              "limitations": [
                  "Synthetic model, not GPU execution or an estimate of campus workload prevalence.",
                  "All jobs start together and use one GPU; same cohort admitted in every arm.",
                  "One held-out recall per device, followed by stable continuation after owner use.",
                  "History distributions and runtime declarations may be wrong; negative regimes retained.",
                  "No shared checkpoint bandwidth, simultaneous resource conflicts, or arbitrary training.",
                  "Strict failures use the preregistered 400-second reporting horizon, not invented completion.",
                  "Bootstrap intervals describe paired synthetic seeds only; no real-world confidence claim.",
              ]}
    write_json(output / "report.json", report)
    write_json(output / "result-manifest.json", {"report_sha256": sha256(output / "report.json"),
                                                "freeze_sha256": sha256(output / "freeze.json")})
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--freeze-only", action="store_true")
    action.add_argument("--run-frozen", action="store_true")
    args = parser.parse_args()
    if args.freeze_only:
        if args.protocol is None:
            parser.error("--freeze-only requires --protocol")
        freeze(args.protocol, args.output)
        print(json.dumps({"status": "FROZEN_NOT_RUN", "output": str(args.output)}))
    else:
        report = run_frozen(args.output)
        print(json.dumps({"main_runs": len(report["main_runs"]),
                          "strict_controls": len(report["strict_controls"]),
                          "output": str(args.output)}))


if __name__ == "__main__":
    main()
