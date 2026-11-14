# Internal contracts and implementation boundaries

`src/campus_fabric/contracts` contains Pydantic v2 data transfer objects and Python
`Protocol` boundaries. These are internal contracts, not additional public API
requirements. The copied `spec/official/api/openapi.yaml` remains authoritative and
unchanged. CP2 A now implements the persistent command kernel, workflow ports,
interruption lifecycle and CPU reference adapter; contract validation remains
separate from runtime and joint product acceptance.

The schema source is `ContractCatalog`; all eight top-level fields are required.
Supporting contracts are exported as JSON Schema `$defs` and TypeScript types.

| Contract | Purpose and main fields |
| --- | --- |
| `CommandEnvelope` | `request_id`, authenticated `actor`, `operation`, discriminated `parameters`, optional `idempotency_key`, optional `expected_version` |
| `CommitReceipt` | `request_id`, outcome, version, logical time, affected entities, committed event IDs, rejection, replay/original request identity |
| `WorkIntent` | Stable intent and owner, kernel-stamped first arrival, durable work specification, execution mode, settlement choice, allocation/job links, attempts, durable stage receipts and result |
| `DecisionSnapshot` | One version and logical time; appeared demands, inventory, occupancy, leases, issued notices, permitted history and metadata |
| `DecisionPlan` | Snapshot version, decision time, policy/model identity, concrete candidates, preconditions, violations, coverage, selection, rationale and tie-break rule |
| `CommittedEvent` | Event identity and sequence, commit version/time, owner/entities, actual request or earlier event cause, attempt, state/resource deltas and decision reference |
| `ReadSnapshot` | Authorized owner or administrator projection at one version; allocations, jobs, work, events, decisions, legal actions and epoch-scoped cursor |
| `EvidenceRun` | Build/semantics/model/policy/scenario/calculator identities, seed, immutable inputs, window, environment, execution status, outputs, exceptions and executed checks |

## Representation and identities

Models use `strict=True`, `frozen=True`, `extra="forbid"`, and validated defaults.
Nested DTOs are frozen. Collections use tuples and key/value entry DTOs instead of
mutable lists and dictionaries. Python callers supply tuples; JSON arrays are
accepted by `model_validate_json` and become tuples. Parse incoming JSON through
validation, rather than `model_construct` or `model_copy(update=...)`, which can
bypass validation. Returned JSON/TypeScript data is a serialization, not a live
reference to the authoritative domain objects.

Epochs, revisions, counts and resource quantities are strict integers. They are
nonnegative and bounded by JavaScript's largest safe integer, `2^53 - 1`; generation
and event sequence begin at 1. Logical time is finite, nonnegative seconds. Logical
time is separate from the optional measured `physical_elapsed_seconds` on real CPU
executor events. The upper integer bound is an internal serialization limit, not
an official capacity guarantee.

`VersionStamp(epoch, revision)` never compares a revision across epochs. A reset
creates a new epoch; old event causes, plans and read cursors cannot be relabeled as
current by a higher revision. The kernel must still compare the *current* version
inside the transaction. These DTOs cannot know what the current database contains.

`AuthenticatedActor(actor_id, role, owner_id)` is created by the authentication
adapter. User roles are `researcher`, `contributor`, and `administrator`;
`test_driver` and `system` are separate internal authorities. The DTO is not a
credential and cannot authenticate itself. Allocation creation, intent registration
and resource registration require their declared owner to match the actor owner.
Test operations require `test_driver`; decision and executor submissions require
`system`. Resource and object authorization is rechecked by the kernel.

`AttemptRef(intent_id, attempt_id, generation, allocation_id, job_id)` preserves the
stable work identity while distinguishing execution attempts. Attempt IDs differ
from intent IDs. A work's attempts have unique IDs and generations and must refer
to allocations and jobs linked to that work. These checks do not authorize two
concurrent jobs to share an allocation; that remains an explicit kernel semantic.

## Ports and the durable workflow

The protocols are synchronous service boundaries. Implementations may enqueue work
behind them, but a receipt must identify a completed durable commit or rejection.

| Protocol | Method |
| --- | --- |
| `CommandPort` | `commit(command) -> CommitReceipt` |
| `SnapshotPort` | `decision_snapshot() -> DecisionSnapshot` |
| `ReadPort` | `read_snapshot(query) -> ReadSnapshot` |
| `PolicyPort` | `plan(snapshot) -> DecisionPlan` |
| `ExecutorPort` | `dispatch(command) -> None`; `events(after=None) -> tuple[ExecutorEvent, ...]` |
| `WorkflowPort` | `register_intent(registration, expected_version, *, actor, request_id)` |
| `WorkflowPort` | `create_allocation_and_bind(intent_id, stage_id, expected_version, *, actor, request_id)` |
| `WorkflowPort` | `create_job_and_bind(intent_id, stage_id, expected_version, *, actor, request_id)` |

All three workflow methods return `CommitReceipt`. They adapt to the typed internal
commands `workflow.register_intent`, `workflow.create_allocation_and_bind`, and
`workflow.create_job_and_bind` through the same command kernel. B does not need a
second database or an untyped write hook.

`WorkIntentRegistration` contains the stable ID, owner, mode, settlement choice and
`WorkSpecification`. The specification stores allocation parameters and a
`JobSpecification` before any resource is obtained. The latter deliberately has no
allocation ID; binding supplies the actual committed allocation. The kernel stamps
first arrival when accepting registration and persists the entire specification.
The UI cannot backdate it. A paired experiment submits each external arrival at its
fixed logical instant. Existing HTTP-only clients need no intent registration.

