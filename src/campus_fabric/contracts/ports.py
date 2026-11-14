"""Synchronous internal boundaries, intentionally without implementations or HTTP routes."""

from typing import Protocol

from .models import (
    AuthenticatedActor,
    CommandEnvelope,
    CommitReceipt,
    DecisionPlan,
    DecisionSnapshot,
    EventCursor,
    ExecutorCommand,
    ExecutorEvent,
    ReadQuery,
    ReadSnapshot,
    VersionStamp,
    WorkIntentRegistration,
)


class CommandPort(Protocol):
    def commit(self, command: CommandEnvelope) -> CommitReceipt:
        """Recheck authority/version/resources, then atomically persist state and events.

        Idempotency is scoped to authenticated owner, operation and key. Reuse with
        conflicting parameters is rejected. Failed plan checks cannot debit resources.
        """
        ...


class SnapshotPort(Protocol):
    def decision_snapshot(self) -> DecisionSnapshot:
        """Return an immutable causal prefix from one authoritative revision."""
        ...


class ReadPort(Protocol):
    def read_snapshot(self, query: ReadQuery) -> ReadSnapshot:
        """Apply authorization before projection; return one revision and epoch."""
        ...


class PolicyPort(Protocol):
    def plan(self, snapshot: DecisionSnapshot) -> DecisionPlan:
        """Compute without state mutation, evaluator access or future scenario inputs."""
        ...


class ExecutorPort(Protocol):
    def dispatch(self, command: ExecutorCommand) -> None:
        """Request work; dispatch success is not evidence of stop/save/completion."""
        ...

    def events(self, after: EventCursor | None = None) -> tuple[ExecutorEvent, ...]:
        """Return callbacks for kernel validation; stale generations remain rejected."""
        ...


class WorkflowPort(Protocol):
    def register_intent(
        self,
        registration: WorkIntentRegistration,
        expected_version: VersionStamp,
        *,
        actor: AuthenticatedActor,
        request_id: str,
    ) -> CommitReceipt:
        """Atomically persist immutable work inputs and the kernel-stamped first arrival.

        Reusing an intent ID with the same parameters returns the durable result;
        conflicting parameters fail. Actor ownership is rechecked by the kernel.
        Implementations submit workflow.register_intent to the same command kernel.
        """
        ...

    def create_allocation_and_bind(
        self,
        intent_id: str,
        stage_id: str,
        expected_version: VersionStamp,
        *,
        actor: AuthenticatedActor,
        request_id: str,
    ) -> CommitReceipt:
        """Atomically allocate, bind the stored intent, and persist the stage receipt.

        Use the stored specification; identical stage retries return the original
        result and conflicting stage parameters fail. This submits the internal
        workflow.create_allocation_and_bind command to the same authoritative kernel.
        """
        ...

    def create_job_and_bind(
        self,
        intent_id: str,
        stage_id: str,
        expected_version: VersionStamp,
        *,
        actor: AuthenticatedActor,
        request_id: str,
    ) -> CommitReceipt:
        """Create the job, bind the stored intent, and persist its stage receipt atomically.

        The stage's specification is already durably stored; recheck actor ownership. A retry
        of the same intent/stage returns its original result. Conflicting stage data
        is rejected. Persist the stage receipt before returning success. This is an
        internal workflow guarantee, not a new public JobRequest/header requirement.
        It submits workflow.create_job_and_bind to the same authoritative kernel.
        """
        ...
