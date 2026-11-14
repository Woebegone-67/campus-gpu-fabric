"""Immutable internal CP0 messages; these are not the frozen HTTP schemas.

Validation checks representation and local semantic consistency. Authorization,
current versions, allocation legality, and atomic effects belong to the kernel.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_SAFE_INTEGER = 9_007_199_254_740_991
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^\S+$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
NonnegativeInt = Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]
PositiveInt = Annotated[int, Field(ge=1, le=MAX_SAFE_INTEGER)]
ResourceAmount = NonnegativeInt
LogicalTime = Annotated[float, Field(ge=0, le=MAX_SAFE_INTEGER, allow_inf_nan=False)]
FiniteNumber = Annotated[
    float, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER, allow_inf_nan=False)
]
SignedInt = Annotated[int, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]
Role = Literal["researcher", "contributor", "administrator", "test_driver", "system"]
ExecutionMode = Literal["logical_simulation", "cpu_reference"]
Operation = Literal[
    "allocation.create",
    "allocation.release",
    "allocation.renew",
    "allocation.checkpoint",
    "allocation.resume",
    "job.create",
    "job.cancel",
    "clock.advance",
    "test.reset",
    "test.contributor_demand",
    "test.node_state",
    "test.fault",
    "contribution.register",
    "contribution.recall",
    "decision.commit",
    "executor.report",
    "workflow.register_intent",
    "workflow.create_allocation_and_bind",
    "workflow.create_job_and_bind",
]


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", validate_default=True)


def _unique(values: tuple[object, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class VersionStamp(ContractModel):
    epoch: NonnegativeInt
    revision: NonnegativeInt

    def is_same_epoch_at_or_before(self, other: VersionStamp) -> bool:
        return self.epoch == other.epoch and self.revision <= other.revision


class AuthenticatedActor(ContractModel):
    """Constructed by a trusted authentication adapter, never trusted from a body."""

    actor_id: Identifier
    role: Role
    owner_id: Identifier | None = None

    @model_validator(mode="after")
    def owner_for_user(self) -> AuthenticatedActor:
        if self.role in ("researcher", "contributor") and self.owner_id is None:
            raise ValueError("user actors require an authenticated owner_id")
        return self


class KeyValue(ContractModel):
    key: Identifier
    value: str


class ErrorDetail(ContractModel):
    code: Literal[
        "invalid_command",
        "unauthorized",
        "forbidden",
        "not_found",
        "stale_version",
        "idempotency_conflict",
        "insufficient_resources",
        "lease_unavailable",
        "budget_exhausted",
        "attempt_stale",
        "stage_conflict",
        "execution_unconfirmed",
        "rate_limited",
        "injected_fault",
        "internal_error",
    ]
    detail: Text
    retryable: bool = False
    current_version: VersionStamp | None = None


class EntityRef(ContractModel):
    kind: Literal[
        "intent", "allocation", "job", "attempt", "node", "gpu", "checkpoint", "decision", "run"
    ]
    entity_id: Identifier


class GPURequest(ContractModel):
    count: Annotated[int, Field(ge=0, le=16)]
    model: Text = "any"
    memory_mib: ResourceAmount = 0
    profile: Text | None = None


class ResourceRequest(ContractModel):
    cpu_millicores: ResourceAmount
    memory_mib: ResourceAmount
    gpu: GPURequest
    ephemeral_storage_mib: ResourceAmount = 0


class ResourceAmounts(ContractModel):
    cpu_millicores: ResourceAmount = 0
    memory_mib: ResourceAmount = 0
    ephemeral_storage_mib: ResourceAmount = 0
    gpu_count: ResourceAmount = 0
    gpu_memory_mib: ResourceAmount = 0


class GPUDevice(ContractModel):
    device_id: Identifier
    physical_gpu_id: Identifier
    model: Text
    memory_mib: ResourceAmount
    profile: Text | None = None
    state: Literal["available", "occupied", "quarantined", "unavailable"]

    @model_validator(mode="after")
    def physical_identity(self) -> GPUDevice:
        if self.profile is None and self.device_id != self.physical_gpu_id:
            raise ValueError("a whole GPU uses its physical GPU identity")
        if self.profile is not None and self.device_id == self.physical_gpu_id:
            raise ValueError("a profile instance requires a distinct physical parent")
        return self


class NodeInventory(ContractModel):
    node_id: Identifier
    contributor_owner_id: Identifier
    state: Literal["Ready", "Draining", "NotReady"]
    capacity: ResourceAmounts
    available: ResourceAmounts
    observed_at: LogicalTime
    devices: tuple[GPUDevice, ...] = ()
    labels: tuple[KeyValue, ...] = ()

    @model_validator(mode="after")
    def local_inventory(self) -> NodeInventory:
        _unique(tuple(item.device_id for item in self.devices), "device IDs")
        _unique(tuple(item.key for item in self.labels), "node label keys")
        for field in ResourceAmounts.model_fields:
            if getattr(self.available, field) > getattr(self.capacity, field):
                raise ValueError(f"available {field} exceeds stated capacity")
        return self


class PlacementSlice(ContractModel):
    node_id: Identifier
    resources: ResourceAmounts
    device_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def unique_devices(self) -> PlacementSlice:
        _unique(self.device_ids, "placement device IDs")
        return self


class Placement(ContractModel):
    slices: Annotated[tuple[PlacementSlice, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_placement(self) -> Placement:
        _unique(tuple(item.node_id for item in self.slices), "placement node IDs")
        _unique(
            tuple(device for item in self.slices for device in item.device_ids),
            "placement device IDs",
        )
        return self


class AttemptRef(ContractModel):
    intent_id: Identifier
    attempt_id: Identifier
    generation: PositiveInt
    allocation_id: Identifier
    job_id: Identifier | None = None

    @model_validator(mode="after")
    def distinct_identity(self) -> AttemptRef:
        if self.intent_id == self.attempt_id:
            raise ValueError("intent and attempt identities must be distinct")
        return self


class CommitReceipt(ContractModel):
    request_id: Identifier
    outcome: Literal["committed", "rejected", "noop"]
    version: VersionStamp
    logical_time: LogicalTime
    objects: tuple[EntityRef, ...] = ()
    event_ids: tuple[Identifier, ...] = ()
    error: ErrorDetail | None = None
    idempotency_replayed: bool = False
    original_request_id: Identifier | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> CommitReceipt:
        _unique(self.event_ids, "receipt event IDs")
        if self.outcome == "rejected" and (self.error is None or self.event_ids):
            raise ValueError("rejected receipts require an error and no committed events")
        if self.outcome != "rejected" and self.error is not None:
            raise ValueError("successful receipts cannot contain a rejection")
        if self.outcome == "committed" and not self.event_ids:
            raise ValueError("committed effects require transaction event IDs")
        if self.idempotency_replayed != (self.original_request_id is not None):
            raise ValueError("idempotency replay requires the original request identity")
        return self


class CheckpointFact(ContractModel):
    checkpoint_id: Identifier
    report_status: Literal["ready", "failed"]
    reported_at: LogicalTime
    artifact_uri: Text | None = None
    verification_status: Literal["unverified", "verified", "failed"] = "unverified"
    verifier_id: Identifier | None = None
    verified_at: LogicalTime | None = None
    continuation_status: Literal["not_attempted", "applied", "failed"] = "not_attempted"
    resumed_attempt_id: Identifier | None = None

    @model_validator(mode="after")
    def separate_evidence_layers(self) -> CheckpointFact:
        if self.verification_status == "verified" and (
            self.report_status != "ready"
            or self.artifact_uri is None
            or self.verifier_id is None
            or self.verified_at is None
        ):
            raise ValueError(
                "verified artifacts require ready report, URI, verifier and verification time"
            )
        if self.verified_at is not None and self.verified_at < self.reported_at:
            raise ValueError("artifact verification cannot precede its report")
        if self.continuation_status == "applied" and (
            self.verification_status != "verified" or self.resumed_attempt_id is None
        ):
            raise ValueError("applied continuation requires verified artifact and resumed attempt")
        return self


class AttemptSnapshot(ContractModel):
    ref: AttemptRef
    state: Literal[
        "waiting",
        "starting",
        "running",
        "checkpointing",
        "stopping",
        "stopped",
        "quarantined",
        "completed",
        "failed",
        "cancelled",
    ]
    created_at: LogicalTime
    started_at: LogicalTime | None = None
    finished_at: LogicalTime | None = None
    checkpoint: CheckpointFact | None = None

    @model_validator(mode="after")
    def chronological_attempt(self) -> AttemptSnapshot:
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("attempt start precedes creation")
        if self.finished_at is not None and self.finished_at < (
            self.started_at if self.started_at is not None else self.created_at
        ):
            raise ValueError("attempt finish precedes its start or creation")
        return self


class StageReceipt(ContractModel):
    stage_id: Identifier
    operation: Literal["allocate", "create_job_and_bind", "resume", "settle"]
    state: Literal["waiting", "committed", "failed"]
    receipt: CommitReceipt | None = None

    @model_validator(mode="after")
    def durable_stage_result(self) -> StageReceipt:
        if self.state != "waiting" and self.receipt is None:
            raise ValueError("completed workflow stages require a durable receipt")
        if self.receipt is not None:
            if self.state == "failed" and self.receipt.outcome != "rejected":
                raise ValueError("failed stages require rejected receipts")
            if self.state == "committed" and self.receipt.outcome == "rejected":
                raise ValueError("committed stages cannot contain rejected receipts")
        return self


class WorkResult(ContractModel):
    kind: Literal["success", "failure", "cancelled"]
    recorded_at: LogicalTime
    artifact_uris: tuple[Text, ...] = ()
    failure: ErrorDetail | None = None

    @model_validator(mode="after")
    def result_kind(self) -> WorkResult:
        if (self.kind == "failure") != (self.failure is not None):
            raise ValueError(
                "failure results require a failure reason; other results cannot have one"
            )
        return self


class WorkIntent(ContractModel):
    intent_id: Identifier
    owner_id: Identifier
    first_arrival_at: LogicalTime
    execution_mode: ExecutionMode
    settlement_action: Literal["keep_lease", "release_on_success", "release_on_terminal"]
    specification: WorkSpecification
    state: Literal[
        "pending", "waiting", "running", "completed", "failed", "cancelled", "manual_action"
    ]
    allocation_ids: tuple[Identifier, ...] = ()
    job_ids: tuple[Identifier, ...] = ()
    attempts: tuple[AttemptSnapshot, ...] = ()
    stages: tuple[StageReceipt, ...] = ()
    result: WorkResult | None = None

    @model_validator(mode="after")
    def stable_work_identity(self) -> WorkIntent:
        if self.specification.allocation.owner_id != self.owner_id:
            raise ValueError("stored work specification must use the intent owner")
        _unique(self.allocation_ids, "intent allocation IDs")
        _unique(self.job_ids, "intent job IDs")
        _unique(tuple(item.stage_id for item in self.stages), "workflow stage IDs")
        _unique(tuple(item.ref.attempt_id for item in self.attempts), "attempt IDs")
        _unique(tuple(item.ref.generation for item in self.attempts), "attempt generations")
        for attempt in self.attempts:
            if attempt.ref.intent_id != self.intent_id:
                raise ValueError("every attempt must belong to the stable intent")
            if attempt.created_at < self.first_arrival_at:
                raise ValueError("attempt creation precedes first intent arrival")
            if attempt.ref.allocation_id not in self.allocation_ids:
                raise ValueError("attempt allocation must be linked to the intent")
            if attempt.ref.job_id is not None and attempt.ref.job_id not in self.job_ids:
                raise ValueError("attempt job must be linked to the intent")
        if self.result is not None and self.result.recorded_at < self.first_arrival_at:
            raise ValueError("work result precedes first intent arrival")
        terminal_kind = {"completed": "success", "failed": "failure", "cancelled": "cancelled"}.get(
            self.state
        )
        if terminal_kind is not None and (self.result is None or self.result.kind != terminal_kind):
            raise ValueError("terminal intent state requires its matching result")
        if terminal_kind is None and self.result is not None:
            raise ValueError("nonterminal intent cannot already have a terminal result")
        return self


class ObservedDatum(ContractModel):
    """An explicitly available scalar; hidden evaluator inputs are not a source."""

    name: Identifier
    value: str | SignedInt | FiniteNumber | bool | None
    source: Literal["request", "observed_event", "declared_profile", "public_metadata", "history"]
    observed_at: LogicalTime
    available_at: LogicalTime
    source_id: Identifier

    @model_validator(mode="after")
    def availability(self) -> ObservedDatum:
        if self.available_at < self.observed_at:
            raise ValueError("data availability cannot precede observation")
        if isinstance(self.value, float) and (
            not -MAX_SAFE_INTEGER <= self.value <= MAX_SAFE_INTEGER
        ):
            raise ValueError("observed numbers must be finite and safely bounded")
        if type(self.value) is int and not -MAX_SAFE_INTEGER <= self.value <= MAX_SAFE_INTEGER:
            raise ValueError("observed integers must be safely bounded")
        return self


class DemandSnapshot(ContractModel):
    demand_id: Identifier
    owner_id: Identifier
    arrived_at: LogicalTime
    request: ResourceRequest
    priority: Annotated[int, Field(ge=0, le=100)] = 0
    intent_id: Identifier | None = None
    allocation_id: Identifier | None = None
    node_selector: tuple[KeyValue, ...] = ()
    known_data: tuple[ObservedDatum, ...] = ()

    @model_validator(mode="after")
    def unique_selectors(self) -> DemandSnapshot:
        _unique(tuple(item.key for item in self.node_selector), "node selector keys")
        return self


class LeaseSnapshot(ContractModel):
    allocation_id: Identifier
    owner_id: Identifier
    starts_at: LogicalTime
    expires_at: LogicalTime
    placement: Placement
    attempt: AttemptRef | None = None

    @model_validator(mode="after")
    def lease_interval(self) -> LeaseSnapshot:
        if self.expires_at < self.starts_at:
            raise ValueError("lease expiry precedes start")
        if self.attempt is not None and self.attempt.allocation_id != self.allocation_id:
            raise ValueError("lease attempt belongs to a different allocation")
        return self


class Occupancy(ContractModel):
    allocation_id: Identifier
    owner_id: Identifier
    placement: Placement
    since: LogicalTime
    status: Literal["reserved", "executing", "stopping", "quarantined"]


class ContributorNotice(ContractModel):
    notice_id: Identifier
    contributor_owner_id: Identifier
    device_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    issued_at: LogicalTime
    deadline: LogicalTime
    unavailable_until: LogicalTime

    @model_validator(mode="after")
    def notice_interval(self) -> ContributorNotice:
        _unique(self.device_ids, "notice device IDs")
        if not self.issued_at <= self.deadline <= self.unavailable_until:
            raise ValueError("notice times must be ordered")
        return self


class DecisionSnapshot(ContractModel):
    version: VersionStamp
    now: LogicalTime
    execution_mode: ExecutionMode
    demands: tuple[DemandSnapshot, ...] = ()
    inventory: tuple[NodeInventory, ...] = ()
    occupancy: tuple[Occupancy, ...] = ()
    leases: tuple[LeaseSnapshot, ...] = ()
    notices: tuple[ContributorNotice, ...] = ()
    history: tuple[ObservedDatum, ...] = ()
    metadata: tuple[ObservedDatum, ...] = ()

    @model_validator(mode="after")
    def causal_cutoff(self) -> DecisionSnapshot:
        for values, label in (
            (tuple(item.demand_id for item in self.demands), "demand IDs"),
            (tuple(item.node_id for item in self.inventory), "node IDs"),
            (tuple(item.allocation_id for item in self.leases), "lease IDs"),
            (tuple(item.notice_id for item in self.notices), "notice IDs"),
        ):
            _unique(values, label)
        visible_times = (
            tuple(item.arrived_at for item in self.demands)
            + tuple(item.observed_at for item in self.inventory)
            + tuple(item.since for item in self.occupancy)
            + tuple(item.starts_at for item in self.leases)
            + tuple(item.issued_at for item in self.notices)
        )
        if any(value > self.now for value in visible_times):
            raise ValueError("snapshot cannot contain facts that have not appeared by now")
        data = (
            self.history
            + self.metadata
            + tuple(item for demand in self.demands for item in demand.known_data)
        )
        if any(item.available_at > self.now for item in data):
            raise ValueError("snapshot data must be available by its causal cutoff")
        return self


class VersionedArtifact(ContractModel):
    name: Identifier
    version: Identifier
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    uri: Text | None = None


class Precondition(ContractModel):
    kind: Literal[
        "owner",
        "allocation_state",
        "lease_valid",
        "resource_available",
        "attempt_generation",
        "budget_remaining",
        "checkpoint_verified",
    ]
    entity: EntityRef
    expected_value: str | NonnegativeInt | bool


class ConstraintViolation(ContractModel):
    constraint: Literal[
        "cpu",
        "memory",
        "storage",
        "gpu_count",
        "gpu_model",
        "gpu_memory",
        "profile",
        "labels",
        "node_state",
        "owner",
        "lease",
        "budget",
        "attempt",
        "quarantine",
    ]
    detail: Text
    entity: EntityRef | None = None


class ScoreComponent(ContractModel):
    name: Identifier
    value: FiniteNumber
    explanation: Text


class CandidateAction(ContractModel):
    candidate_id: Identifier
    action: Literal["allocate", "resume", "defer", "reject"]
    owner_id: Identifier
    demand_id: Identifier
    intent_id: Identifier | None = None
    allocation_id: Identifier | None = None
    attempt: AttemptRef | None = None
    placement: Placement | None = None
    feasibility: Literal["legal", "infeasible", "unexamined"]
    violations: tuple[ConstraintViolation, ...] = ()
    preconditions: tuple[Precondition, ...] = ()
    scores: tuple[ScoreComponent, ...] = ()

    @model_validator(mode="after")
    def candidate_consistency(self) -> CandidateAction:
        if self.feasibility == "infeasible" and not self.violations:
            raise ValueError("infeasible candidates require a violated constraint")
        if self.feasibility == "legal" and self.violations:
            raise ValueError("legal candidates cannot contain constraint violations")
        if (
            self.feasibility == "legal"
            and self.action in ("allocate", "resume")
            and self.placement is None
        ):
            raise ValueError("legal placement actions require a concrete placement")
        if self.attempt is not None and (
            self.intent_id != self.attempt.intent_id
            or self.allocation_id != self.attempt.allocation_id
        ):
            raise ValueError("candidate attempt identity must match its intent and allocation")
        return self


class SearchCoverage(ContractModel):
    scope: Literal["complete", "partial"]
    domain_description: Text
    examined_count: NonnegativeInt
    domain_size: NonnegativeInt | None = None
    truncation_reason: Text | None = None

    @model_validator(mode="after")
    def coverage_consistency(self) -> SearchCoverage:
        if self.domain_size is not None and self.examined_count > self.domain_size:
            raise ValueError("examined count exceeds the declared domain")
        if self.scope == "complete" and (
            self.domain_size is None
            or self.examined_count != self.domain_size
            or self.truncation_reason is not None
        ):
            raise ValueError("complete coverage requires an explicit fully examined domain")
        if self.scope == "partial" and self.truncation_reason is None:
            raise ValueError("partial coverage requires a truncation reason")
        return self


class DecisionPlan(ContractModel):
    decision_id: Identifier
    snapshot_version: VersionStamp
    decision_at: LogicalTime
    policy: VersionedArtifact
    model: VersionedArtifact
    candidates: tuple[CandidateAction, ...]
    coverage: SearchCoverage
    selected_candidate_id: Identifier | None
    rationale: Text
    tie_break_rule: Text
    asserts_pool_infeasible: bool = False

    @model_validator(mode="after")
    def explained_selection(self) -> DecisionPlan:
        ids = tuple(item.candidate_id for item in self.candidates)
        _unique(ids, "candidate IDs")
        examined = tuple(item for item in self.candidates if item.feasibility != "unexamined")
        if self.coverage.examined_count != len(examined):
            raise ValueError("coverage count must match the recorded examined candidates")
        if self.coverage.scope == "complete" and len(examined) != len(self.candidates):
            raise ValueError("complete coverage cannot contain unexamined candidates")
        if self.selected_candidate_id is not None:
            selected = next(
                (
                    item
                    for item in self.candidates
                    if item.candidate_id == self.selected_candidate_id
                ),
                None,
            )
            if selected is None or selected.feasibility != "legal":
                raise ValueError("selection must identify a recorded legal candidate")
        if self.asserts_pool_infeasible:
            if self.coverage.scope != "complete" or any(
                item.feasibility != "infeasible" for item in self.candidates
            ):
                raise ValueError("pool infeasibility requires complete domain exclusion evidence")
            if self.selected_candidate_id is not None:
                raise ValueError("an infeasible pool cannot have a selected candidate")
        return self


class RequestCause(ContractModel):
    kind: Literal["request"]
    request_id: Identifier


class EventCause(ContractModel):
    kind: Literal["event"]
    event_id: Identifier
    sequence: PositiveInt
    version: VersionStamp


class StateChange(ContractModel):
    entity: EntityRef
    before: Text | None
    after: Text


class ResourceDelta(ContractModel):
    node_id: Identifier
    allocation_id: Identifier
    cpu_millicores: Annotated[int, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)] = 0
    memory_mib: Annotated[int, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)] = 0
    ephemeral_storage_mib: Annotated[int, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)] = 0
    device_ids_added: tuple[Identifier, ...] = ()
    device_ids_removed: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def disjoint_devices(self) -> ResourceDelta:
        _unique(self.device_ids_added, "added device IDs")
        _unique(self.device_ids_removed, "removed device IDs")
        if set(self.device_ids_added) & set(self.device_ids_removed):
            raise ValueError("a resource delta cannot both add and remove the same device")
        return self


class CommittedEvent(ContractModel):
    event_id: Identifier
    sequence: PositiveInt
    version: VersionStamp
    logical_time: LogicalTime
    event_type: Identifier
    owner_id: Identifier | None
    entities: tuple[EntityRef, ...]
    cause: Annotated[RequestCause | EventCause, Field(discriminator="kind")]
    attempt: AttemptRef | None = None
    state_changes: tuple[StateChange, ...] = ()
    resource_deltas: tuple[ResourceDelta, ...] = ()
    decision_id: Identifier | None = None
    decision_snapshot_version: VersionStamp | None = None

    @model_validator(mode="after")
    def causal_event(self) -> CommittedEvent:
        if isinstance(self.cause, EventCause):
            if self.cause.event_id == self.event_id or self.cause.sequence >= self.sequence:
                raise ValueError("event cause must precede the committed event")
            if not self.cause.version.is_same_epoch_at_or_before(self.version):
                raise ValueError(
                    "event cause must be in the same epoch at an earlier or equal revision"
                )
        if (self.decision_id is None) != (self.decision_snapshot_version is None):
            raise ValueError("decision identity and snapshot version must appear together")
        if (
            self.decision_snapshot_version is not None
            and not self.decision_snapshot_version.is_same_epoch_at_or_before(self.version)
        ):
            raise ValueError("event cannot commit a future or previous-epoch decision")
        return self


class ExecutorEvent(ContractModel):
    callback_id: Identifier
    version: VersionStamp
    attempt: AttemptRef
    kind: Literal[
        "started",
        "progress",
        "checkpoint_reported",
        "stopped",
        "completed",
        "failed",
        "alive",
        "unknown",
    ]
    mode: ExecutionMode
    observed_at: LogicalTime
    physical_elapsed_seconds: LogicalTime | None = None
    checkpoint: CheckpointFact | None = None
    result_uri: Text | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def executor_evidence(self) -> ExecutorEvent:
        if self.mode == "logical_simulation" and self.physical_elapsed_seconds is not None:
            raise ValueError("logical simulation cannot report measured physical execution time")
        if self.kind == "checkpoint_reported" and self.checkpoint is None:
            raise ValueError("checkpoint callbacks require the checkpoint report")
        if self.kind == "failed" and self.error is None:
            raise ValueError("failed executor callbacks require an error")
        return self


class AllocationCreateParameters(ContractModel):
    kind: Literal["allocation.create"]
    owner_id: Identifier
    request: ResourceRequest
    queue: Text = "default"
    priority: Annotated[int, Field(ge=0, le=100)] = 0
    ttl_seconds: Annotated[int, Field(ge=30, le=3600)] = 300
    preemptible: bool = True
    preemption_mode: Literal["best_effort", "checkpoint", "restartable", "non_preemptible"] = (
        "best_effort"
    )
    checkpoint_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 15
    max_restarts: Annotated[int, Field(ge=0, le=20)] = 2
    node_selector: tuple[KeyValue, ...] = ()

    @model_validator(mode="after")
    def selectors(self) -> AllocationCreateParameters:
        _unique(tuple(item.key for item in self.node_selector), "node selector keys")
        return self


class AllocationTargetParameters(ContractModel):
    kind: Literal["allocation.release", "allocation.resume"]
    allocation_id: Identifier


class AllocationRenewParameters(ContractModel):
    kind: Literal["allocation.renew"]
    allocation_id: Identifier
    ttl_seconds: Annotated[int, Field(ge=30, le=3600)] | None = None


class CheckpointReportParameters(ContractModel):
    kind: Literal["allocation.checkpoint"]
    allocation_id: Identifier
    status: Literal["ready", "failed"]
    artifact_uri: Text | None = None


class JobSpecification(ContractModel):
    image: Text
    command: tuple[str, ...]
    env: tuple[KeyValue, ...] = ()
    timeout_seconds: Annotated[int, Field(ge=1, le=86400)] | None = None
    max_retries: Annotated[int, Field(ge=0, le=10)] | None = None

    @model_validator(mode="after")
    def environment_keys(self) -> JobSpecification:
        _unique(tuple(item.key for item in self.env), "environment keys")
        return self


class JobCreateParameters(JobSpecification):
    kind: Literal["job.create"]
    allocation_id: Identifier


class WorkSpecification(ContractModel):
    """Durable inputs needed to finish the workflow after browser or server restart."""

    allocation: AllocationCreateParameters
    job: JobSpecification


class WorkIntentRegistration(ContractModel):
    """First-arrival time is stamped by the kernel, never supplied by the UI."""

    intent_id: Identifier
    owner_id: Identifier
    execution_mode: ExecutionMode
    settlement_action: Literal["keep_lease", "release_on_success", "release_on_terminal"]
    specification: WorkSpecification

    @model_validator(mode="after")
    def specification_owner(self) -> WorkIntentRegistration:
        if self.specification.allocation.owner_id != self.owner_id:
            raise ValueError("registered work specification must use the intent owner")
        return self


class WorkflowIntentParameters(ContractModel):
    kind: Literal["workflow.register_intent"]
    registration: WorkIntentRegistration


class WorkflowStageParameters(ContractModel):
    kind: Literal["workflow.create_allocation_and_bind", "workflow.create_job_and_bind"]
    intent_id: Identifier
    stage_id: Identifier


class JobCancelParameters(ContractModel):
    kind: Literal["job.cancel"]
    job_id: Identifier


class ClockAdvanceParameters(ContractModel):
    kind: Literal["clock.advance"]
    advance_seconds: Annotated[float, Field(ge=0, le=86400, allow_inf_nan=False)]
    source: Literal["test_control", "wall_clock_driver", "manual"]
    driver_tick_id: Identifier | None = None

    @model_validator(mode="after")
    def clock_source(self) -> ClockAdvanceParameters:
        if self.source == "wall_clock_driver" and self.driver_tick_id is None:
            raise ValueError("wall clock advances require a driver tick identity")
        if self.source != "wall_clock_driver" and self.driver_tick_id is not None:
            raise ValueError("driver tick identities belong only to wall clock advances")
        return self


class ResetParameters(ContractModel):
    kind: Literal["test.reset"]


class RecallParameters(ContractModel):
    kind: Literal["test.contributor_demand", "contribution.recall"]
    device_ids: tuple[Identifier, ...]
    notice_seconds: Annotated[int, Field(ge=1, le=300)]
    duration_seconds: Annotated[int, Field(ge=1, le=3600)]

    @model_validator(mode="after")
    def unique_recalled_devices(self) -> RecallParameters:
        _unique(self.device_ids, "recalled device IDs")
        return self


class NodeStateParameters(ContractModel):
    kind: Literal["test.node_state"]
    node_id: Identifier
    state: Literal["Ready", "Draining", "NotReady"]


class FaultParameters(ContractModel):
    kind: Literal["test.fault"]
    target_operation: Identifier
    mode: Literal["none", "429", "500", "503", "latency"]
    count: Annotated[int, Field(ge=0, le=20)] | None = None
    delay_ms: Annotated[int, Field(ge=0, le=30000)] | None = None


class ContributionRegisterParameters(ContractModel):
    kind: Literal["contribution.register"]
    node: NodeInventory


class DecisionCommitParameters(ContractModel):
    kind: Literal["decision.commit"]
    plan: DecisionPlan


class ExecutorReportParameters(ContractModel):
    kind: Literal["executor.report"]
    event: ExecutorEvent


type CommandParameters = Annotated[
    AllocationCreateParameters
    | AllocationTargetParameters
    | AllocationRenewParameters
    | CheckpointReportParameters
    | JobCreateParameters
    | JobCancelParameters
    | ClockAdvanceParameters
    | ResetParameters
    | RecallParameters
    | NodeStateParameters
    | FaultParameters
    | ContributionRegisterParameters
    | DecisionCommitParameters
    | ExecutorReportParameters
    | WorkflowIntentParameters
    | WorkflowStageParameters,
    Field(discriminator="kind"),
]


class CommandEnvelope(ContractModel):
    request_id: Identifier
    actor: AuthenticatedActor
    operation: Operation
    parameters: CommandParameters
    idempotency_key: Identifier | None = None
    expected_version: VersionStamp | None = None

    @model_validator(mode="after")
    def trusted_identity_and_payload(self) -> CommandEnvelope:
        if self.operation != self.parameters.kind:
            raise ValueError("operation must match the typed parameter kind")
        if (
            isinstance(self.parameters, AllocationCreateParameters)
            and self.parameters.owner_id != self.actor.owner_id
        ):
            raise ValueError("allocation owner must match the authenticated actor owner")
        if (
            isinstance(self.parameters, WorkflowIntentParameters)
            and self.parameters.registration.owner_id != self.actor.owner_id
        ):
            raise ValueError("work intent owner must match the authenticated actor owner")
        if self.operation.startswith("workflow.") and self.expected_version is None:
            raise ValueError("workflow commands require an expected version")
        if (
            isinstance(self.parameters, ContributionRegisterParameters)
            and self.parameters.node.contributor_owner_id != self.actor.owner_id
        ):
            raise ValueError("registered resource owner must match the authenticated actor owner")
        if self.operation.startswith("test.") and self.actor.role != "test_driver":
            raise ValueError("test operations require the isolated test driver role")
        if self.operation.startswith("contribution.") and self.actor.role not in (
            "contributor",
            "administrator",
        ):
            raise ValueError("contribution commands require contributor or administrator authority")
        if self.operation == "clock.advance" and self.actor.role not in (
            "test_driver",
            "system",
            "administrator",
        ):
            raise ValueError("clock commands require a clock-control authority")
        if self.operation in ("decision.commit", "executor.report") and self.actor.role != "system":
            raise ValueError("internal decisions and executor reports require the system role")
        if (
            isinstance(self.parameters, DecisionCommitParameters)
            and self.expected_version != self.parameters.plan.snapshot_version
        ):
            raise ValueError("decision commit expected version must match its snapshot")
        return self


class AllocationView(ContractModel):
    allocation_id: Identifier
    owner_id: Identifier
    state: Literal[
        "Pending",
        "Allocated",
        "Running",
        "PreemptRequested",
        "Preempted",
        "Released",
        "Expired",
        "Evicted",
        "Rejected",
    ]
    request: ResourceRequest
    created_at: LogicalTime
    placement: Placement | None = None
    expires_at: LogicalTime | None = None


class JobView(ContractModel):
    job_id: Identifier
    allocation_id: Identifier
    owner_id: Identifier
    state: Literal[
        "Pending", "Running", "Checkpointing", "Preempted", "Succeeded", "Failed", "Cancelled"
    ]
    created_at: LogicalTime
    attempt: AttemptRef | None = None

    @model_validator(mode="after")
    def attempt_target(self) -> JobView:
        if self.attempt is not None and (
            self.attempt.job_id != self.job_id or self.attempt.allocation_id != self.allocation_id
        ):
            raise ValueError("job view attempt must match its job and allocation")
        return self


class LegalAction(ContractModel):
    operation: Operation
    entity: EntityRef
    explanation: Text


class EventCursor(ContractModel):
    epoch: NonnegativeInt
    after_sequence: NonnegativeInt


class ReadQuery(ContractModel):
    actor: AuthenticatedActor
    global_scope: bool = False
    entities: tuple[EntityRef, ...] = ()
    after: EventCursor | None = None

    @model_validator(mode="after")
    def global_authority(self) -> ReadQuery:
        if self.global_scope and self.actor.role != "administrator":
            raise ValueError("global read scope requires administrator authority")
        return self


class ReadSnapshot(ContractModel):
    version: VersionStamp
    now: LogicalTime
    viewer: AuthenticatedActor
    global_scope: bool = False
    allocations: tuple[AllocationView, ...] = ()
    jobs: tuple[JobView, ...] = ()
    intents: tuple[WorkIntent, ...] = ()
    events: tuple[CommittedEvent, ...] = ()
    decisions: tuple[DecisionPlan, ...] = ()
    legal_actions: tuple[LegalAction, ...] = ()
    next_cursor: EventCursor | None = None

    @model_validator(mode="after")
    def authorized_single_version(self) -> ReadSnapshot:
        if self.global_scope and self.viewer.role != "administrator":
            raise ValueError("global read scope requires administrator authority")
        if not self.global_scope:
            if self.viewer.owner_id is None:
                raise ValueError("owner-scoped reads require an authenticated owner")
            owners = tuple(
                item.owner_id for item in self.allocations + self.jobs + self.intents + self.events
            )
            owners += tuple(
                candidate.owner_id for plan in self.decisions for candidate in plan.candidates
            )
            if any(owner != self.viewer.owner_id for owner in owners):
                raise ValueError("owner-scoped snapshot contains another owner's data")
        if any(
            not event.version.is_same_epoch_at_or_before(self.version)
            or event.logical_time > self.now
            for event in self.events
        ):
            raise ValueError("read events must be from the snapshot epoch and causal prefix")
        if any(
            not plan.snapshot_version.is_same_epoch_at_or_before(self.version)
            or plan.decision_at > self.now
            for plan in self.decisions
        ):
            raise ValueError("read decisions must be from the snapshot epoch and causal prefix")
        if any(item.created_at > self.now for item in self.allocations + self.jobs):
            raise ValueError("read objects cannot precede their creation")
        for intent in self.intents:
            times = (intent.first_arrival_at,)
            times += tuple(
                value
                for attempt in intent.attempts
                for value in (attempt.created_at, attempt.started_at, attempt.finished_at)
                if value is not None
            )
            if intent.result is not None:
                times += (intent.result.recorded_at,)
            if any(value > self.now for value in times):
                raise ValueError("read intents must be from the snapshot causal prefix")
            if any(
                stage.receipt is not None
                and (
                    not stage.receipt.version.is_same_epoch_at_or_before(self.version)
                    or stage.receipt.logical_time > self.now
                )
                for stage in intent.stages
            ):
                raise ValueError(
                    "read stage receipts must be from the snapshot epoch and causal prefix"
                )
        if self.next_cursor is not None and self.next_cursor.epoch != self.version.epoch:
            raise ValueError("read event cursor must use the snapshot epoch")
        return self


class ExecutorCommand(ContractModel):
    dispatch_id: Identifier
    version: VersionStamp
    attempt: AttemptRef
    action: Literal["start", "checkpoint", "stop", "resume", "probe"]
    mode: ExecutionMode
    job: JobCreateParameters | None = None
    checkpoint: CheckpointFact | None = None

    @model_validator(mode="after")
    def execution_target(self) -> ExecutorCommand:
        if self.action in ("start", "resume") and self.job is None:
            raise ValueError("start and resume require the concrete job specification")
        if self.job is not None and self.job.allocation_id != self.attempt.allocation_id:
            raise ValueError("executor job allocation must match its attempt")
        if self.action == "resume" and (
            self.checkpoint is None or self.checkpoint.verification_status != "verified"
        ):
            raise ValueError("artifact resume requires a verified checkpoint")
        return self


class EvidenceWindow(ContractModel):
    start: LogicalTime
    end: LogicalTime

    @model_validator(mode="after")
    def ordered_window(self) -> EvidenceWindow:
        if self.end < self.start:
            raise ValueError("evidence window end precedes start")
        return self


class EvidenceEnvironment(ContractModel):
    platform: Text
    python_version: Identifier
    cpu_count: PositiveInt
    memory_mib: ResourceAmount
    execution_mode: ExecutionMode
    dependencies: tuple[VersionedArtifact, ...] = ()


class EvidenceCheck(ContractModel):
    target_id: Literal[
        "T-CONTRACT",
        "T-STATE",
        "T-REPLAY",
        "T-PRODUCT",
        "T-POLICY",
        "T-REAL",
        "T-SCALE",
        "T-LATENCY",
        "T-DELIVERY",
    ]
    status: Literal["MET", "NOT_MET", "NOT_RUN"]
    expected: Text
    actual: Text | None = None
    executor: Identifier | None = None
    command_or_action: Text | None = None
    evidence_uris: tuple[Text, ...] = ()
    unresolved: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def executed_evidence(self) -> EvidenceCheck:
        if self.status != "NOT_RUN" and (
            self.actual is None
            or self.executor is None
            or self.command_or_action is None
            or not self.evidence_uris
        ):
            raise ValueError(
                "executed checks require actual result, executor, action, and evidence"
            )
        if self.status == "NOT_RUN" and self.actual is not None:
            raise ValueError("unexecuted checks cannot report an actual result")
        return self


class EvidenceRun(ContractModel):
    run_id: Identifier
    run_kind: Literal["factual", "replay", "counterfactual"]
    parent_run_id: Identifier | None = None
    build: VersionedArtifact
    semantics: VersionedArtifact
    model: VersionedArtifact
    policy: VersionedArtifact
    scenario: VersionedArtifact
    calculator: VersionedArtifact
    seed: NonnegativeInt
    inputs: tuple[VersionedArtifact, ...]
    window: EvidenceWindow
    environment: EvidenceEnvironment
    status: Literal["not_run", "running", "completed", "failed"]
    outputs: tuple[VersionedArtifact, ...] = ()
    exceptions: tuple[ErrorDetail, ...] = ()
    checks: tuple[EvidenceCheck, ...] = ()

    @model_validator(mode="after")
    def run_provenance(self) -> EvidenceRun:
        if self.run_kind in ("replay", "counterfactual") and self.parent_run_id is None:
            raise ValueError("derived runs require their original run identity")
        if self.parent_run_id == self.run_id:
            raise ValueError("a run cannot derive from itself")
        if self.status == "failed" and not self.exceptions:
            raise ValueError("failed runs retain their exception evidence")
        if self.status == "not_run" and (
            self.outputs or self.exceptions or any(item.status != "NOT_RUN" for item in self.checks)
        ):
            raise ValueError("unexecuted runs cannot claim outputs or executed checks")
        if self.status == "completed" and not self.outputs:
            raise ValueError("completed runs require output provenance")
        if (
            any(item.target_id == "T-REAL" and item.status == "MET" for item in self.checks)
            and self.environment.execution_mode != "cpu_reference"
        ):
            raise ValueError("logical simulation cannot satisfy real execution evidence")
        return self


class ContractCatalog(ContractModel):
    """Schema export root; examples are illustrative contract data, not acceptance."""

    command_envelope: CommandEnvelope
    commit_receipt: CommitReceipt
    work_intent: WorkIntent
    decision_snapshot: DecisionSnapshot
    decision_plan: DecisionPlan
    committed_event: CommittedEvent
    read_snapshot: ReadSnapshot
    evidence_run: EvidenceRun
