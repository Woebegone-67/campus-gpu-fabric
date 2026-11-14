"""Local B assembly over A's CP1 kernel; no second ledger or automatic clock.

The worker discovers only persisted WorkIntents through ReadPort. Owner identities
below are trusted service delegation from those registered intents, never HTTP
role claims. Every stage still passes A's command authorization/version checks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from campus_fabric.api.queries import make_kernel_services, register_queries
from campus_fabric.app import create_app as create_core_app
from campus_fabric.contracts import models as m
from campus_fabric.policies import make_policy
from campus_fabric.projections.read_models import EvidenceRead
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator, WorkflowReadError

ROOT = Path(__file__).resolve().parents[3]
LOG = logging.getLogger(__name__)
OBSERVER = m.AuthenticatedActor(actor_id="b-workflow-observer", role="administrator")
POLICY_ACTOR = m.AuthenticatedActor(actor_id="b-policy-worker", role="system")


class WorkflowWorker:
    """Replay durable bindings; cached failures only suppress repeated attempts.

    A failed stage waits for a relevant intent/lease change, not unrelated tenant
    revisions. A stale-version race can retry on the next tick. A process restart
    re-reads all bindings; repeated stages cannot create a second object.
    """

    def __init__(self, kernel, *, policy_name: str | None = None):
        self.kernel = kernel
        self.workflow = WorkflowOrchestrator(kernel, kernel)
        self.policy = make_policy(policy_name) if policy_name else None
        self.blocked: dict[tuple[int, str], str] = {}
        self.transport_retries: dict[tuple[int, str], tuple[int, float]] = {}

    def tick(self):
        snapshot = self.kernel.read_snapshot(m.ReadQuery(actor=OBSERVER, global_scope=True))
        live = set()
        for intent in snapshot.intents:
            if (intent.state in {"completed", "failed", "cancelled", "manual_action"}
                    or intent.job_ids or any(stage.state == "failed" for stage in intent.stages)):
                continue
            key = (snapshot.version.epoch, intent.intent_id)
            live.add(key)
            signature = json.dumps({
                "intent": intent.model_dump(mode="json"),
                "allocations": [a.model_dump(mode="json") for a in snapshot.allocations
                                if a.allocation_id in intent.allocation_ids],
            }, sort_keys=True)
            if self.blocked.get(key) == signature:
                continue
            retries, retry_at = self.transport_retries.get(key, (0, 0.0))
            if time.monotonic() < retry_at:
                continue
            actor = m.AuthenticatedActor(actor_id="b-workflow", role="researcher",
                                         owner_id=intent.owner_id)
            try:
                progress = self.workflow.advance(intent.intent_id, actor=actor,
                    expected_epoch=snapshot.version.epoch,
                    allocation_stage_id="allocate-v1", job_stage_id="job-v1")
            except (WorkflowReadError, PermissionError):
                LOG.warning("Work %s could not be reconciled from its authorized snapshot",
                            intent.intent_id)
                self.blocked[key] = signature
                continue
            if progress.state == "unknown" and retries < 3:
                self.transport_retries[key] = (retries + 1, time.monotonic() + 2 ** retries)
                continue
            self.transport_retries.pop(key, None)
            if progress.state in {"blocked", "partial_success", "unknown"}:
                stale = any(r.error and r.error.code == "stale_version" for r in progress.receipts)
                if not stale:
                    self.blocked[key] = signature
                    LOG.warning("Work %s requires reconciliation: %s", intent.intent_id,
                                progress.state)
        self.blocked = {k: v for k, v in self.blocked.items() if k in live}
        self.transport_retries = {k: v for k, v in self.transport_retries.items() if k in live}
        # Bound each pass. Never commit empty/defer-only plans: they can change
        # revision without progress and keep an idle scheduler spinning forever.
        if self.policy:
            for _ in range(32):
                decision = self.kernel.decision_snapshot()
                if not decision.demands:
                    break
                plan = self.policy.plan(decision)
                if plan.selected_candidate_id is None:
                    break
                receipt = self.kernel.commit(m.CommandEnvelope(
                    request_id=f"apply-{plan.decision_id}", actor=POLICY_ACTOR,
                    operation="decision.commit", expected_version=plan.snapshot_version,
                    parameters=m.DecisionCommitParameters(kind="decision.commit", plan=plan),
                ))
                if receipt.outcome == "rejected":
                    break


def create_app(*, worker_enabled=True, **core_options):
    app = create_core_app(**core_options)
    kernel = app.state.kernel
    semantics = json.loads((ROOT / "configs/semantics.json").read_text())
    origin = next(rule["value"]["clock_origin"] for rule in semantics["rules"]
                  if rule["id"] == "bootstrap_epoch")
    registry = json.loads((ROOT / "configs/checkpoints.json").read_text())

    def evidence(actor):
        snapshot = kernel.read_snapshot(m.ReadQuery(actor=actor,
                                                   global_scope=actor.role == "administrator"))
        # The joint acceptance registry is NOT evidence of execution. Until
        # version-bound joint receipts are registered, every target stays NOT_RUN.
        checks = tuple(m.EvidenceCheck(target_id=check["target_id"], status="NOT_RUN",
            expected=f"{check['id']}: {check['expected']}")
            for name, checkpoint in registry["checkpoints"].items() if name != "CP0"
            for check in checkpoint["required_checks"])
        return EvidenceRead(viewer=actor, version=snapshot.version, now=snapshot.now,
                            checks=checks, build_matches=None)

    services = make_kernel_services(kernel, app.state.identity, clock_origin=origin,
                                    evidence_reader=evidence)
    register_queries(app, services, replace_existing=True)
    mode = core_options.get("scheduling_mode") or os.environ.get("FABRIC_SCHEDULING_MODE", "builtin")
    policy_name = os.environ.get("FABRIC_B_POLICY", "B0") if mode == "external" else None
    worker = WorkflowWorker(kernel, policy_name=policy_name)
    app.state.workflow_worker = worker

    async def run_worker():
        while True:
            try:
                await asyncio.to_thread(worker.tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Workflow pass failed; persisted work is preserved")
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(run_worker()) if worker_enabled else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app.router.lifespan_context = lifespan
    return app
