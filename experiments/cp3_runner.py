"""CP3 isolated logical-kernel runner. No online policy receives scenario inputs."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.policies import make_policy
from campus_fabric.policies.candidate import CandidatePolicy
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator
from experiments.cp3_inputs import POLICIES

ADMIN = m.AuthenticatedActor(actor_id="cp3-evidence-reader", role="administrator")
SYSTEM = m.AuthenticatedActor(actor_id="cp3-clock-controller", role="system")
DRIVER = m.AuthenticatedActor(actor_id="cp3-exogenous-driver", role="test_driver")
SUPPLIER = m.AuthenticatedActor(actor_id="cp3-supplier", role="contributor", owner_id="supplier")


def owner_actor(owner):
    return m.AuthenticatedActor(actor_id=f"cp3-{owner}", role="researcher", owner_id=owner)


def policy_for(name: str, max_candidates: int = 512):
    if name not in POLICIES:
        raise ValueError("Unknown policy label")
    return (CandidatePolicy(max_candidates=max_candidates, opportunity_enabled=False)
            if name == "P-no-opportunity" else make_policy(name, max_candidates=max_candidates))


def registration(work):
    return m.WorkIntentRegistration(intent_id=work["intent_id"], owner_id=work["owner_id"],
        execution_mode="logical_simulation", settlement_action="release_on_terminal",
        specification=m.WorkSpecification(allocation=m.AllocationCreateParameters(
            kind="allocation.create", owner_id=work["owner_id"], ttl_seconds=work["ttl_seconds"],
            priority=work["priority"], preemption_mode="checkpoint", max_restarts=work["max_restarts"],
            checkpoint_timeout_seconds=work["checkpoint_timeout_seconds"],
            request=m.ResourceRequest(cpu_millicores=work["cpu_millicores"], memory_mib=work["memory_mib"],
                gpu=m.GPURequest(count=work["gpu_count"], model=work["gpu_model"],
                    memory_mib=work["gpu_memory_mib"], profile=work["profile"]))),
            job=m.JobSpecification(image="declared-logical-model", command=("no-image-execution",),
                                   timeout_seconds=work["timeout_seconds"], max_retries=work["max_retries"])))


def action_signature(plan):
    selected = next((c for c in plan.candidates if c.candidate_id == plan.selected_candidate_id), None)
    if selected is None:
        return None
    return {"demand_id": selected.demand_id, "action": selected.action,
            "placement": selected.placement.model_dump(mode="json") if selected.placement else None}


def _schedule(inputs):
    horizon = inputs["horizon"]
    quantum = inputs["model"]["dispatcher_quantum_seconds"]
    ticks = {0., float(horizon)}
    ticks.update(float(t) for t in range(0, int(horizon) + 1, quantum))
    ticks.update(w["arrived_at"] for w in inputs["works"])
    for event in inputs["events"]:
        ticks.add(float(event["at"]))
        if event["kind"] == "recall":
            ticks.add(event["at"] + event["notice_seconds"])
            ticks.add(event["at"] + event["notice_seconds"] + event["duration_seconds"])
    return sorted(t for t in ticks if 0 <= t <= horizon)


def execute_scenario(inputs: dict, policy_name: str, *, db_path: Path) -> dict:
    """All business mutations are real A commands; each run has a new database."""
    if len(inputs["works"]) > 64 or not 1 <= inputs["horizon"] <= 3600:
        raise ValueError("CP3 bounded execution budget exceeded")
    if db_path.exists():
        raise ValueError("Refuse to reuse or reset an existing database")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kernel = Kernel(db_path, inventory=tuple(m.NodeInventory.model_validate_json(json.dumps(row)) for row in inputs["inventory"]),
                    scheduling_mode="external", simulation_duration_seconds=inputs["model"]["duration_seconds"])
    workflow = WorkflowOrchestrator(kernel, kernel)
    policy = policy_for(policy_name, inputs["model"]["max_candidates"])
    baseline = policy_for("B2", inputs["model"]["max_candidates"])
    ablation = policy_for("P-no-opportunity", inputs["model"]["max_candidates"])
    receipts, commands, decisions, failures, resource_samples, injections = [], [], [], [], [], []
    registered, resumes, reports = set(), set(), set()
    works = {w["intent_id"]: w for w in inputs["works"]}
    counter = 0
    fast_forward_at = None
    def read():
        return kernel.read_snapshot(m.ReadQuery(actor=ADMIN, global_scope=True))
    def command(parameters, actor=SYSTEM, expected=None):
        nonlocal counter
        counter += 1
        envelope = m.CommandEnvelope(request_id=f"cp3-command-{counter}", actor=actor,
            operation=parameters.kind, parameters=parameters, expected_version=expected)
        receipt = kernel.commit(envelope)
        receipts.append(receipt.model_dump(mode="json"))
        commands.append({"command": envelope.model_dump(mode="json"), "receipt": receipt.model_dump(mode="json")})
        return receipt
    wall_started = time.perf_counter()
    try:
        previous = 0.
        for tick in _schedule(inputs):
            if fast_forward_at is not None and tick != inputs["horizon"]:
                continue
            if tick > previous:
                response = command(m.ClockAdvanceParameters(kind="clock.advance", advance_seconds=tick-previous, source="manual"))
                if response.outcome == "rejected":
                    raise RuntimeError(f"clock rejected: {response.error}")
            previous = tick
            for event in inputs["events"]:
                if event["at"] != tick:
                    continue
                if event["kind"] == "recall":
                    response = command(m.RecallParameters(kind="contribution.recall", device_ids=tuple(event["device_ids"]),
                        notice_seconds=event["notice_seconds"], duration_seconds=event["duration_seconds"]), SUPPLIER)
                elif event["kind"] == "node_state":
                    response = command(m.NodeStateParameters(kind="test.node_state", node_id=event["node_id"], state=event["state"]), DRIVER)
                elif event["kind"] in {"late_callback", "failure_callback"}:
                    current = read()
                    intent = next((i for i in current.intents if i.intent_id == event["intent_id"]), None)
                    attempt = next((a for a in intent.attempts if a.ref.generation == event["generation"]), None) if intent else None
                    terminal = attempt and attempt.state in {"stopped", "failed", "cancelled", "completed"}
                    allowed = terminal if event["kind"] == "late_callback" else attempt and not terminal
                    if not allowed:
                        injections.append({"input": event, "status": "not_injected_target_not_eligible"})
                        continue
                    response = command(m.ExecutorReportParameters(kind="executor.report", event=m.ExecutorEvent(
                        callback_id=f"{event['kind']}-{event['intent_id']}-{event['generation']}", version=current.version,
                        attempt=attempt.ref, kind="completed" if event["kind"] == "late_callback" else "failed",
                        error=m.ErrorDetail(code="internal_error", detail="Frozen stable-work execution failure") if event["kind"] == "failure_callback" else None,
                        mode="logical_simulation", observed_at=current.now)))
                    injections.append({"input": event, "status": "injected", "attempt": attempt.ref.model_dump(mode="json"),
                                       "receipt": response.model_dump(mode="json")})
                    if event["kind"] == "late_callback":
                        if response.outcome != "rejected" or not response.error or response.error.code != "attempt_stale":
                            failures.append({"kind": "late_callback_not_rejected_as_stale", "receipt": response.model_dump(mode="json")})
                    elif response.outcome != "committed":
                        failures.append({"kind": "stable_failure_injection_rejected", "receipt": response.model_dump(mode="json")})
                    continue
                else:
                    raise ValueError("Unknown frozen external event")
                injections.append({"input": event, "status": "injected", "receipt": response.model_dump(mode="json")})
                if response.outcome == "rejected":
                    failures.append({"kind": "exogenous_event_rejected", "event": event, "receipt": response.model_dump(mode="json")})
            for work in inputs["works"]:
                if work["arrived_at"] == tick:
                    progress = workflow.start(registration(work), kernel.decision_snapshot().version,
                                              actor=owner_actor(work["owner_id"]), request_id=f"arrival-{work['intent_id']}")
                    receipts.extend(r.model_dump(mode="json") for r in progress.receipts)
                    registered.add(work["intent_id"])
            if any(e["kind"] == "recall" and e["at"] <= tick for e in inputs["events"]):
                current = read()
                allocation_work = {aid: works[i.intent_id] for i in current.intents for aid in i.allocation_ids}
                for allocation in current.allocations:
                    work = allocation_work.get(allocation.allocation_id)
                    key = (allocation.allocation_id, allocation.restart_count)
                    if work and allocation.state == "PreemptRequested" and allocation.checkpoint is None and key not in reports:
                        reports.add(key)
                        behavior = work["checkpoint_behavior"]
                        if behavior != "timeout":
                            command(m.CheckpointReportParameters(kind="allocation.checkpoint", allocation_id=allocation.allocation_id,
                                                                status=behavior), owner_actor(allocation.owner_id))
                if any(a.state == "PreemptRequested" for a in current.allocations):
                    current = read()
                for allocation in current.allocations:
                    key = (allocation.allocation_id, allocation.restart_count)
                    if allocation.state == "Preempted" and key not in resumes:
                        resumes.add(key)
                        command(m.AllocationTargetParameters(kind="allocation.resume", allocation_id=allocation.allocation_id),
                                owner_actor(allocation.owner_id))
            newly_granted = set()
            for _ in range(len(inputs["works"]) + 1):
                snapshot = kernel.decision_snapshot()
                if not snapshot.demands:
                    break
                before = time.perf_counter_ns()
                plan = policy.plan(snapshot)
                elapsed_ns = time.perf_counter_ns() - before
                diagnostic = None
                if policy_name in {"P", "P-no-opportunity"}:
                    before = time.perf_counter_ns()
                    b2 = baseline.plan(snapshot)
                    off = ablation.plan(snapshot)
                    diagnostic = {"same_state_b2_action": action_signature(b2),
                                  "same_state_opportunity_off_action": action_signature(off),
                                  "off_matches_b2": action_signature(off) == action_signature(b2),
                                  "action_changed_from_b2": action_signature(plan) != action_signature(b2),
                                  "diagnostic_elapsed_ns": time.perf_counter_ns() - before,
                                  "interpretation": "same-state action replacement only; not a counterfactual outcome gain"}
                row = {"snapshot": snapshot.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
                       "elapsed_ns": elapsed_ns, "same_state_diagnostic": diagnostic, "commit_receipt": None}
                decisions.append(row)
                if plan.selected_candidate_id is None:
                    break
                response = command(m.DecisionCommitParameters(kind="decision.commit", plan=plan), expected=snapshot.version)
                row["commit_receipt"] = response.model_dump(mode="json")
                if response.outcome == "rejected":
                    failures.append({"kind": "decision_rejected", "receipt": response.model_dump(mode="json")})
                    break
                selected = next(c for c in plan.candidates if c.candidate_id == plan.selected_candidate_id)
                demand = next(d for d in snapshot.demands if d.demand_id == selected.demand_id)
                if demand.intent_id:
                    newly_granted.add(demand.intent_id)
            # Only unbound jobs require orchestration; terminal settlement is server-side.
            for intent_id in sorted(newly_granted):
                progress = workflow.advance(intent_id, actor=owner_actor(works[intent_id]["owner_id"]))
                receipts.extend(r.model_dump(mode="json") for r in progress.receipts)
            snapshot = kernel.decision_snapshot()
            resource_samples.append({"logical_time": snapshot.now,
                "leased_cpu_millicores": sum(s.resources.cpu_millicores for lease in snapshot.leases for s in lease.placement.slices),
                "leased_gpu_units": sum(s.resources.gpu_count for lease in snapshot.leases for s in lease.placement.slices),
                "lease_count": len(snapshot.leases)})
            if (not snapshot.leases and not snapshot.notices
                    and not any(w["arrived_at"] > tick for w in inputs["works"])
                    and not any(e["at"] > tick for e in inputs["events"])):
                # No occupancy or future external arrival can change the selected action.
                # The next meaningful observation is H; expiry processing remains A-owned.
                fast_forward_at = tick
    except Exception as exc:
        failures.append({"kind": "runner_exception", "type": type(exc).__name__, "detail": str(exc)})
    finally:
        final = read()
        inventory = kernel.decision_snapshot().model_dump(mode="json")
        kernel.store.close()
    return {"snapshot": final, "receipts": receipts, "commands": commands, "decisions": decisions,
            "failures": failures, "resource_samples": resource_samples, "external_injections": injections,
            "final_decision_snapshot": inventory, "elapsed_wall_seconds": time.perf_counter()-wall_started}


def summarize_run(inputs, result, run_id, policy, input_sha256):
    intents = {i.intent_id: i for i in result["snapshot"].intents}
    allocations = {a.allocation_id: a for a in result["snapshot"].allocations}
    rows = []
    first_grants = {}
    for event in result["snapshot"].events:
        if event.event_type == "allocation.granted":
            for entity in event.entities:
                if entity.kind == "allocation":
                    first_grants.setdefault(entity.entity_id, event.logical_time)
    jobs = {j.job_id: j for j in result["snapshot"].jobs}
    for work in inputs["works"]:
        intent = intents.get(work["intent_id"])
        status = intent.state if intent and intent.state in {"completed", "failed", "cancelled"} else "unfinished"
        attempts = list(intent.attempts) if intent else []
        first_start = min((a.started_at for a in attempts if a.started_at is not None), default=None)
        wasted = sum(max(0., min(a.finished_at or inputs["horizon"], inputs["horizon"])-a.started_at)
                     for a in attempts if a.started_at is not None and a.state in {"stopped", "failed", "cancelled"})
        first_grant = min((first_grants[a] for a in intent.allocation_ids if a in first_grants), default=None) if intent else None
        first_job = min((jobs[j].created_at for j in intent.job_ids if j in jobs), default=None) if intent else None
        terminal_time = intent.result.recorded_at if intent and intent.result else inputs["horizon"]
        rows.append({"intent_id": work["intent_id"], "owner_id": work["owner_id"], "arrived_at": work["arrived_at"],
                     "status": status, "completed_at": intent.result.recorded_at if status == "completed" else None,
                     "cancelled_at": intent.result.recorded_at if status == "cancelled" else None,
                     "effective_service": 1. if status == "completed" and intent.result.recorded_at <= inputs["horizon"] else 0.,
                     "first_attempt_start": first_start, "admission_to_first_attempt_seconds": None if first_start is None else first_start-work["arrived_at"],
                     "first_allocation_grant": first_grant,
                     "allocation_wait_seconds": None if first_grant is None else first_grant-work["arrived_at"],
                     "job_lifecycle_truncated_seconds": None if first_job is None else min(terminal_time, inputs["horizon"])-first_job,
                     "completion_right_censored": status != "completed" or terminal_time > inputs["horizon"],
                     "attempt_count": len(attempts), "wasted_attempt_seconds": wasted,
                     "allocation_states": [allocations[a].state for a in intent.allocation_ids if a in allocations] if intent else [],
                     "failure": intent.result.failure.model_dump(mode="json") if status == "failed" else None})
    events = Counter(e.event_type for e in result["snapshot"].events)
    diagnostics = [d["same_state_diagnostic"] for d in result["decisions"] if d["same_state_diagnostic"]]
    legal_scores = [[s["value"] for c in d["plan"]["candidates"] if c["feasibility"] == "legal"
                     for s in c["scores"] if s["name"] == "observed_opportunity_loss"] for d in result["decisions"]]
    samples = result["resource_samples"]
    integrated = {k: sum(a[k]*(b["logical_time"]-a["logical_time"]) for a, b in zip(samples, samples[1:]))
                  for k in ("leased_cpu_millicores", "leased_gpu_units")}
    def quantiles(values):
        values = sorted(values)
        if not values:
            return {"count": 0, "p50": None, "p95": None, "p99": None}
        import math
        return {"count": len(values), **{name: values[max(0, math.ceil(q*len(values))-1)]
            for name, q in (("p50", .5), ("p95", .95), ("p99", .99))}}
    truncated = [max(0., min(w["completed_at"] if w["status"] == "completed" else inputs["horizon"], inputs["horizon"])-w["arrived_at"])
                 for w in rows if w["arrived_at"] <= inputs["horizon"]]
    return dict(run_id=run_id, policy=policy, scenario_family=inputs["scenario_family"], seed=inputs["seed"],
        input_sha256=input_sha256, model_version=inputs["model"]["name"], horizon=inputs["horizon"],
        observed_until=result["snapshot"].now, cancellation_policy=inputs["cancellation_policy"], service_unit=inputs["service_unit"],
        service_definition="One successfully completed fixed-duration model work is one unit; partial/wasted processing counts as zero effective service.",
        works=rows, tail_metrics={"truncated_completion_seconds_all_arrived": quantiles(truncated),
            "completion_censored_count": sum(w["completion_right_censored"] for w in rows),
            "allocation_wait_seconds_observed_grants_only": quantiles([w["allocation_wait_seconds"] for w in rows if w["allocation_wait_seconds"] is not None]),
            "allocation_never_granted_count": sum(w["allocation_wait_seconds"] is None for w in rows),
            "warning": "Truncated completion quantiles are not uncensored completion estimates; failed/unfinished work stays at H."}, runner_failures=result["failures"], event_counts=dict(events),
        instrumentation=dict(plan_count=len(result["decisions"]),
            plan_elapsed_ns=sum(d["elapsed_ns"] for d in result["decisions"]),
            opportunity_nonzero_decisions=sum(any(v > 0 for v in scores) for scores in legal_scores),
            opportunity_zero_decisions=sum(bool(scores) and all(v == 0 for v in scores) for scores in legal_scores),
            action_changed_from_b2=sum(d["action_changed_from_b2"] for d in diagnostics),
            baseline_ablation_identity_violations=sum(not d["off_matches_b2"] for d in diagnostics),
            diagnostic_elapsed_ns=sum(d["diagnostic_elapsed_ns"] for d in diagnostics)),
        occupancy_integrals=integrated, occupancy_definition="Left-hand sampled model leased capacity times logical seconds. Internal due boundaries within dispatcher intervals can differ by up to the sampling interval; not exact occupancy or measured utilization.",
        scope=inputs["scope"], limitations=inputs["limitations"])
