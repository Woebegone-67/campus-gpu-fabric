"""Isolated authority replay of fixed predictable/index0; no production writes.

This is a separate contract check, not part of the sealed neural comparison.
Only the selected case's ``observed`` dictionary enters inference. Its raw
history keeps original observation/availability times; the synthetic protocol
declares a newly opened window at the probe timestamp. No live A feature
producer, real GPU work, model accuracy or workload performance is asserted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.policies import availability_model as model
from campus_fabric.policies.baselines import make_policy
from campus_fabric.policies.learned import LearnedAvailabilityPolicy, task_gpu_rankings
from campus_fabric.policies.reliability import MAX_AGE, MIN_SAMPLES
from campus_fabric.workflows import WorkflowOrchestrator

ROOT = Path(__file__).resolve().parents[2]
OWNER = m.AuthenticatedActor(actor_id="local-neural-replay", owner_id="alice", role="researcher")
SYSTEM = m.AuthenticatedActor(actor_id="local-neural-replay-worker", role="system")
TTL = 900


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def plain(value):
    return value.to_dict() if hasattr(value, "to_dict") else value.model_dump(mode="json")


def require(condition, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read(kernel):
    return kernel.read_snapshot(m.ReadQuery(actor=OWNER))


def selected(plan):
    return next((candidate for candidate in plan.candidates
                 if candidate.candidate_id == plan.selected_candidate_id), None)


def advance_to(kernel, target: float) -> list[dict]:
    receipts = []
    while kernel.decision_snapshot().now < target:
        now = kernel.decision_snapshot().now
        receipt = kernel.commit(m.CommandEnvelope(
            request_id=f"replay-clock-{now:g}-{target:g}", actor=SYSTEM,
            operation="clock.advance", parameters=m.ClockAdvanceParameters(
                kind="clock.advance", advance_seconds=min(86400., target - now), source="test_control"),
        ))
        require(receipt.outcome == "committed", "Isolated clock command rejected")
        receipts.append(plain(receipt))
    return receipts


def commit_plan(kernel, plan, *, suffix=""):
    return kernel.commit(m.CommandEnvelope(
        request_id=f"replay-apply-{plan.decision_id}{suffix}", actor=SYSTEM,
        operation="decision.commit", expected_version=plan.snapshot_version,
        parameters=m.DecisionCommitParameters(kind="decision.commit", plan=plan),
    ))


def inventory(observed):
    nodes = []
    for device in observed["devices"]:
        ident = device["device_id"]
        gpu = m.GPUDevice(device_id=ident, physical_gpu_id=ident, model="A100",
                          memory_mib=40960, state="available")
        capacity = m.ResourceAmounts(cpu_millicores=8000, memory_mib=32768,
                                     gpu_count=1, gpu_memory_mib=40960)
        nodes.append(m.NodeInventory(node_id="node-" + ident, contributor_owner_id=ident,
            state="Ready", capacity=capacity, available=capacity, observed_at=0., devices=(gpu,)))
    return tuple(nodes)


def registration(task):
    return m.WorkIntentRegistration(
        intent_id=task["task_id"], owner_id="alice", execution_mode="logical_simulation",
        settlement_action="keep_lease",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(
                kind="allocation.create", owner_id="alice", ttl_seconds=TTL,
                max_restarts=task["remaining_recoveries"], preemption_mode="restartable",
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                    gpu=m.GPURequest(count=1, memory_mib=1024))),
            job=m.JobSpecification(image="local-contract-replay", command=("no-real-execution",),
                                   timeout_seconds=TTL),
        ),
    )


def setup_kernel(path, observed):
    kernel = Kernel(path, inventory=inventory(observed), scheduling_mode="external",
                    simulation_duration_seconds=900., execution_mode="logical_simulation")
    receipts = advance_to(kernel, observed["at"])
    registrations = []
    workflow = WorkflowOrchestrator(kernel, kernel)
    for task in observed["tasks"]:
        request = registration(task)
        result = workflow.start(request, kernel.decision_snapshot().version,
                                actor=OWNER, request_id="register-" + task["task_id"])
        require(result.state == "waiting", "Local registration did not create a pending demand")
        registrations.append({"registration": plain(request), "result": plain(result)})
    return kernel, receipts, registrations


def datum(name, value, ident, *, observed_at, available_at=None, source):
    return m.ObservedDatum(name=name, value=value, source=source, source_id=ident,
        observed_at=observed_at, available_at=observed_at if available_at is None else available_at)


def enrich(snapshot, observed):
    """Explicit local research adapter; no timestamps are invented for history."""
    require(snapshot.now == observed["at"], "Fresh-window metadata cannot be replayed at another time")
    tasks = {task["task_id"]: task for task in observed["tasks"]}
    demands, history, metadata, diagnostics = [], [], [], []
    for demand in snapshot.demands:
        task = tasks[demand.intent_id]
        known = [datum(name, task[name], demand.demand_id, observed_at=snapshot.now,
                       source="declared_profile") for name in (
            "estimated_compute_seconds", "checkpoint_seconds", "restart_seconds", "stop_seconds")]
        known.extend(datum(name, value, demand.demand_id, observed_at=snapshot.now,
                           source="public_metadata") for name, value in (
            ("remaining_recoveries", task["remaining_recoveries"]),
            ("execution_window_seconds", TTL)))
        demands.append(demand.model_copy(update={"known_data": tuple(known)}))
    for device in observed["devices"]:
        ident = device["device_id"]
        features = model.historical_features(snapshot.now, device["completed_history"], utc_offset_hours=0.)
        require(features == device["features"], "Saved features differ from causal raw-history extraction")
        recent = 0
        for record in device["completed_history"]:
            require(record["ended_at"] <= record["available_at"] <= snapshot.now,
                    "Future or delayed history cannot enter replay")
            history.append(datum("availability_window_seconds", record["duration_seconds"], ident,
                source="history", observed_at=record["ended_at"], available_at=record["available_at"]))
            recent += snapshot.now - record["ended_at"] <= MAX_AGE
        metadata.extend(datum(name, value, ident, source="history", observed_at=snapshot.now)
                        for name, value in features.items())
        metadata.extend(datum(name, device[name], ident, source="public_metadata", observed_at=snapshot.now)
                        for name in ("notice_seconds", "owner_use_seconds"))
        # A probe starts at observed.at by the frozen synthetic protocol. This is
        # not inferred from a hidden future end or claimed to be a live event.
        metadata.append(datum("availability_window_age_seconds", 0., ident,
                              source="observed_event", observed_at=observed["at"]))
        diagnostics.append({"device_id": ident, "completed_history_count": len(device["completed_history"]),
            "recent_count": recent, "minimum_recent_count": MIN_SAMPLES, "features": features})
    return m.DecisionSnapshot.model_validate({**snapshot.model_dump(), "demands": tuple(demands),
        "history": tuple(history), "metadata": tuple(metadata)}), diagnostics


def pinned_policy(model_path, model_sha):
    names = ("FABRIC_AVAILABILITY_MODEL_PATH", "FABRIC_AVAILABILITY_MODEL_SHA256")
    old = {name: os.environ.get(name) for name in names}
    try:
        os.environ[names[0]], os.environ[names[1]] = str(model_path), model_sha
        policy = make_policy("P")
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    require(isinstance(policy, LearnedAvailabilityPolicy), "Explicit P opt-in did not construct learned policy")
    return policy


def verify_study(study):
    freeze = json.loads((study / "freeze.json").read_text())
    for name, expected in freeze["sha256"].items():
        require(sha256(study / name) == expected, "Sealed study artifact changed: " + name)
    for frozen_name, live_path in {
        "model.py": Path(model.__file__),
        "adapter.py": ROOT / "src/campus_fabric/policies/learned.py",
        "renewal.py": ROOT / "src/campus_fabric/policies/renewal.py",
        "matching.py": ROOT / "src/campus_fabric/policies/reliability.py",
    }.items():
        require(sha256(live_path) == freeze["sha256"][frozen_name],
                "Live inference source differs from sealed study: " + frozen_name)
    manifest = json.loads((study / "result-manifest.json").read_text())
    require(sha256(study / "freeze.json") == manifest["freeze_sha256"], "Study freeze manifest changed")
    model_sha = manifest["model_sha256"]
    predictor = model.load_predictor(study / "model.json", expected_sha256=model_sha)
    # Only observed is retained; this script never accesses case.actual or the
    # downstream report. Fixed case selection is independent of its outcomes.
    inputs = json.loads((study / "inputs.json").read_text())
    matching = [case["observed"] for case in inputs["cases"]
                if case["group"] == "predictable" and case["index"] == 0]
    require(len(matching) == 1, "Exactly one preselected predictable/index0 case is required")
    observed = matching[0]
    training_hash = hashlib.sha256(json.dumps(inputs["training"], sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    artifact = json.loads((study / "model.json").read_text())
    require(artifact["training"]["data_sha256"] == training_hash, "Model is not trained from pinned study rows")
    require(predictor.trained_through <= observed["at"], "Model includes labels later than replay time")
    require(inputs["config"]["lease_ttl_seconds"] == TTL
            and inputs["config"]["horizon_seconds"] == TTL, "Replay TTL must match the declared study horizon")
    return observed, predictor, model_sha, freeze


def run(study: Path, output: Path) -> dict:
    require(not output.exists(), "Replay output must be a new directory; never overwrite evidence")
    observed, predictor, model_sha, freeze = verify_study(study)
    output.mkdir(parents=True, exist_ok=False)
    (output / "RUN_STARTED").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    write_json(output / "observed.json", observed)
    rankings = task_gpu_rankings(observed["tasks"], observed["devices"], predictor)
    write_json(output / "task-gpu-rankings.json", rankings)
    policy = pinned_policy(study / "model.json", model_sha)
    kernel, initial_clock, registrations = setup_kernel(output / "allocation.sqlite", observed)
    write_json(output / "setup-receipts.json", {"clock": initial_clock, "registrations": registrations})
    plans, mapping = [], []
    workflow = WorkflowOrchestrator(kernel, kernel)
    diagnostics = []
    for index in range(len(observed["tasks"])):
        snapshot, diagnostics = enrich(kernel.decision_snapshot(), observed)
        write_json(output / f"snapshot-{index}.json", plain(snapshot))
        baseline = make_policy("B2").plan(snapshot)
        plan = policy.plan(snapshot)
        choice = selected(plan)
        require(choice is not None and choice.feasibility == "legal", "P returned no legal action")
        require(choice.demand_id == selected(baseline).demand_id, "P changed B2 admission order")
        receipt = commit_plan(kernel, plan)
        require(receipt.outcome == "committed", "A rejected P's local plan: " + str(receipt.error))
        result = workflow.advance(choice.intent_id, actor=OWNER)
        require(result.state == "running", "Accepted allocation did not bind to a simulated job")
        lease = next(item for item in read(kernel).allocations if item.allocation_id == choice.demand_id)
        require(lease.expires_at == observed["at"] + TTL, "Policy changed the registered lease expiry")
        active = not plan.rationale.startswith("Learned matching inactive;")
        chosen_device = choice.placement.slices[0].device_ids[0]
        mapping.append({"task_id": choice.intent_id, "allocation_id": choice.demand_id,
            "selected_device": chosen_device, "neural_active": active,
            "b2_device": selected(baseline).placement.slices[0].device_ids[0],
            "lease_expires_at": lease.expires_at})
        plans.append({"plan": plain(plan), "selected_candidate": plain(choice),
                      "commit_receipt": plain(receipt), "workflow_result": plain(result),
                      "allocation_after_commit": plain(lease)})
    require(len({row["selected_device"] for row in mapping}) == len(mapping), "GPU assignment is not exclusive")
    write_json(output / "plans-and-commit-receipts.json", plans)

    target = mapping[0]
    contributor = m.AuthenticatedActor(actor_id=target["selected_device"],
        owner_id=target["selected_device"], role="contributor")
    recall = kernel.commit(m.CommandEnvelope(request_id="local-owner-one-second-recall", actor=contributor,
        operation="contribution.recall", parameters=m.RecallParameters(kind="contribution.recall",
            device_ids=(target["selected_device"],), notice_seconds=1, duration_seconds=20)))
    require(recall.outcome == "committed", "Actual contributor recall was rejected")
    recall_clock = advance_to(kernel, observed["at"] + 1)
    after_recall = next(lease for lease in read(kernel).allocations
                        if lease.allocation_id == target["allocation_id"])
    require(after_recall.state == "Preempted" and after_recall.placement is None,
            "Contributor's actual one-second deadline did not remove the allocation")
    notice = next(item for item in kernel.decision_snapshot().notices
                  if target["selected_device"] in item.device_ids)
    require(notice.deadline == observed["at"] + 1, "Prediction extended contributor's actual deadline")
    expiry_clock = advance_to(kernel, observed["at"] + TTL)
    expired = read(kernel).allocations
    require(all(lease.state == "Expired" and lease.placement is None for lease in expired),
            "Registered leases failed to expire at the unchanged 900-second limit")
    write_json(output / "recall-and-expiry-receipts.json", {"recall": plain(recall),
        "clock_to_deadline": recall_clock, "notice": plain(notice), "allocation_at_deadline": plain(after_recall),
        "clock_to_expiry": expiry_clock, "allocations_at_expiry": [plain(lease) for lease in expired]})

    # Separate SQLite instance verifies stale-plan rejection without altering the
    # allocation sequence or selecting another study case.
    stale_kernel, _, _ = setup_kernel(output / "stale-version.sqlite", observed)
    stale_plan = policy.plan(enrich(stale_kernel.decision_snapshot(), observed)[0])
    advance_to(stale_kernel, observed["at"] + 1)
    stale_receipt = commit_plan(stale_kernel, stale_plan, suffix="-stale")
    require(stale_receipt.outcome == "rejected" and stale_receipt.error.code == "stale_version",
            "Kernel accepted a stale model-generated plan")
    write_json(output / "stale-version-receipt.json", plain(stale_receipt))
    source_files = [Path(__file__), *sorted((ROOT / "src/campus_fabric").rglob("*.py"))]
    sources = {str(path.relative_to(ROOT)): sha256(path) for path in source_files}
    report = {"status": "PASS" if all(row["neural_active"] for row in mapping) else "PASS_WITH_FALLBACK",
        "scope": "independent local SQLite contract replay; no performance or live deployment claim",
        "case": {"group": "predictable", "index": 0, "observed_at": observed["at"]},
        "study_directory": str(study), "inputs_sha256": freeze["sha256"]["inputs.json"],
        "model_sha256": model_sha, "trained_through": predictor.trained_through,
        "source_sha256": sources, "history_diagnostics": diagnostics, "task_gpu_rankings": rankings,
        "committed_assignments": mapping, "actual_return_deadline": notice.deadline,
        "declared_notice_seconds": 12., "actual_notice_seconds": 1., "stale_plan_rejected": True,
        "unchanged_lease_ttl_seconds": TTL, "all_allocations_expired_at_original_deadline": True,
        "limitations": ["Research metadata adapter, not a production A observation producer.",
            "Kernel runs fixed 900-second logical jobs solely to exercise authority, not study runtimes.",
            "B2 candidate ordering and preserved ties may differ from helper study matching.",
            "Forecast rankings alone do not grant resource authority."]}
    write_json(output / "receipt.json", report)
    lines = ["# Trained neural policy — isolated kernel replay", "",
        f"Result: **{report['status']}**. Fixed case: predictable / index 0.", "",
        f"Model SHA-256: `{model_sha}`.", f"Observed logical time: {observed['at']:g}; training cutoff: {predictor.trained_through:g}.", "",
        "Only causal observed history was replayed. Raw observation times were retained; no case.actual or result report informed selection.", "",
        "| Task | Selected GPU | Neural active | B2 GPU | Original lease expiry |",
        "| --- | --- | --- | --- | --- |"]
    lines.extend(f"| {row['task_id']} | {row['selected_device']} | {row['neural_active']} | {row['b2_device']} | {row['lease_expires_at']:g} |"
                 for row in mapping)
    lines += ["", "All four selected plans were committed by the actual SQLite kernel and bound to simulated jobs. GPU assignments were exclusive.",
        "The contributor then issued a 1-second recall despite the 12-second declared profile; the kernel preempted the selected lease and removed its placement at that actual deadline.",
        "All leases expired at their original 900-second deadline. A separate local SQLite instance rejected a stale neural plan with stale_version.", "",
        "| Task | GPU | Task-specific rank | Predicted restricted completion (s) |",
        "| --- | --- | --- | --- |"]
    lines.extend(f"| {row['task_id']} | {row['device_id']} | {row['rank']} | {row['predicted_restricted_seconds']:.6f} |"
                 for row in rankings)
    lines += ["", "Full snapshots, plans, commit receipts, recall/expiry receipts and source hashes accompany this record.",
        "This separate verification is outside the frozen comparison. It proves contract execution and authority boundaries, not beneficial predictive performance or deployed learning.", ""]
    (output / "RECEIPT.md").write_text("\n".join(lines))
    write_json(output / "manifest.json", {path.name: sha256(path) for path in output.iterdir()
        if path.is_file() and path.suffix in {".json", ".md", ".sqlite"}})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    existed = args.output.exists()
    try:
        result = run(args.study_dir.resolve(), args.output.resolve())
    except Exception as error:
        if not existed and args.output.exists():
            write_json(args.output / "FAILURE.json", {"status": "FAILED", "error_type": type(error).__name__,
                "error": str(error), "script_sha256": sha256(Path(__file__)),
                "scope": "Independent contract replay failure, not a formal study rerun"})
        raise
    print(json.dumps({"status": result["status"], "output": str(args.output.resolve()),
                      "model_sha256": result["model_sha256"]}))


if __name__ == "__main__":
    main()
