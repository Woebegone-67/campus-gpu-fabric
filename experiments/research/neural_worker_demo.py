"""One local WorkflowWorker demo using the fixed trained model and case index 0.

This is operational wiring evidence, not a new performance experiment. It uses
two fresh SQLite instances, declared logical jobs and the observed half of the
sealed predictable/index0 case. No production service or hidden future is read.
"""
from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from campus_fabric.contracts import models as m
from campus_fabric.policies.availability_model import FEATURE_NAMES
from campus_fabric.policies.learned import LearnedAvailabilityPolicy, task_gpu_rankings
from campus_fabric.policies.reliability import MAX_AGE
from campus_fabric.workflows.availability_inputs import specification_sha256
from campus_fabric.workflows.devhost import (
    OBSERVER,
    WorkflowWorker,
    availability_input_producer,
)
from experiments.research.neural_kernel_replay import (
    ROOT,
    TTL,
    advance_to,
    plain,
    read,
    require,
    selected,
    setup_kernel,
    sha256,
    verify_study,
    write_json,
)

NAMESPACE = "local-neural-worker-fixed-case-0"
ENV_NAMES = ("FABRIC_AVAILABILITY_MODEL_PATH", "FABRIC_AVAILABILITY_MODEL_SHA256",
             "FABRIC_AVAILABILITY_INPUTS_PATH", "FABRIC_AVAILABILITY_INPUTS_SHA256",
             "FABRIC_AVAILABILITY_NAMESPACE")


@contextmanager
def configured(values):
    previous = {name: os.environ.get(name) for name in ENV_NAMES}
    try:
        for name in ENV_NAMES:
            if name in values:
                os.environ[name] = values[name]
            else:
                os.environ.pop(name, None)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class RecordingKernel:
    """Transparent local recorder; commits and reads still execute in A's kernel."""

    def __init__(self, kernel):
        self.kernel, self.commits = kernel, []

    def __getattr__(self, name):
        return getattr(self.kernel, name)

    def commit(self, command):
        receipt = self.kernel.commit(command)
        self.commits.append({"command": plain(command), "receipt": plain(receipt)})
        return receipt


def registry_from_observed(observed, persisted, model_sha):
    intents = {item.intent_id: item for item in persisted.intents}
    at = observed["at"]
    devices = {}
    for device in observed["devices"]:
        # This runtime adapter requires every retained sample to be recent. The
        # full original observed record is preserved separately. Do not refresh
        # history timestamps or pretend these are all 32 study input windows.
        recent = [window for window in device["completed_history"]
                  if 0 <= at - window["ended_at"] <= MAX_AGE
                  and window["available_at"] <= at]
        require(len(recent) >= 4, "Fixed case has insufficient genuinely recent history")
        devices[device["device_id"]] = {
            "contributor_owner_id": device["device_id"], "completed_windows": recent,
            "current_window_started_at": at, "observed_at": at, "available_at": at,
            "notice_seconds": device["notice_seconds"],
            "owner_use_seconds": device["owner_use_seconds"],
        }
    tasks = {}
    for task in observed["tasks"]:
        intent = intents[task["task_id"]]
        tasks[intent.intent_id] = {"owner_id": intent.owner_id,
            "specification_sha256": specification_sha256(intent.specification),
            "observed_at": at, "available_at": at,
            **{name: task[name] for name in ("estimated_compute_seconds", "checkpoint_seconds",
                                           "restart_seconds", "stop_seconds")}}
    return {"schema_version": 1, "namespace": NAMESPACE, "epoch": persisted.version.epoch,
            "execution_mode": "logical_simulation", "calendar_origin": "logical_day0_monday",
            "utc_offset_hours": 0., "model_sha256": model_sha, "devices": devices, "tasks": tasks}


def ranking_inputs(snapshot):
    tasks = []
    for demand in snapshot.demands:
        fields = {item.name: item.value for item in demand.known_data}
        tasks.append({"task_id": demand.intent_id,
                      **{name: fields[name] for name in ("estimated_compute_seconds", "checkpoint_seconds",
                         "restart_seconds", "stop_seconds", "remaining_recoveries")}})
    devices = []
    for node in snapshot.inventory:
        ident = node.devices[0].physical_gpu_id
        fields = {item.name: item.value for item in snapshot.metadata if item.source_id == ident}
        devices.append({"device_id": ident, "features": {name: fields[name] for name in FEATURE_NAMES},
                        **{name: fields[name] for name in ("notice_seconds", "owner_use_seconds")}})
    return tasks, devices