Allocation creation, the intent link and the allocation-stage receipt are one
transaction. Job creation, the link and the job-stage receipt are another single
transaction. Both recheck authenticated ownership and expected version. The stage
receipt is durable before any success response. Lease success followed by job
failure remains a visible partial success, with the existing lease and rejected
job-stage receipt retained.

The kernel scopes workflow identities to epoch and authenticated owner. An identical
intent/stage retry returns the stored result; a changed operation or normalized
payload for the same identity returns a recognizable conflict. Look up an existing
stage receipt before treating its retry's now-stale version as a fresh command.
Deliberate new business attempts use a new stage identity; transport retries retain
the identity. Restart recovery must retain all of these records. The DTOs express
this contract; the kernel implements the transactions and their crash/replay
checks are recorded separately from schema tests.

The public `POST /v1/jobs` has no required idempotency key. Product workflow retries
must use the internal atomic stage operation, rather than assuming a public job POST
can be safely repeated. Public clients also need no new intent, stage, expected
version, role or attempt fields. Adapters translate frozen public fields, including
`owner`, environment objects and node-selector objects, to the internal DTO shape.
Omitted renew TTL and job timeout/retry values remain `None` for the centralized
semantics configuration to resolve; an adapter must not invent official defaults.

## Causality, explanation and evidence limits

`DecisionSnapshot` rejects demands that have not arrived, future observations,
unissued notices, and metadata whose `available_at` exceeds `now`. An issued notice
may contain a future deadline. `ObservedDatum` sources exclude hidden evaluator
inputs. These checks prevent explicit timing mistakes; they cannot detect a caller
that falsely labels a hidden value as an observed fact. Adapter isolation and
prefix-consistency experiments are still required.

Each selected candidate must be recorded and locally marked legal. An infeasible
candidate needs a constraint violation. Partial search must state why it stopped.
A whole-pool infeasibility assertion requires an explicitly fully examined finite
domain and exclusion evidence for every recorded candidate. The schema cannot prove
the declared domain covers all physical placements or that a claimed constraint is
correct. Full-domain enumeration/certificates and commit-time legality checks belong
to the implementation and independent verification.

Inventory checks validate nonnegative bounded quantities and `available <= capacity`;
placement checks reject duplicate device IDs. Whole GPU IDs and profile-instance
parent IDs remain distinct. These are structural checks, **not proof of physical
allocation legality, MIG parent/child exclusion, resource conservation, isolation,
authorization, atomicity or absence of overselling**.

`CheckpointFact` separates a received ready/failed report, verified artifact and
applied continuation. A ready report without an artifact URI is valid and remains
unverified. Verification needs URI, verifier and time; applied continuation needs a
verified artifact and resumed attempt. A simulated event cannot report measured
physical execution time. Executor dispatch is merely a request; resource release
after a real stop still requires the kernel's actual stop confirmation and stale
generation checks.

`EvidenceCheck` uses `MET / NOT_MET / NOT_RUN`. Executed checks require an actual
result, executor, command/action and evidence location. Unexecuted runs cannot claim
outputs or executed checks, and simulated evidence cannot mark `T-REAL` met. Strings
and hashes alone cannot establish that an experiment occurred; independent evidence
inspection remains necessary. Example artifacts deliberately use fixture labels,
placeholder hashes and `NOT_RUN` rather than claiming product acceptance.

## Reproduction and examples

```sh
uv run python scripts/export_contracts.py
uv run python scripts/export_contracts.py --check
uv run pytest tests/contracts -q
```

The exporter sorts JSON keys and emits a final newline, with no timestamps or
machine-dependent paths. It writes `contracts/generated/contracts.schema.json`.
The root is a `ContractCatalog`; a standalone DTO uses `#/$defs/<ContractName>` with
the exported definitions retained. The web build generates
`web/src/generated/contracts.ts` from that schema.

`contracts/examples/manifest.json` lists each fixture's path, contract, expected
validity and validation layer. `catalog.valid.json` exercises all eight contracts
and preserves an illustrative allocation-success/job-failure state. Separate valid
examples cover checkpoint reports without URIs, public-shaped job submission without
an idempotency key, and the three internal workflow commands. Negative fixtures
cover resources, owner identity, future information, partial-search overclaims,
epoch isolation, intent/attempt aliasing and unexecuted success claims.

JSON Schema describes field shapes and bounds; Pydantic additionally checks local
cross-field semantics. A semantic-negative fixture can therefore satisfy JSON
Schema and must still fail Pydantic validation. Tests deliberately verify that
distinction. Passing these tests is a CP0 contract result, not `T-CONTRACT`,
`T-STATE`, `T-POLICY`, or `T-REAL` runtime acceptance.

## CP2 additive read fields

`AllocationView` now exposes `preemption`, `checkpoint`, `restart_count` and
`pending_terminal`. `JobView` adds `execution_mode`, `retry_count`, `checkpoint`,
`result_uri`, `failure` and `finished_at`. Schema and TypeScript are regenerated
together. These fields describe server facts; they do not prove that B UI has
rendered or accepted them. `FaultParameters.target_operation` is text because
its value is an exact `METHOD /route-template` pair containing a space.

The concrete CPU adapter takes authoritative owner and logical-time arguments;
`CpuDriver` bridges its durable `poll`/`acknowledge` interface to the command kernel.
Do not assume the generic `ExecutorPort` sketch alone implements that wiring.
