"""Bounded CP2 development scenarios, isolated from every operating instance.

Only the runner owns future inputs. Policies receive immutable current snapshots.
These are logical-model development records, never a CP3 result or a T-REAL pass.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import random
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.policies import make_policy
from campus_fabric.projections.read_models import OwnedEvidence
from campus_fabric.workflows.acceptance_evidence import _gate_module
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator

POLICIES = ("B0", "B1", "B2", "P")
MAX_CANDIDATES = 512
ARTIFACT_IDS = ("input", "summary", "events", "decisions", "receipts", "final-snapshot", "run-metadata")
ADMIN = m.AuthenticatedActor(actor_id="isolated-experiment-reader", role="administrator")
SYSTEM = m.AuthenticatedActor(actor_id="isolated-experiment-controller", role="system")
TEST_DRIVER = m.AuthenticatedActor(actor_id="isolated-scenario-driver", role="test_driver")
_SCENARIOS = (
    ("cp2-contention-v1", "异构资源竞争", "当前已知需求竞争与放置解释的小规模研发样例。", "heterogeneous-contention", 8),
    ("cp2-recall-v1", "贡献者收回与接续", "真实逻辑内核的保存报告、停止、等待和同工作接续。", "contributor-recall", 3),
    ("cp2-negative-control-v1", "失败与未完成对照", "包含无法满足的需求和超时；失败与未完成留在结果中。", "negative-control", 4),
    ("cp2-node-loss-v1", "节点故障", "仅在独立临时实例注入节点不可用，保留实际失败。", "node-loss", 3),
)


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _atomic(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def catalog():
    return [{"scenario_id": key, "label": label, "description": detail,
             "scenario_family": family, "execution_mode": "logical_simulation", "horizon": 40,
             "work_count": count, "policies": list(POLICIES), "seed_min": 0, "seed_max": 9999,
             "max_candidates": MAX_CANDIDATES} for key, label, detail, family, count in _SCENARIOS]


def scenario_input(root: Path, scenario_id: str, seed: int):
    spec = next((s for s in catalog() if s["scenario_id"] == scenario_id), None)
    if spec is None:
        raise ValueError("Unknown registered scenario")
    if type(seed) is not int or not 0 <= seed <= 9999:
        raise ValueError("seed must be an integer between 0 and 9999")
    rng = random.Random(seed)
    works = []
    for index in range(spec["work_count"]):
        model = rng.choice(("any", "A10", "A100")) if "contention" in scenario_id else "A10"
        if "negative" in scenario_id and index == 0:
            model = "H100"
        works.append({"intent_id": f"sample-work-{index}", "owner_id": f"sample-owner-{index % 3}",
                      "arrived_at": float(index // 3 if "contention" in scenario_id else 0),
                      "gpu_model": model, "timeout_seconds": 3 if "negative" in scenario_id and index == 1 else 30})
    events = []
    if "recall" in scenario_id:
        events = [{"at": 2, "kind": "recall", "device_ids": ["sim-a10-gpu0"],
                   "notice_seconds": 4, "duration_seconds": 12}]
    if "node-loss" in scenario_id:
        events = [{"at": 4, "kind": "node_state", "node_id": "sim-a10", "state": "NotReady"},
                  {"at": 15, "kind": "node_state", "node_id": "sim-a10", "state": "Ready"}]
    return {"scenario_id": scenario_id, "scenario_family": spec["scenario_family"], "version": "0.2.0",
            "seed": seed, "horizon": spec["horizon"], "tick_seconds": 1, "works": works,
            "events": events, "inventory": json.loads((root / "configs/cp1_inventory.json").read_text())["nodes"],
            "model": {"name": "constant-work-v1", "duration_seconds": 10, "execution_mode": "logical_simulation"},
            "recovery_rule": "Report ready during an open checkpoint window, request resume once per stopped generation; all policies share this rule.",
            "cancellation_policy": "count_until_horizon", "service_unit": "completed_work_equivalents",
            "scope": "CP2 development sample; not CP3 validation or real CPU/GPU execution"}


def _registration(work):
    return m.WorkIntentRegistration(intent_id=work["intent_id"], owner_id=work["owner_id"],
        execution_mode="logical_simulation", settlement_action="release_on_terminal",
        specification=m.WorkSpecification(allocation=m.AllocationCreateParameters(
            kind="allocation.create", owner_id=work["owner_id"], ttl_seconds=300,
            preemption_mode="checkpoint", checkpoint_timeout_seconds=3, max_restarts=2,
            request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                gpu=m.GPURequest(count=1, model=work["gpu_model"], memory_mib=1024))),
            job=m.JobSpecification(image="declared-logical-model", command=("no-image-execution",),
                                   timeout_seconds=work["timeout_seconds"], max_retries=0)))


def _actor(owner):
    return m.AuthenticatedActor(actor_id=f"isolated-{owner}", role="researcher", owner_id=owner)


def execute_scenario(inputs: dict, policy_name: str, *, db_path: Path):
    """Internal runner, not a browser input API. Return actual receipts and state."""
    if policy_name not in POLICIES:
        raise ValueError("Unknown policy")
    if len(inputs["works"]) > 32 or not 1 <= inputs["horizon"] <= 120:
        raise ValueError("Scenario exceeds bounded CP2 development budget")
    kernel = Kernel(db_path, inventory=tuple(m.NodeInventory.model_validate_json(json.dumps(row)) for row in inputs["inventory"]),
                    scheduling_mode="external", simulation_duration_seconds=inputs["model"]["duration_seconds"])
    workflow = WorkflowOrchestrator(kernel, kernel)
    policy = make_policy(policy_name, max_candidates=MAX_CANDIDATES)
    receipts, plans, failures = [], [], []
    registered, requested_resume = set(), set()
    command_number = 0
    def command(parameters, actor=SYSTEM, expected=None):
        nonlocal command_number
        command_number += 1
        receipt = kernel.commit(m.CommandEnvelope(request_id=f"scenario-command-{command_number}", actor=actor,
            operation=parameters.kind, parameters=parameters, expected_version=expected))
        receipts.append(receipt.model_dump(mode="json"))
        return receipt
    try:
        for tick in range(inputs["horizon"] + 1):
            if tick:
                receipt = command(m.ClockAdvanceParameters(kind="clock.advance", advance_seconds=1., source="manual"))
                if receipt.outcome == "rejected":
                    raise RuntimeError("isolated clock advance rejected")
            for event in inputs["events"]:
                if event["at"] != tick:
                    continue
                if event["kind"] == "recall":
                    parameters = m.RecallParameters(kind="contribution.recall", device_ids=tuple(event["device_ids"]),
                        notice_seconds=event["notice_seconds"], duration_seconds=event["duration_seconds"])
                    actor = m.AuthenticatedActor(actor_id="isolated-supplier", role="contributor", owner_id="supplier")
                else:
                    parameters = m.NodeStateParameters(kind="test.node_state", node_id=event["node_id"], state=event["state"])
                    actor = TEST_DRIVER
                receipt = command(parameters, actor)
                if receipt.outcome == "rejected":
                    raise RuntimeError("registered external scenario event rejected")
            for work in inputs["works"]:
                if work["arrived_at"] == tick:
                    result = workflow.start(_registration(work), kernel.decision_snapshot().version,
                        actor=_actor(work["owner_id"]), request_id=f"arrival-{work['intent_id']}")
                    receipts.extend(r.model_dump(mode="json") for r in result.receipts)
                    registered.add(work["intent_id"])
            current = kernel.read_snapshot(m.ReadQuery(actor=ADMIN, global_scope=True))
            for allocation in current.allocations:
                owner = _actor(allocation.owner_id)
                if allocation.state == "PreemptRequested" and allocation.checkpoint is None:
                    command(m.CheckpointReportParameters(kind="allocation.checkpoint",
                        allocation_id=allocation.allocation_id, status="ready"), owner)
            current = kernel.read_snapshot(m.ReadQuery(actor=ADMIN, global_scope=True))
            for allocation in current.allocations:
                key = (allocation.allocation_id, allocation.restart_count)
                if allocation.state == "Preempted" and key not in requested_resume:
                    requested_resume.add(key)
                    command(m.AllocationTargetParameters(kind="allocation.resume", allocation_id=allocation.allocation_id),
                            _actor(allocation.owner_id))
            for _ in range(32):
                snapshot = kernel.decision_snapshot()
                if not snapshot.demands:
                    break
                before = time.perf_counter_ns()
                plan = policy.plan(snapshot)  # No scenario, future arrival, seed or hidden duration argument.
                elapsed = time.perf_counter_ns() - before
                diagnostic_start = time.perf_counter_ns()
                baseline_id = (make_policy("B2", max_candidates=MAX_CANDIDATES).plan(snapshot).selected_candidate_id
                               if policy_name == "P" else None)
                plans.append({"snapshot": snapshot.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
                              "elapsed_ns": elapsed,
                              "same_snapshot_b2_selected_id": baseline_id,
                              "same_snapshot_diagnostic_ns": time.perf_counter_ns() - diagnostic_start})
                if plan.selected_candidate_id is None:
                    break
                receipt = command(m.DecisionCommitParameters(kind="decision.commit", plan=plan), expected=snapshot.version)
                if receipt.outcome == "rejected":
                    failures.append({"kind": "decision_rejected", "receipt": receipt.model_dump(mode="json")})
                    break
            for work in inputs["works"]:
                if work["intent_id"] not in registered:
                    continue
                current = kernel.read_snapshot(m.ReadQuery(actor=_actor(work["owner_id"])))
                if not any(i.intent_id == work["intent_id"] for i in current.intents):
                    continue
                result = workflow.advance(work["intent_id"], actor=_actor(work["owner_id"]))
                receipts.extend(r.model_dump(mode="json") for r in result.receipts)
    except Exception as exc:
        failures.append({"kind": "runner_exception", "type": type(exc).__name__, "detail": str(exc)})
    finally:
        final = kernel.read_snapshot(m.ReadQuery(actor=ADMIN, global_scope=True))
        kernel.store.close()
    return {"snapshot": final, "receipts": receipts, "decisions": plans, "failures": failures,
            "policy": (
                m.VersionedArtifact.model_validate(plans[0]["plan"]["policy"]) if plans else
                policy.plan(m.DecisionSnapshot(version=m.VersionStamp(epoch=0, revision=0), now=0.,
                    execution_mode="logical_simulation")).policy)}


def _summary(inputs, result, run_id, policy):
    intents = {i.intent_id: i for i in result["snapshot"].intents}
    works = []
    for work in inputs["works"]:
        intent = intents.get(work["intent_id"])
        status = intent.state if intent and intent.state in {"completed", "failed", "cancelled"} else "unfinished"
        works.append({"intent_id": work["intent_id"], "owner_id": work["owner_id"],
            "arrived_at": work["arrived_at"], "status": status,
            "completed_at": intent.result.recorded_at if status == "completed" else None,
            "cancelled_at": intent.result.recorded_at if status == "cancelled" else None,
            "effective_service": 1. if status == "completed" else 0.,
            "failure": intent.result.failure.model_dump(mode="json") if status == "failed" else None})
    return {"run_id": run_id, "policy": policy, "scenario_family": inputs["scenario_family"],
            "seed": inputs["seed"], "input_sha256": _digest(_json(inputs)), "model_version": "constant-work-v1:10s",
            "horizon": inputs["horizon"], "observed_until": result["snapshot"].now,
            "cancellation_policy": inputs["cancellation_policy"], "service_unit": inputs["service_unit"],
            "works": works, "runner_failures": result["failures"],
            "scope": inputs["scope"], "service_definition": "Only successfully completed model work counts as one; partial processing is not claimed as effective service."}


class ExperimentService:
    """One bounded worker, persistent private run records, administrator-only reads."""
    def __init__(self, root: Path, output_root: Path):
        self.root, self.output_root = Path(root).resolve(), Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._file_lock = (self.output_root / ".service.lock").open("a+")
        try:
            fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file_lock.close()
            raise ValueError("Another experiment service owns this output directory") from None
        self._closed = False
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="isolated-experiment")
        self._records, self._futures = {}, {}
        for path in sorted(self.output_root.glob("experiment-*/evidence.json")):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            try:
                record = m.EvidenceRun.model_validate_json(path.read_bytes())
                if path.parent.name != record.run_id or not re.fullmatch(r"experiment-[a-f0-9]{32}", record.run_id):
                    continue
                if record.status == "running":
                    record = record.model_copy(update={"status": "failed", "exceptions": (
                        m.ErrorDetail(code="internal_error", detail="Previous process stopped before this run finished."),)})
                    _atomic(path, record.model_dump_json(indent=2).encode())
                self._records[record.run_id] = record
            except (OSError, ValueError):
                continue

    @staticmethod
    def _authorize(actor):
        if actor.role != "administrator":
            raise PermissionError("Only administrators can access isolated experiment records")

    def catalog(self):
        return catalog()

    def list_runs(self, actor):
        self._authorize(actor)
        with self._lock:
            return tuple(OwnedEvidence(owner_id="isolated-experiments", run=r) for r in self._records.values())

    def submit(self, *, actor, scenario_id, policy, seed=0):
        self._authorize(actor)
        if policy not in POLICIES:
            raise ValueError("Unknown registered policy")
        inputs = scenario_input(self.root, scenario_id, seed)
        with self._lock:
            if self._closed:
                raise ValueError("Experiment service is closed")
            if sum(r.status == "running" for r in self._records.values()) >= 4:
                raise ValueError("At most four experiment runs may be queued or running")
            if len(self._records) >= 128:
                raise ValueError("This instance has reached its 128-run retention limit")
            run_id = f"experiment-{uuid4().hex}"
            directory = self.output_root / run_id
            directory.mkdir()
            build = _gate_module(self.root).current_build(self.root)
            def artifact(name, data):
                return m.VersionedArtifact(name=name, version="0.2.0", sha256=_digest(data))
            policy_fact = make_policy(policy, max_candidates=MAX_CANDIDATES).plan(m.DecisionSnapshot(
                version=m.VersionStamp(epoch=0, revision=0), now=0., execution_mode="logical_simulation")).policy
            record = m.EvidenceRun(run_id=run_id, run_kind="factual",
                build=m.VersionedArtifact(name="campus-gpu-fabric", version=build["git_commit"] or "uncommitted",
                                          sha256=build["source_sha256"]),
                semantics=artifact("semantics", (self.root / "configs/semantics.json").read_bytes()),
                model=artifact("constant-work-v1", _json(inputs["model"])), policy=policy_fact,
                scenario=artifact(scenario_id, _json(inputs)), calculator=artifact("work-cohort-v1",
                    (self.root / "experiments/compare_runs.py").read_bytes()), seed=seed,
                inputs=(m.VersionedArtifact(name="input", version="0.2.0", sha256=_digest(_json(inputs)),
                                            uri=f"experiment://{run_id}/input"),),
                window=m.EvidenceWindow(start=0., end=float(inputs["horizon"])),
                environment=m.EvidenceEnvironment(platform=platform.platform(), python_version=platform.python_version(),
                    cpu_count=os.cpu_count() or 1, memory_mib=0, execution_mode="logical_simulation"),
                status="running")
            _atomic(directory / "input.json", _json(inputs))
            self._save(record)
            self._futures[run_id] = self._pool.submit(self._execute, record, inputs, build)
            return record

    def _save(self, record):
        with self._lock:
            _atomic(self.output_root / record.run_id / "evidence.json", record.model_dump_json(indent=2).encode())
            self._records[record.run_id] = record

    def _execute(self, record, inputs, initial_build):
        directory = self.output_root / record.run_id
        outputs, exceptions = [], []
        try:
            with tempfile.TemporaryDirectory(prefix="isolated-kernel-", dir=directory) as runtime:
                result = execute_scenario(inputs, record.policy.name, db_path=Path(runtime) / "kernel.sqlite")
            payloads = {"summary": _summary(inputs, result, record.run_id, record.policy.name),
                "events": [e.model_dump(mode="json") for e in result["snapshot"].events],
                "decisions": result["decisions"], "receipts": result["receipts"],
                "final-snapshot": result["snapshot"].model_dump(mode="json"),
                "run-metadata": {"build": initial_build, "max_candidates": MAX_CANDIDATES,
                    "failures": result["failures"], "environment_memory_mib": "not measured",
                    "scope": inputs["scope"], "recovery_cost_component": "inactive; no verified cost model",
                    "policy_observations": {
                        "decision_count": len(result["decisions"]),
                        "opportunity_nonzero_decisions": sum(any(s["name"] == "observed_opportunity_loss" and s["value"] > 0
                            for c in d["plan"]["candidates"] for s in c["scores"]) for d in result["decisions"]),
                        "same_snapshot_action_changes_vs_b2": sum(d["plan"]["selected_candidate_id"] != d["same_snapshot_b2_selected_id"]
                            for d in result["decisions"]) if record.policy.name == "P" else None,
                        "total_policy_elapsed_ns": sum(d["elapsed_ns"] for d in result["decisions"]),
                        "note": "Same-snapshot action change is a diagnostic, not a causal completion gain."},
                    "execution": "Actual isolated transactional Kernel commands; logical simulation only"}}
            for name, payload in payloads.items():
                content = _json(payload)
                _atomic(directory / f"{name}.json", content)
                outputs.append(m.VersionedArtifact(name=name, version="0.2.0", sha256=_digest(content),
                                                  uri=f"experiment://{record.run_id}/{name}"))
            if result["failures"]:
                exceptions.append(m.ErrorDetail(code="internal_error", detail="Scenario runner recorded failures; inspect its registered outputs."))
            if _gate_module(self.root).current_build(self.root) != initial_build:
                exceptions.append(m.ErrorDetail(code="stale_version", detail="Source build changed while this run was executing."))
        except Exception as exc:
            exceptions.append(m.ErrorDetail(code="internal_error", detail=f"Isolated runner stopped with {type(exc).__name__}; partial files are retained locally."))
        self._save(record.model_copy(update={"status": "failed" if exceptions else "completed",
                                             "outputs": tuple(outputs), "exceptions": tuple(exceptions)}))

    def artifact(self, actor, run_id, artifact_id):
        self._authorize(actor)
        with self._lock:
            record = self._records.get(run_id)
        if record is None or artifact_id not in ARTIFACT_IDS:
            raise KeyError("Unknown registered artifact")
        expected = next((a for a in (*record.inputs, *record.outputs) if a.name == artifact_id), None)
        if expected is None:
            raise KeyError("Artifact is not yet available")
        path = self.output_root / run_id / f"{artifact_id}.json"
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(self.output_root):
            raise ValueError("Artifact path is outside its isolated run")
        content = path.read_bytes()
        if _digest(content) != expected.sha256:
            raise ValueError("Artifact integrity check failed")
        return content, "application/json"

    def wait(self, run_id, timeout=None):
        future = self._futures.get(run_id)
        if future is not None:
            future.result(timeout=timeout)
        with self._lock:
            return self._records[run_id]

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True)
        fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_UN)
        self._file_lock.close()