def worker_assignments(recorder, worker):
    # First tick commits placements. Second tick advances persisted work stages
    # to jobs. Both calls use the real existing worker, not a replacement loop.
    worker.tick()
    worker.tick()
    snapshot = read(recorder)
    require(len(snapshot.intents) == 4 and all(item.state == "running" for item in snapshot.intents),
            "Actual WorkflowWorker did not bind four running logical jobs")
    mappings = []
    for record in recorder.commits:
        command = record["command"]
        if command["operation"] != "decision.commit":
            continue
        require(record["receipt"]["outcome"] == "committed", "Actual worker plan was rejected")
        plan = m.DecisionPlan.model_validate_json(json.dumps(command["parameters"]["plan"]))
        choice = selected(plan)
        require(choice is not None, "Worker committed an empty decision")
        lease = next(item for item in snapshot.allocations if item.allocation_id == choice.demand_id)
        mappings.append({"task_id": choice.intent_id, "allocation_id": choice.demand_id,
            "gpu": choice.placement.slices[0].device_ids[0], "policy_name": plan.policy.name,
            "neural_active": plan.model is not None and plan.model.name == "neural-availability-window",
            "model_sha256": None if plan.model is None else plan.model.sha256,
            "expires_at": lease.expires_at, "decision_id": plan.decision_id})
    require(len(mappings) == 4 and len({item["gpu"] for item in mappings}) == 4,
            "Worker allocation set is incomplete or not exclusive")
    return mappings


