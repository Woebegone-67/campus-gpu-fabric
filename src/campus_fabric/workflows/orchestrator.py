"""B's restart-safe *orchestration*, with all durable state owned by A.

An authoritative worker/outbox calls ``advance`` after commits and on restart.
This class has no background timer, database, browser state, automatic retry loop,
or public job POST. Each call performs at most two atomic stages through
WorkflowPort. A must implement their durable receipt and stage identity contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from campus_fabric.contracts.models import (
    AllocationView,
    AuthenticatedActor,
    CommitReceipt,
    ReadQuery,
    ReadSnapshot,
    StageReceipt,
    VersionStamp,
    WorkIntent,
    WorkIntentRegistration,
)
from campus_fabric.contracts.ports import ReadPort, WorkflowPort


class WorkflowReadError(RuntimeError):
    """Authoritative read is missing, stale, or outside the authenticated scope."""


@dataclass(frozen=True)
class WorkflowProgress:
    intent_id: str
    state: Literal["waiting", "running", "partial_success", "blocked", "terminal", "unknown"]
    reason: str
    intent: WorkIntent | None = None
    receipts: tuple[CommitReceipt, ...] = ()
    next_stage_id: str | None = None
    allocation_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "state": self.state,
            "reason": self.reason,
            "intent": self.intent.model_dump(mode="json") if self.intent else None,
            "receipts": [r.model_dump(mode="json") for r in self.receipts],
            "next_stage_id": self.next_stage_id,
            "allocation_id": self.allocation_id,
        }


def _request_id(intent_id: str, stage_id: str) -> str:
    # request_id is for correlation. The kernel's durable idempotency identity is
    # (epoch, authenticated owner, intent_id, stage_id), not this hash alone.
    return "workflow-" + sha256(f"{intent_id}\0{stage_id}".encode()).hexdigest()[:40]


class WorkflowOrchestrator:
    def __init__(self, workflow: WorkflowPort, reads: ReadPort):
        self.workflow = workflow
        self.reads = reads

    def _read(self, intent_id: str, actor: AuthenticatedActor) -> tuple[ReadSnapshot, WorkIntent]:
        if actor.owner_id is None or not actor.can_use_compute:
            raise PermissionError("Work orchestration requires an authenticated work owner")
        snapshot = self.reads.read_snapshot(ReadQuery(actor=actor))
        if snapshot.viewer != actor or snapshot.global_scope:
            raise WorkflowReadError("ReadPort returned the wrong authenticated scope")
        intent = next((item for item in snapshot.intents if item.intent_id == intent_id), None)
        if intent is None or intent.owner_id != actor.owner_id:
            raise WorkflowReadError("Work intent was not found in the authorized read")
        return snapshot, intent

    @staticmethod
    def _stage(intent: WorkIntent, stage_id: str, operation: str) -> StageReceipt | None:
        stage = next((s for s in intent.stages if s.stage_id == stage_id), None)
        if stage is not None and stage.operation != operation:
            raise WorkflowReadError("Stage identity belongs to a different operation")
        return stage

    @staticmethod
    def _allocation(snapshot: ReadSnapshot, intent: WorkIntent) -> AllocationView | None:
        candidates = [a for a in snapshot.allocations if a.allocation_id in intent.allocation_ids]
        if len(candidates) != len(intent.allocation_ids):
            raise WorkflowReadError("ReadPort omitted one or more linked allocations")
        live = [a for a in candidates if a.state not in ("Released", "Expired", "Evicted", "Rejected")]
        if len(live) > 1:
            raise WorkflowReadError("More than one live allocation needs explicit reconciliation")
        return live[0] if live else None

    def start(
        self,
        registration: WorkIntentRegistration,
        expected_version: VersionStamp,
        *,
        actor: AuthenticatedActor,
        request_id: str,
    ) -> WorkflowProgress:
        if actor.owner_id != registration.owner_id or not actor.can_use_compute:
            raise PermissionError("Registration owner must match the authenticated work owner")
        try:
            receipt = self.workflow.register_intent(
                registration, expected_version, actor=actor, request_id=request_id
            )
        except (TimeoutError, ConnectionError):
            return WorkflowProgress(
                intent_id=registration.intent_id, state="unknown",
                reason="Registration response lost; retry the same intent and immutable specification.",
            )
        if receipt.outcome == "rejected":
            return WorkflowProgress(
                intent_id=registration.intent_id, state="blocked",
                reason=receipt.error.detail, receipts=(receipt,),
            )
        result = self.advance(
            registration.intent_id, actor=actor, expected_epoch=receipt.version.epoch
        )
        return WorkflowProgress(
            intent_id=result.intent_id, state=result.state, reason=result.reason,
            intent=result.intent, receipts=(receipt, *result.receipts),
            next_stage_id=result.next_stage_id, allocation_id=result.allocation_id,
        )

    def advance(
        self,
        intent_id: str,
        *,
        actor: AuthenticatedActor,
        allocation_stage_id: str = "allocate",
        job_stage_id: str = "create-job",
        expected_epoch: int | None = None,
    ) -> WorkflowProgress:
        """Resume from durable truth; a deliberate job retry uses a NEW job_stage_id.

        Transport retries retain stage IDs. Failed durable stages are surfaced,
        not hammered in a loop. Stale-version rejections also return immediately.
        Supplying a new job-stage ID cannot bypass an already bound job. Workers
        must pass the epoch from discovery so a reset cannot reuse a queued ID.
        """
        if not allocation_stage_id or not job_stage_id or allocation_stage_id == job_stage_id:
            raise ValueError("Distinct, nonempty allocation and job stage IDs are required")
        snapshot, intent = self._read(intent_id, actor)
        if expected_epoch is not None and snapshot.version.epoch != expected_epoch:
            return WorkflowProgress(
                intent_id=intent_id, state="blocked",
                reason="Epoch changed since discovery; discard the old work item after reset.",
            )
        initial_epoch = snapshot.version.epoch
        receipts: list[CommitReceipt] = []

        def result(state: str, reason: str, stage: str | None = None,
                   allocation: AllocationView | None = None) -> WorkflowProgress:
            return WorkflowProgress(
                intent_id=intent_id, state=state, reason=reason, intent=intent,
                receipts=tuple(receipts), next_stage_id=stage,
                allocation_id=allocation.allocation_id if allocation else None,
            )

        if intent.state in ("completed", "failed", "cancelled"):
            allocation = self._allocation(snapshot, intent)
            leases = [a for a in snapshot.allocations if a.allocation_id in intent.allocation_ids]
            reason = "Work is terminal. Recorded lease states: " + (
                ", ".join(f"{a.allocation_id}={a.state}" for a in leases) or "no lease"
            ) + "."
            should_release = intent.settlement_action == "release_on_terminal" or (
                intent.state == "completed" and intent.settlement_action == "release_on_success"
            )
            if should_release and allocation is not None:
                reason += (
                    " Automatic settlement is kernel-owned; the lease is not yet recorded as "
                    "released. No local workflow release was fabricated."
                )
            return result("terminal", reason, allocation=allocation)
        if intent.job_ids:
            linked = [j for j in snapshot.jobs if j.job_id in intent.job_ids]
            if len(linked) != len(intent.job_ids):
                raise WorkflowReadError("ReadPort omitted one or more linked jobs")
            allocation = self._allocation(snapshot, intent)
            current = {job.attempt for job in linked if job.attempt is not None}
            stopping = any(attempt.ref in current and attempt.state in {"stopping", "quarantined"}
                           for attempt in intent.attempts)
            if stopping or (allocation is not None and allocation.pending_terminal is not None):
                return result("waiting", "Waiting for authoritative CPU stop confirmation; "
                              "resource claims are still held.", allocation=allocation)
            if allocation is not None and allocation.state == "Preempted":
                budget = intent.specification.allocation.max_restarts
                if allocation.restart_count >= budget:
                    return result("blocked", "Restart budget is exhausted; release or cancel "
                                  "the existing work explicitly.", allocation=allocation)
                return result("waiting", "Work is interrupted. An explicit resume action uses "
                              "the same lease and job with a new attempt; no new job is created.",
                              allocation=allocation)
            if allocation is not None and allocation.state == "Pending":
                return result("waiting", "The existing job is waiting for replacement capacity; "
                              "do not create another allocation or job.", allocation=allocation)
            if any(job.state == "Checkpointing" for job in linked):
                return result("waiting", "A save window is active; report, artifact verification "
                              "and applied continuation are separate facts.", allocation=allocation)
            return result(
                "running" if any(j.state == "Running" for j in linked) else "waiting",
                "A job is already bound; observe its lifecycle instead of submitting another job.",
                allocation=allocation,
            )

        allocation = self._allocation(snapshot, intent)
        if intent.allocation_ids and allocation is None:
            return result("blocked", "Existing lease is terminal; an explicit new-work decision is required.")
        if allocation is None:
            stage = self._stage(intent, allocation_stage_id, "allocate")
            if stage is not None:
                if stage.receipt is not None:
                    receipts.append(stage.receipt)
                if stage.state == "failed":
                    return result("blocked", "Allocation stage was rejected; inspect its saved receipt.",
                                  allocation_stage_id)
                if stage.state == "committed":
                    raise WorkflowReadError("Committed allocation stage has no linked allocation")
                return result("waiting", "Allocation stage is still pending in authoritative state.",
                              allocation_stage_id)
            try:
                receipt = self.workflow.create_allocation_and_bind(
                    intent_id, allocation_stage_id, snapshot.version, actor=actor,
                    request_id=_request_id(intent_id, allocation_stage_id),
                )
            except (TimeoutError, ConnectionError):
                return result("unknown", "Allocation response lost; retry this same stage identity.",
                              allocation_stage_id)
            receipts.append(receipt)
            if receipt.outcome == "rejected":
                return result("blocked", receipt.error.detail, allocation_stage_id)
            snapshot, intent = self._read(intent_id, actor)
            if snapshot.version.epoch != initial_epoch:
                return result("blocked", "Epoch changed after allocation; reconcile after reset.")
            allocation = self._allocation(snapshot, intent)
            if allocation is None:
                raise WorkflowReadError("Allocation commit is absent from authoritative read")
            # A second worker can have advanced the workflow after our allocation
            # commit. Do not race it with another job even with a new stage ID.
            if intent.job_ids:
                return result("waiting", "Another worker already bound the job; observe it.",
                              allocation=allocation)

        if allocation.state == "Pending":
            return result("waiting", "Resource lease is queued; job creation waits for allocation.",
                          job_stage_id, allocation)
        if allocation.state != "Allocated":
            return result("blocked", f"Lease state {allocation.state} does not admit a new job.",
                          job_stage_id, allocation)
        if allocation.expires_at is not None and allocation.expires_at <= snapshot.now:
            return result("blocked", "Lease has reached its deadline; do not submit a new job.",
                          job_stage_id, allocation)

        stage = self._stage(intent, job_stage_id, "create_job_and_bind")
        if stage is not None:
            if stage.receipt is not None:
                receipts.append(stage.receipt)
            if stage.state == "failed":
                return result("partial_success",
                              "Lease exists; job stage failed. Retry deliberately with a new job stage, "
                              "or release the existing lease through A's command adapter.",
                              job_stage_id, allocation)
            if stage.state == "committed":
                raise WorkflowReadError("Committed job stage has no bound job")
            return result("waiting", "Job stage is pending in authoritative state.",
                          job_stage_id, allocation)
        try:
            receipt = self.workflow.create_job_and_bind(
                intent_id, job_stage_id, snapshot.version, actor=actor,
                request_id=_request_id(intent_id, job_stage_id),
            )
        except (TimeoutError, ConnectionError):
            return result("unknown", "Job response lost; retry this same stage identity, never public POST /v1/jobs.",
                          job_stage_id, allocation)
        receipts.append(receipt)
        if receipt.outcome == "rejected":
            return result("partial_success", receipt.error.detail, job_stage_id, allocation)
        snapshot, intent = self._read(intent_id, actor)
        if snapshot.version.epoch != initial_epoch:
            return result("blocked", "Epoch changed after job commit; reconcile after reset.")
        if not intent.job_ids:
            raise WorkflowReadError("Job commit is absent from authoritative read")
        linked = [j for j in snapshot.jobs if j.job_id in intent.job_ids]
        if len(linked) != len(intent.job_ids):
            raise WorkflowReadError("ReadPort omitted the committed job")
        return result("running" if any(j.state == "Running" for j in linked) else "waiting",
                      "Job is durably bound; execution status comes from the authoritative snapshot.",
                      allocation=allocation)
