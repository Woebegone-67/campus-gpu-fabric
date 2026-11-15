"""Frozen, causal paired model for the production reclaim decision policy.

This is a parametric outcome model, not a measured GPU benchmark. The planner
never receives actual outcome rates, faults, or future recall events.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from campus_fabric.policies.reclaim import REPORT_MARGIN_SECONDS, ReclaimInputs, plan_reclaim

ROOT = Path(__file__).resolve().parents[2]
POLICIES = {
    "cold_restart": "立即停止、从零重启",
    "immediate_save": "立即保存",
    "latest_save": "固定最晚安全保存",
    "finish_or_save": "能完成则完成、否则立即保存",
    "adaptive": "自适应收回管理",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def online_inputs(case: dict, weight: float) -> ReclaimInputs:
    """A strict field boundary: no hidden actual/future data reaches selection."""
    return ReclaimInputs(**case["observed"], owner_wait_weight=weight)


def select(policy: str, inputs: ReclaimInputs) -> dict:
    adaptive = plan_reclaim(inputs)
    if policy == "adaptive":
        return adaptive
    stop_at = max(inputs.now, min(inputs.checkpoint_due,
                                 inputs.owner_deadline - inputs.stop_cost_upper))
    latest = stop_at - inputs.checkpoint_cost_upper - REPORT_MARGIN_SECONDS
    action, dispatch = "save_now", inputs.now
    if policy == "cold_restart" or latest < inputs.now:
        action = "stop"
    elif policy == "latest_save":
        action, dispatch = "save_at", latest
    elif policy == "finish_or_save":
        known = (inputs.trusted and inputs.observed_at is not None
                 and 0 <= inputs.now - inputs.observed_at <= 1.0
                 and inputs.progress_steps is not None and inputs.total_steps is not None
                 and inputs.rate_lower is not None and inputs.rate_lower > 0)
        if known:
            remaining = (inputs.total_steps - inputs.progress_steps) / inputs.rate_lower
            if remaining <= latest - inputs.now:
                action, dispatch = "finish_first", latest
    elif policy != "immediate_save":
        raise ValueError(f"Unknown policy: {policy}")
    return {"action": action, "dispatch_at": dispatch, "stop_at": stop_at,
            "reason": policy, "inputs": inputs.__dict__}


def execute_episode(case: dict, inputs: ReclaimInputs, choice: dict) -> dict:
    """Independent continuous-time outcome accounting with a stop barrier.

    Saving blocks computation and captures its starting state. The model has no
    speculative overlap or hidden benefit from continuing during a disk write.
    """
    actual = case["actual"]
    rate = actual["rate"]
    progress, total = inputs.progress_steps, inputs.total_steps
    remaining = (total - progress) / rate
    at = max(0., choice["dispatch_at"] - inputs.now)
    stop_at = max(0., choice["stop_at"] - inputs.now)
    action = choice["action"]
    complete_before_action = action != "stop" and remaining <= min(at, stop_at)
    if complete_before_action or remaining == 0:
        return {
            "status": "completed", "branch": "finished_before_checkpoint",
            "retained_steps": total, "rollback_steps": 0., "checkpoint_seconds": 0.,
            "restart_seconds": 0., "owner_wait_seconds": remaining + actual["stop_seconds"],
            "remaining_compute_seconds": 0., "compute_after_notice_seconds": remaining,
            "checkpoint_failed": False,
        }
    if action == "stop":
        failed = not inputs.allow_cold_restart
        return {
            "status": "failed" if failed else "interrupted", "branch": "forced_stop"
            if failed else "cold_restart",
            "retained_steps": 0., "rollback_steps": progress, "checkpoint_seconds": 0.,
            "restart_seconds": 0. if failed else actual["restart_seconds"],
            "owner_wait_seconds": actual["stop_seconds"],
            "remaining_compute_seconds": None if failed else total / rate,
            "compute_after_notice_seconds": 0.,
            "checkpoint_failed": False,
        }
    at = min(at, stop_at)
    retained = min(total, progress + at * rate)
    save_end = at + actual["checkpoint_seconds"]
    at_boundary = math.isclose(save_end, stop_at, rel_tol=0., abs_tol=1e-9)
    failure = actual["checkpoint_fails"] or save_end > stop_at or at_boundary
    save_time = max(0., min(save_end, stop_at) - at)
    terminal = failure and not inputs.allow_cold_restart
    return {
        "status": "failed" if terminal else "interrupted",
        "branch": "checkpoint_failed" if failure else "saved",
        "retained_steps": 0. if failure else retained,
        "rollback_steps": retained if failure else 0.,
        "checkpoint_seconds": save_time,
        "restart_seconds": 0. if terminal else actual["restart_seconds"],
        "owner_wait_seconds": min(save_end, stop_at) + actual["stop_seconds"],
        "remaining_compute_seconds": None if terminal else (total if failure else total - retained) / rate,
        "compute_after_notice_seconds": at,
        "checkpoint_failed": failure,
    }


def run_case(case: dict, policy: str, preference: str, weight: float, protocol: dict) -> dict:
    inputs = online_inputs(case, weight)
    episodes = []
    wall = 0.
    between = protocol["between_recalls_compute_seconds"]
    for index in range(case["recall_count"]):
        choice = select(policy, inputs)
        outcome = execute_episode(case, inputs, choice)
        outcome["deadline_lateness_seconds"] = max(
            0., outcome["owner_wait_seconds"] - (inputs.owner_deadline - inputs.now))
        episodes.append({"index": index + 1, "decision": choice, "outcome": outcome})
        wall += outcome["owner_wait_seconds"]
        if outcome["status"] in {"completed", "failed"}:
            break
        unavailable_until = inputs.owner_deadline - inputs.now + protocol["owner_use_seconds"]
        wait_for_owner = max(0., unavailable_until - outcome["owner_wait_seconds"])
        outcome["resource_wait_seconds"] = wait_for_owner
        wall += wait_for_owner + outcome["restart_seconds"]
        if index + 1 < case["recall_count"]:
            useful = min(between, outcome["remaining_compute_seconds"])
            wall += useful
            progress = min(inputs.total_steps,
                           outcome["retained_steps"] + useful * case["actual"]["rate"])
            # New observation is available only after this resumed compute.
            # Bounds remain frozen; future slowdown is not fed back as hindsight.
            inputs = replace(inputs, progress_steps=int(progress), observed_at=inputs.now)
    last = episodes[-1]["outcome"]
    failed = last["status"] == "failed"
    remaining = last["remaining_compute_seconds"]
    if not failed and last["status"] != "completed":
        wall += remaining
    owner_wait = sum(row["outcome"]["owner_wait_seconds"] for row in episodes)
    overhead = sum(row["outcome"]["checkpoint_seconds"] + row["outcome"]["restart_seconds"]
                   for row in episodes)
    score_remaining = protocol["failure_completion_horizon_seconds"] if failed else remaining
    return {
        "case": case["case"], "case_label": case["label"], "policy": policy,
        "preference": preference, "owner_wait_weight": weight,
        "preemption_mode": "best_effort" if inputs.allow_cold_restart else "checkpoint",
        "action": episodes[0]["decision"]["action"],
        "status": "failed" if failed else "completed", "recalls_observed": len(episodes),
        "owner_wait_seconds": owner_wait, "overhead_seconds": overhead,
        "remaining_compute_seconds": remaining,
        "retained_compute_seconds": last["retained_steps"] / case["actual"]["rate"],
        "repeated_compute_seconds": None if failed else sum(
            row["outcome"]["rollback_steps"] / case["actual"]["rate"] for row in episodes),
        "discarded_compute_seconds": sum(
            row["outcome"]["rollback_steps"] / case["actual"]["rate"] for row in episodes),
        "deadline_misses": sum(row["outcome"]["deadline_lateness_seconds"] > 1e-8
                               for row in episodes),
        "max_return_lateness_seconds": max(
            row["outcome"]["deadline_lateness_seconds"] for row in episodes),
        "checkpoint_failures": sum(row["outcome"]["checkpoint_failed"] for row in episodes),
        "completion_seconds": None if failed else wall,
        "truncated_completion_seconds": protocol["failure_completion_horizon_seconds"]
        if failed else wall,
        "cost": score_remaining + overhead + weight * owner_wait,
        "episodes": episodes,
    }


def make_report(protocol: dict, protocol_path: Path) -> dict:
    rows = [run_case(copy.deepcopy(case), policy, preference, weight, protocol)
            for preference, weight in protocol["preferences"].items()
            for case in protocol["cases"] for policy in protocol["policies"]]
    summaries = []
    comparisons = []
    for preference in protocol["preferences"]:
        for policy in protocol["policies"]:
            subset = [r for r in rows if r["preference"] == preference and r["policy"] == policy]
            summaries.append({
                "policy": policy, "label": POLICIES[policy], "preference": preference,
                "cases": len(subset), "completed": sum(r["status"] == "completed" for r in subset),
                "mean_cost": mean(r["cost"] for r in subset),
                "mean_owner_wait_seconds": mean(r["owner_wait_seconds"] for r in subset),
                "deadline_misses": sum(r["deadline_misses"] for r in subset),
                "checkpoint_failures": sum(r["checkpoint_failures"] for r in subset),
                "mean_retained_compute_seconds": mean(r["retained_compute_seconds"] for r in subset),
                "mean_truncated_completion_seconds": mean(r["truncated_completion_seconds"] for r in subset),
            })
        adaptive = {r["case"]: r for r in rows
                    if r["preference"] == preference and r["policy"] == "adaptive"}
        for policy in protocol["policies"]:
            if policy == "adaptive":
                continue
            baseline = {r["case"]: r for r in rows
                        if r["preference"] == preference and r["policy"] == policy}
            differences = {case: baseline[case]["cost"] - adaptive[case]["cost"]
                           for case in adaptive}
            comparisons.append({
                "preference": preference, "baseline": policy,
                "mean_cost_reduction": mean(differences.values()),
                "adaptive_wins": [k for k, v in differences.items() if v > 1e-8],
                "ties": [k for k, v in differences.items() if abs(v) <= 1e-8],
                "adaptive_losses": [k for k, v in differences.items() if v < -1e-8],
                "paired_cost_reduction": differences,
            })
    strict_rows = []
    for case in protocol["cases"]:
        if case["case"] not in protocol["return_modes"]["strict_controls"]:
            continue
        strict = copy.deepcopy(case)
        strict["observed"]["allow_cold_restart"] = False
        for preference, weight in protocol["preferences"].items():
            for policy in protocol["policies"]:
                if policy != "cold_restart":
                    strict_rows.append(run_case(strict, policy, preference, weight, protocol))
    nominal_cases = {c["case"] for c in protocol["cases"] if c["case"] not in {
        "stale-observation", "save-failure", "compute-slowdown", "save-underestimate",
        "stop-underestimate"}}
    group_summary = []
    for group, cases in (("nominal", nominal_cases),
                         ("stress", {c["case"] for c in protocol["cases"]} - nominal_cases)):
        for preference in protocol["preferences"]:
            for policy in protocol["policies"]:
                subset = [r for r in rows if r["case"] in cases
                          and r["preference"] == preference and r["policy"] == policy]
                group_summary.append({"group": group, "preference": preference, "policy": policy,
                                      "cases": len(subset), "mean_cost": mean(r["cost"] for r in subset),
                                      "deadline_misses": sum(r["deadline_misses"] for r in subset),
                                      "checkpoint_failures": sum(r["checkpoint_failures"] for r in subset)})
    return {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_label": "参数化执行模型（CPU 小状态保存成本仅提供量级参照）",
        "protocol_sha256": sha256(protocol_path),
        "planner_sha256": sha256(ROOT / "src/campus_fabric/policies/reclaim.py"),
        "runner_sha256": sha256(Path(__file__)),
        "preferences": protocol["preferences"], "summary": summaries,
        "comparisons": comparisons, "runs": rows, "strict_controls": strict_rows,
        "group_summary": group_summary, "primary_preemption_mode": "best_effort",
        "limitations": [
            "14 个预先固定的条件，不是校园负载分布或独立统计样本。",
            "数值来自参数化模型，不代表真实 GPU 吞吐或训练加速。",
            "昂贵保存参数是敏感性条件，未经当前约 600 字节 CPU 工件标定。",
            "收益按贡献者所选等待权重计算；降低该成本不等于所有原始指标同时改善。",
            "突发性能下降、保存失败和停止耗时低估可能破坏预计收益或归还上界。",
            "当前实现针对通知后的单任务动作选择，不包含风险预测放置或共享保存带宽优化。",
            "主表允许失败后从零重启；强制保存模式另列控制案例，不能把主表收益当作该模式收益。",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=ROOT / "evidence/reclaim-aware-20261115")
    args = parser.parse_args()
    protocol_path = args.output / "protocol.json"
    protocol = json.loads(protocol_path.read_text())
    report = make_report(protocol, protocol_path)
    report_path = args.output / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    columns = [k for k in report["runs"][0] if k != "episodes"]
    with (args.output / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({k: row[k] for k in columns} for row in report["runs"])
    manifest = {"schema_version": 1, "artifacts": {
        "report": {"path": "report.json", "sha256": sha256(report_path)},
        "protocol": {"path": "protocol.json", "sha256": sha256(protocol_path)},
    }}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"runs": len(report["runs"]), "summary": report["summary"],
                      "comparisons": [{k: v for k, v in c.items() if k != "paired_cost_reduction"}
                                      for c in report["comparisons"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
