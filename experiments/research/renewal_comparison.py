"""Prospective repeated-window simulation; hidden calendars never reach selectors.

The sealed one-recall study is retained unchanged. This study preserves its
task/history cases and changes only the prospective availability process and
the predeclared eight-arm comparison. No real GPU or live product mutation.
"""
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

from campus_fabric.policies import reliability, renewal
from experiments.research import reliability_comparison as first

ROOT = Path(__file__).resolve().parents[2]
REGIMES = first.REGIMES
POLICIES = (*first.POLICIES, "renewal_edge_greedy", "renewal_batch")
CONFIG = {"version": "reliability-renewal-v1", "horizon_seconds": 900.,
          "calendar_windows": 64, "owner_use_seconds": 10.,
          "report_margin_seconds": .1, "bootstrap_resamples": 2000,
          "bootstrap_seed": 20261115, "include_strict_controls": False,
          "max_recoveries": 2, "lease_ttl_seconds": 900.,
          "future_rng_prefix": "renewal-future-window"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def generate_case(source: dict) -> dict:
    """Retain source tasks/history exactly; add a separate held-out calendar."""
    case = copy.deepcopy(source)
    for task in case["observed"]["tasks"]:
        task["remaining_recoveries"] = CONFIG["max_recoveries"]
        task["execution_window_seconds"] = CONFIG["horizon_seconds"]
    means = [10., 30., 60., 140.]
    random.Random(f"device-order:{case['seed']}").shuffle(means)
    if case["regime"] == "baseline_controls":
        means = [300.] * 4
    elif case["regime"] == "homogeneous_devices":
        means = [45.] * 4
    elif case["regime"] == "reversed_history":
        inverse = dict(zip(sorted(means), reversed(sorted(means)), strict=True))
        means = [inverse[value] for value in means]
    for i, (device, actual_device) in enumerate(zip(
            case["observed"]["devices"], case["actual"]["devices"], strict=True)):
        device["owner_use_seconds"] = CONFIG["owner_use_seconds"]
        rng = random.Random(f"{CONFIG['future_rng_prefix']}:{case['seed']}:{i}")
        windows = [means[i] * rng.uniform(.75, 1.25) for _ in range(CONFIG["calendar_windows"])]
        if case["regime"] == "baseline_controls":
            windows = [300.] * CONFIG["calendar_windows"]
        actual_device.pop("window_seconds")
        actual_device.update(windows=windows, owner_use_seconds=CONFIG["owner_use_seconds"])
        coverage = sum(windows) + len(windows) * (
            actual_device["notice_seconds"] + actual_device["owner_use_seconds"])
        if coverage < CONFIG["horizon_seconds"]:
            raise ValueError("Predeclared calendar does not cover the observation horizon")
    return case


def select(policy: str, observed: dict) -> tuple[int, ...]:
    if policy in first.POLICIES:
        return first.select(policy, observed)
    if policy not in {"renewal_edge_greedy", "renewal_batch"}:
        raise ValueError(f"Unknown policy: {policy}")
    matrix = renewal.budgeted_cost_matrix(observed["tasks"], observed["devices"])
    if policy == "renewal_batch":
        assignment = reliability.batch_assign(matrix)
        if assignment is None:
            raise ValueError("The fixed admitted cohort unexpectedly has no matching")
        return assignment
    available = set(range(len(observed["devices"])))
    assignment = []
    for row in matrix:
        device = min(available, key=lambda column: (row[column], column))
        assignment.append(device)
        available.remove(device)
    return tuple(assignment)


def execute_task(task: dict, device: dict, actual_task: dict, calendar: dict, *,
                 horizon: float = 900., allow_cold_restart: bool = True,
                 max_recoveries: int | None = 2) -> dict:
    """Independent continuous event execution with durable and volatile progress.

    Every assignment uses this responder. Actual deadlines become public only
    at notice; actual runtime can finish execution but cannot select its action.
    A horizon cut records only costs already incurred and keeps pending returns.
    """
    total = actual_task["compute_seconds"]
    checkpoint_cost = actual_task["checkpoint_seconds"]
    actual_stop = calendar["stop_seconds"]
    notice = calendar["notice_seconds"]
    owner_use = calendar["owner_use_seconds"]
    restart_cost = task["restart_seconds"]
    progress = durable = compute = waste = checkpoint_used = restart_used = stop_used = 0.
    owner_wait = max_lateness = 0.
    completed_at = None
    terminal_failed = False
    budget_exhausted = False
    recoveries = 0
    has_checkpoint = False
    returned_at = 0.
    pending_returns = deadline_misses = checkpoint_failures = 0
    episodes = []
    window_start = 0.

    def finish_return(notice_at, stop_start, episode):
        nonlocal owner_wait, max_lateness, stop_used, deadline_misses, pending_returns, returned_at
        returned_at = stop_start + actual_stop
        stop_used += max(0., min(returned_at, horizon) - stop_start)
        wait = max(0., min(returned_at, horizon) - notice_at)
        lateness = max(0., min(returned_at, horizon) - (notice_at + notice))
        owner_wait += wait
        max_lateness = max(max_lateness, lateness)
        deadline_misses += lateness > 1e-8
        pending_returns += returned_at > horizon
        episode.update(returned_at=returned_at if returned_at <= horizon else None,
                       owner_wait_seconds=wait, return_lateness_seconds=lateness)
        return returned_at <= horizon

    def horizon_during_notice(notice_at, episode):
        nonlocal owner_wait, max_lateness, deadline_misses, pending_returns
        wait = max(0., horizon - notice_at)
        lateness = max(0., horizon - (notice_at + notice))
        owner_wait += wait
        max_lateness = max(max_lateness, lateness)
        deadline_misses += lateness > 1e-8
        pending_returns += 1
        episode.update(returned_at=None, owner_wait_seconds=wait,
                       return_lateness_seconds=lateness, horizon_cut=True)

    for index, window in enumerate(calendar["windows"]):
        if window_start >= horizon:
            break
        notice_at = window_start + window
        next_window_start = notice_at + notice + owner_use
        attempt_start = max(window_start, returned_at)
        if attempt_start >= horizon:
            break
        if index:
            if max_recoveries is not None and recoveries >= max_recoveries:
                budget_exhausted = True
                break
            recoveries += 1
        restart = restart_cost if index else 0.
        # Restart occupies the available interval; it is not appended after it.
        restart_done = attempt_start + restart
        restart_used += max(0., min(restart_done, notice_at, horizon) - attempt_start)
        if horizon <= min(restart_done, notice_at):
            break
        cursor = min(restart_done, notice_at)
        if restart_done <= notice_at:
            amount = min(total - progress, max(0., min(notice_at, horizon) - cursor))
            compute += amount
            progress += amount
            cursor += amount
            if progress >= total - 1e-9:
                completed_at = cursor
                break
            if cursor >= horizon:
                break
        episode = {"window_index": index, "window_start": window_start,
                   "notice_at": notice_at, "deadline": notice_at + notice,
                   "progress_at_notice": progress, "durable_at_notice": durable}
        episodes.append(episode)
        latest = notice - task["stop_seconds"] - task["checkpoint_seconds"] - CONFIG["report_margin_seconds"]
        estimated_remaining = max(0., task["estimated_compute_seconds"] - progress)
        if latest <= 0. or restart_done > notice_at:
            action, dispatch = "stop", 0.
        elif estimated_remaining <= latest:
            action, dispatch = "finish_or_save", latest
        else:
            action, dispatch = "save_now", 0.
        episode["action"] = action
        cursor = notice_at
        if action == "finish_or_save":
            amount = min(total - progress, dispatch, max(0., horizon - cursor))
            compute += amount
            progress += amount
            cursor += amount
            if progress >= total - 1e-9:
                completed_at = cursor
                finish_return(notice_at, cursor, episode)
                break
            if cursor >= horizon:
                horizon_during_notice(notice_at, episode)
                break
        saved = False
        cp_spent = 0.
        if action != "stop":
            stop_at = notice_at + max(0., notice - task["stop_seconds"])
            finish_cp = cursor + checkpoint_cost
            end_cp = min(finish_cp, stop_at, horizon)
            cp_spent = max(0., end_cp - cursor)
            checkpoint_used += cp_spent
            if horizon < min(finish_cp, stop_at):
                horizon_during_notice(notice_at, episode)
                episode["checkpoint_seconds"] = cp_spent
                break
            cursor = end_cp
            saved = (finish_cp < stop_at and not math.isclose(finish_cp, stop_at, abs_tol=1e-9)
                     and not actual_task["checkpoint_fails"]
                     and index not in actual_task.get("checkpoint_failure_windows", []))
            checkpoint_failures += not saved
            if saved:
                durable, has_checkpoint = progress, True
        episode.update(checkpoint_seconds=cp_spent, saved=saved)
        if not finish_return(notice_at, cursor, episode):
            break
        if not saved:
            lost = progress - durable
            waste += lost
            progress = durable
            episode["discarded_compute_seconds"] = lost
            if not (allow_cold_restart or has_checkpoint):
                terminal_failed = True
                break
        else:
            episode["discarded_compute_seconds"] = 0.
        if max_recoveries is not None and recoveries >= max_recoveries:
            budget_exhausted = True
            break
        window_start = next_window_start
    else:
        if window_start < horizon:
            raise ValueError("calendar_exhausted: no stable tail may be fabricated")

    completed = completed_at is not None and completed_at <= horizon
    assert abs(compute - waste - progress) < 1e-6, "Physical compute accounting must conserve progress"
    noncompletion_reason = (None if completed else "budget_exhausted" if budget_exhausted
                            else "no_checkpoint_and_cold_restart_forbidden" if terminal_failed
                            else "lease_end_at_horizon" if horizon == CONFIG["lease_ttl_seconds"] else "horizon")
    return {"task_id": task["task_id"], "device_id": device["device_id"],
            "completed": completed, "censored": not completed and not terminal_failed and not budget_exhausted,
            "noncompletion_reason": noncompletion_reason,
            "terminal_failed": terminal_failed, "completion_seconds": completed_at,
            "budget_exhausted": budget_exhausted, "recoveries": recoveries,
            "restricted_completion_seconds": completed_at if completed else horizon,
            "offered_actual_compute_seconds": total, "physical_compute_seconds": compute,
            "compute_waste_seconds": waste, "progress_seconds": progress,
            "durable_progress_seconds": durable, "checkpoint_seconds": checkpoint_used,
            "restart_seconds": restart_used, "stop_seconds": stop_used,
            "overhead_seconds": checkpoint_used + restart_used,
            "owner_wait_seconds": owner_wait, "deadline_misses": deadline_misses,
            "max_return_lateness_seconds": max_lateness, "pending_returns": pending_returns,
            "checkpoint_failures": checkpoint_failures, "recalls": len(episodes),
            "episodes": episodes}


def run_case(case: dict, policy: str, *, allow_cold_restart=True) -> dict:
    assignment = select(policy, case["observed"])
    jobs = [execute_task(task, case["observed"]["devices"][j], case["actual"]["tasks"][i],
                         case["actual"]["devices"][j], horizon=CONFIG["horizon_seconds"],
                         allow_cold_restart=allow_cold_restart,
                         max_recoveries=task["remaining_recoveries"])
            for i, (task, j) in enumerate(zip(case["observed"]["tasks"], assignment, strict=True))]
    offered = sum(job["offered_actual_compute_seconds"] for job in jobs)
    waste = sum(job["compute_waste_seconds"] for job in jobs)
    overhead = sum(job["overhead_seconds"] for job in jobs)
    return {"seed": case["seed"], "regime": case["regime"], "policy": policy,
            "assignment": list(assignment), "allow_cold_restart": allow_cold_restart, "jobs": jobs,
            "admitted": len(jobs), "completed": sum(job["completed"] for job in jobs),
            "noncompleted": sum(not job["completed"] for job in jobs),
            "censored": sum(job["censored"] for job in jobs),
            "terminal_failed": sum(job["terminal_failed"] for job in jobs),
            "budget_exhausted": sum(job["budget_exhausted"] for job in jobs),
            "restricted_mean_completion_seconds": mean(job["restricted_completion_seconds"] for job in jobs),
            "offered_actual_compute_seconds": offered,
            "completed_useful_compute_seconds": sum(job["offered_actual_compute_seconds"] for job in jobs
                                                     if job["completed"]),
            "compute_waste_seconds": waste, "waste_fraction_of_offered_compute": waste / offered,
            "overhead_seconds": overhead, "waste_and_overhead_seconds": waste + overhead,
            **{name: sum(job[name] for job in jobs) for name in (
                "checkpoint_seconds", "restart_seconds", "stop_seconds", "owner_wait_seconds",
                "deadline_misses", "pending_returns", "checkpoint_failures", "recalls")},
            "max_return_lateness_seconds": max(job["max_return_lateness_seconds"] for job in jobs)}


METRICS = ("restricted_mean_completion_seconds", "completed", "noncompleted", "censored", "terminal_failed", "admitted",
           "budget_exhausted",
           "compute_waste_seconds", "waste_fraction_of_offered_compute", "overhead_seconds",
           "waste_and_overhead_seconds", "completed_useful_compute_seconds", "offered_actual_compute_seconds",
           "checkpoint_seconds", "restart_seconds", "owner_wait_seconds", "deadline_misses",
           "pending_returns", "recalls", "max_return_lateness_seconds")


def summarize(rows: list[dict]) -> dict:
    tables, comparisons = [], []
    for group in ["all_regimes", *REGIMES]:
        subset = [row for row in rows if group == "all_regimes" or row["regime"] == group]
        for policy in POLICIES:
            chosen = [row for row in subset if row["policy"] == policy]
            tables.append({"group": group, "policy": policy, "cases": len(chosen),
                           **{name: mean(row[name] for row in chosen) for name in METRICS},
                           "completed_total": sum(row["completed"] for row in chosen),
                           "censored_total": sum(row["censored"] for row in chosen),
                           "blocked_budget_exhausted_total": sum(row["budget_exhausted"] for row in chosen),
                           "noncompleted_total": sum(row["noncompleted"] for row in chosen),
                           "admitted_total": sum(row["admitted"] for row in chosen),
                           "waste_total_divided_by_offered_compute_total":
                           sum(row["compute_waste_seconds"] for row in chosen)
                           / sum(row["offered_actual_compute_seconds"] for row in chosen)})
        target = {(row["regime"], row["seed"]): row for row in subset if row["policy"] == "renewal_batch"}
        for baseline in POLICIES[:-1]:
            pairs = [(row, target[(row["regime"], row["seed"])]) for row in subset if row["policy"] == baseline]
            seeds = sorted({old["seed"] for old, new in pairs})
            deltas = [mean(old["restricted_mean_completion_seconds"] - new["restricted_mean_completion_seconds"]
                           for old, new in pairs if old["seed"] == seed) for seed in seeds]
            baseline_mean = mean(old["restricted_mean_completion_seconds"] for old, new in pairs)
            harms = [{"regime": new["regime"], "seed": new["seed"], "task_id": nj["task_id"],
                      "restricted_completion_delta_seconds": nj["restricted_completion_seconds"]
                      - oj["restricted_completion_seconds"],
                      "waste_delta_seconds": nj["compute_waste_seconds"] - oj["compute_waste_seconds"],
                      "baseline_completed": oj["completed"], "renewal_batch_completed": nj["completed"],
                      "baseline_noncompletion_reason": oj["noncompletion_reason"],
                      "renewal_batch_noncompletion_reason": nj["noncompletion_reason"],
                      "baseline_censored": oj["censored"], "renewal_batch_censored": nj["censored"]}
                     for old, new in pairs for oj, nj in zip(old["jobs"], new["jobs"], strict=True)]
            waste_saved = sum(old["compute_waste_seconds"] - new["compute_waste_seconds"] for old, new in pairs)
            offered = sum(old["offered_actual_compute_seconds"] for old, new in pairs)
            comparisons.append({"group": group, "baseline": baseline,
                                "paired_mean_restricted_completion_reduction_seconds": mean(deltas),
                                "relative_restricted_completion_reduction": mean(deltas) / baseline_mean,
                                "synthetic_seed_bootstrap_95_percent": first.paired_interval(deltas),
                                "absolute_wasted_compute_saved_seconds": waste_saved,
                                "wasted_compute_saved_divided_by_offered_compute": waste_saved / offered,
                                "completed_difference": sum(new["completed"] - old["completed"] for old, new in pairs),
                                "completion_harmed_jobs": sum(h["restricted_completion_delta_seconds"] > 1e-8 for h in harms),
                                "completion_helped_jobs": sum(h["restricted_completion_delta_seconds"] < -1e-8 for h in harms),
                                "worst_completion_harm_seconds": max(h["restricted_completion_delta_seconds"] for h in harms),
                                "per_job_differences": harms})
    return {"tables": tables, "comparisons": comparisons}


def freeze(protocol_path: Path, output: Path) -> None:
    protocol = json.loads(protocol_path.read_text())
    if (protocol["seeds"] != list(range(20)) or protocol["regimes"] != list(REGIMES)
            or protocol["policies"] != list(POLICIES) or protocol["config"] != CONFIG):
        raise ValueError("Protocol differs from the fixed prospective design")
    source = ROOT / protocol["source_inputs"]
    original_runner = ROOT / protocol["source_runner"]
    markdown = ROOT / protocol["protocol_markdown"]
    for path, expected in ((source, protocol["source_inputs_sha256"]),
                           (original_runner, protocol["source_runner_sha256"]),
                           (markdown, protocol["protocol_markdown_sha256"])):
        if sha256(path) != expected:
            raise ValueError(f"Source/protocol hash mismatch: {path.name}")
    if sha256(Path(first.__file__)) != sha256(original_runner):
        raise ValueError("The original six selectors must remain unchanged")
    cases = json.loads(source.read_text())["cases"]
    expected_cases = {(regime, seed) for regime in REGIMES for seed in range(20)}
    if len(cases) != 180 or {(case["regime"], case["seed"]) for case in cases} != expected_cases:
        raise ValueError("All 180 original cases are required exactly once")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "inputs.json", {"config": CONFIG, "cases": [generate_case(case) for case in cases]})
    copies = {"source-inputs.json": source, "one-recall-runner.py": original_runner,
              "protocol.json": protocol_path, "protocol.md": markdown,
              "runner.py": Path(__file__), "one-recall-planner.py": Path(reliability.__file__),
              "renewal-planner.py": Path(renewal.__file__)}
    for name, path in copies.items():
        (output / name).write_bytes(path.read_bytes())
    write_json(output / "freeze.json", {"schema_version": 1, "outcomes_started": False,
               "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
               "sha256": {name: sha256(output / name) for name in ("inputs.json", *copies)}})