def run(study, output):
    require(not output.exists(), "Output must be a fresh directory")
    observed, predictor, model_sha, freeze = verify_study(study)
    output.mkdir(parents=True, exist_ok=False)
    (output / "RUN_STARTED").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    write_json(output / "original-observed.json", observed)
    kernel, clock, registrations = setup_kernel(output / "learned-worker.sqlite", observed)
    persisted = kernel.read_snapshot(m.ReadQuery(actor=OBSERVER, global_scope=True))
    registry = registry_from_observed(observed, persisted, model_sha)
    registry_path = output / "reviewed-inputs.json"
    write_json(registry_path, registry)
    registry_sha = sha256(registry_path)
    write_json(output / "setup-receipts.json", {"clock": clock, "registrations": registrations,
               "authoritative_read": plain(persisted)})
    settings = dict(zip(ENV_NAMES, (str(study / "model.json"), model_sha,
                                    str(registry_path), registry_sha, NAMESPACE), strict=True))
    recorder = RecordingKernel(kernel)
    with configured(settings):
        producer = availability_input_producer(policy_name="P")
        worker = WorkflowWorker(recorder, policy_name="P", input_producer=producer)
    require(isinstance(worker.policy, LearnedAvailabilityPolicy), "Factory failed to enable pinned learned P")
    enriched = producer.enrich(kernel.decision_snapshot(), persisted)
    diagnostics = producer.last_diagnostics
    require(enriched.demands[0].known_data, "Producer fallback: " + "; ".join(diagnostics))
    tasks, devices = ranking_inputs(enriched)
    rankings = task_gpu_rankings(tasks, devices, predictor)
    write_json(output / "enriched-snapshot.json", plain(enriched))
    write_json(output / "ranking-inputs.json", {"tasks": tasks, "devices": devices})
    write_json(output / "task-gpu-rankings.json", rankings)
    learned = worker_assignments(recorder, worker)
    require(all(item["neural_active"] and item["model_sha256"] == model_sha for item in learned),
            "At least one actual worker decision did not use the pinned trained model")
    write_json(output / "learned-worker-commits.json", recorder.commits)
    write_json(output / "learned-running-state.json", plain(read(kernel)))

    # A second independent instance executes the same pending work with the
    # original default P. This is a wiring comparison, not a performance result.
    baseline_kernel, _, _ = setup_kernel(output / "default-worker.sqlite", observed)
    baseline_recorder = RecordingKernel(baseline_kernel)
    with configured({}):
        baseline_worker = WorkflowWorker(baseline_recorder, policy_name="P")
    default = worker_assignments(baseline_recorder, baseline_worker)
    by_task = {item["task_id"]: item for item in default}
    comparison = [{**item, "default_gpu": by_task[item["task_id"]]["gpu"],
                   "changed": item["gpu"] != by_task[item["task_id"]]["gpu"]} for item in learned]
    require(any(item["changed"] for item in comparison),
            "Fixed case did not change placement versus disabled model; preserve this evidence")
    write_json(output / "default-worker-commits.json", baseline_recorder.commits)

    target = learned[0]
    contributor = m.AuthenticatedActor(actor_id=target["gpu"], owner_id=target["gpu"], role="contributor")
    recall = kernel.commit(m.CommandEnvelope(request_id="worker-demo-real-one-second-recall", actor=contributor,
        operation="contribution.recall", parameters=m.RecallParameters(kind="contribution.recall",
            device_ids=(target["gpu"],), notice_seconds=1, duration_seconds=20)))
    require(recall.outcome == "committed", "Authoritative contributor recall rejected")
    recall_clock = advance_to(kernel, observed["at"] + 1)
    after = next(item for item in read(kernel).allocations if item.allocation_id == target["allocation_id"])
    notice = next(item for item in kernel.decision_snapshot().notices if target["gpu"] in item.device_ids)
    require(after.state == "Preempted" and after.placement is None and notice.deadline == observed["at"] + 1,
            "One-second actual contributor deadline was not honored")
    expiry_clock = advance_to(kernel, observed["at"] + TTL)
    require(all(item.state == "Expired" and item.placement is None for item in read(kernel).allocations),
            "Original lease expiry was not honored")
    write_json(output / "recall-and-expiry.json", {"recall": plain(recall), "clock": recall_clock,
        "notice": plain(notice), "allocation_at_deadline": plain(after), "expiry_clock": expiry_clock,
        "final_read": plain(read(kernel))})
    require(all(item["expires_at"] == observed["at"] + TTL for item in learned), "Lease TTL changed")
    verify_study(study)
    source_paths = [Path(__file__), ROOT / "experiments/research/neural_kernel_replay.py",
                    *sorted((ROOT / "src/campus_fabric").rglob("*.py"))]
    receipt = {"status": "PASS", "scope": "actual local WorkflowWorker wiring; no performance claim",
        "case": {"group": "predictable", "index": 0, "observed_at": observed["at"]},
        "model_sha256": model_sha, "training_cutoff": predictor.trained_through,
        "study_inputs_sha256": freeze["sha256"]["inputs.json"], "registry_sha256": registry_sha,
        "namespace": NAMESPACE, "producer_diagnostics": diagnostics,
        "worker_producer_diagnostics": worker.availability_input_status,
        "assignments": comparison, "actual_recall_seconds": 1, "profile_notice_seconds": 12,
        "actual_recall_deadline": notice.deadline, "original_ttl_seconds": TTL,
        "all_leases_expired_at_original_deadline": True,
        "sources": {str(path.relative_to(ROOT)): sha256(path) for path in source_paths},
        "limitations": ["Reviewed export is synthetic historical evidence, not automatic live collection.",
            "Runtime producer uses only the fixed case's genuine last-24h windows, not all 32 study records.",
            "New-window age=0 and explicit 900-second logical jobs; GPU stock is simulated.",
            "This proves executable wiring and allocation differences, not improved completion time."]}
    write_json(output / "receipt.json", receipt)
    lines = ["# 神经模型接入实际 WorkflowWorker：本地运行回执", "", "结果：**PASS**。固定案例 predictable / index 0。", "",
        "真实已训练模型 → 固定 SHA 外部历史 registry → AvailabilityInputProducer → P 合法计划 → WorkflowWorker → A SQLite commit。", "",
        "| 工作 | 默认 P GPU | 神经 P GPU | 分配改变 |", "| --- | --- | --- | --- |"]
    lines.extend(f"| {item['task_id']} | {item['default_gpu']} | {item['gpu']} | {item['changed']} |"
                 for item in comparison)
    lines += ["", f"模型 SHA：`{model_sha}`。", f"输入 registry SHA：`{registry_sha}`。", "",
        "四项工作由实际 worker 建立租约和运行任务，GPU 分配互斥。贡献者随后发出 1 秒收回通知，A 在该期限中断对应租约并移除占用，预测中的 12 秒通知期没有延长真实权利。所有租约仍在原始 900 秒期限到期。", "",
        "实际输入从固定案例原历史中筛出24小时内的完整窗口，保留原始时间戳。排名使用 producer 实际输出，未读取隐藏未来日历。完整排名见 task-gpu-rankings.json。", "",
        "本次仅证明真实本地 worker 接线可运行及模型改变了分配，不证明性能提升。历史来自经审核的合成案例，不宣称自动采集线上数据；未启动真实 GPU 任务，未修改生产服务。", ""]
    (output / "RECEIPT.md").write_text("\n".join(lines))
    write_json(output / "manifest.json", {path.name: sha256(path) for path in output.iterdir()
        if path.is_file() and path.suffix in {".json", ".md", ".sqlite"}})
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    existed = output.exists()
    try:
        receipt = run(args.study_dir.resolve(), output)
    except Exception as error:
        if not existed and output.exists():
            write_json(output / "FAILURE.json", {"status": "FAILED", "error_type": type(error).__name__,
                "error": str(error), "source_sha256": sha256(Path(__file__))})
        raise
    print("PASS | fixed predictable/index0 | actual WorkflowWorker + SQLite")
    print("Task | default P GPU | learned P GPU | changed")
    for item in receipt["assignments"]:
        print(f"{item['task_id']} | {item['default_gpu']} | {item['gpu']} | {item['changed']}")
    print("Contributor recall: 1 second honored; lease expiry: original 900 seconds honored")
    print("Evidence: " + str(output))


if __name__ == "__main__":
    main()