def run_frozen(output: Path) -> dict:
    manifest = json.loads((output / "freeze.json").read_text())
    if (output / "report.json").exists() or (output / "RUN_STARTED").exists():
        raise FileExistsError("Never overwrite or rerun sealed comparative evidence")
    for name, expected in manifest["sha256"].items():
        if sha256(output / name) != expected:
            raise ValueError(f"Frozen artifact changed: {name}")
    for name, path in (("runner.py", Path(__file__)), ("one-recall-runner.py", Path(first.__file__)),
                       ("one-recall-planner.py", Path(reliability.__file__)),
                       ("renewal-planner.py", Path(renewal.__file__))):
        if sha256(path) != manifest["sha256"][name]:
            raise ValueError(f"Source changed after freeze: {name}")
    (output / "RUN_STARTED").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    cases = json.loads((output / "inputs.json").read_text())["cases"]
    rows = [run_case(case, policy) for case in cases for policy in POLICIES]
    report = {"schema_version": 1, "model_only": True, "freeze": manifest,
              "main_runs": rows, "summary": summarize(rows), "strict_controls": "NOT_RUN",
              "limitations": [
                  "Post-first-study hypothesis, prospectively frozen second synthetic study; not independent field confirmation.",
                  "All tasks start at zero, remain on their initial device, and have one fixed runtime/checkpoint profile.",
                  "Renewal prediction assumes independent stationary past-window renewal; adverse controls remain included.",
                  "Budgeted prediction uses conservative one-second quantization; measured execution is continuous time.",
                  "Main maximum recoveries is 2; declared TTL/timeout 900 seconds is legal but not the product default 300.",
                  "Budget-exhausted jobs are blocked unfinished, not successful and not merely lost observations.",
                  "Censored tasks remain unfinished; 900 is a reporting horizon, not their observed completion time.",
                  "No real GPU, queue selection, migration, production resource, checkpoint bandwidth contention or trace calibration.",
                  "Offered actual useful computation is a model denominator, not measured GPU throughput.",
                  "Helper-level matching comparison, not a product-policy replay; wrapper ties preserve B2 placement.",
              ]}
    write_json(output / "report.json", report)
    write_json(output / "result-manifest.json", {"report_sha256": sha256(output / "report.json"),
                                                "freeze_sha256": sha256(output / "freeze.json")})
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--freeze-only", action="store_true")
    mode.add_argument("--run-frozen", action="store_true")
    args = parser.parse_args()
    if args.freeze_only:
        if args.protocol is None:
            parser.error("--freeze-only requires --protocol")
        freeze(args.protocol, args.output)
        print(json.dumps({"status": "FROZEN_NOT_RUN", "output": str(args.output)}))
    else:
        report = run_frozen(args.output)
        print(json.dumps({"main_runs": len(report["main_runs"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
