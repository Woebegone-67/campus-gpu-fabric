"""Injection boundary between B's HTTP projections and A's authoritative ports."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import Request
from pydantic import ValidationError

from campus_fabric.contracts.models import (
    AuthenticatedActor,
    EntityRef,
    EventCursor,
    EvidenceCheck,
    EvidenceRun,
    NodeInventory,
    ReadQuery,
    ReadSnapshot,
    VersionStamp,
)
from campus_fabric.contracts.ports import ReadPort
from campus_fabric.projections.read_models import (
    ArtifactRead,
    EvidenceRead,
    InventoryRead,
    OverviewRead,
    OwnedEvidence,
)

ActorResolver = Callable[[Request], AuthenticatedActor]
InventoryReader = Callable[[AuthenticatedActor], InventoryRead]
OverviewReader = Callable[[AuthenticatedActor], OverviewRead]
EvidenceReader = Callable[[AuthenticatedActor], EvidenceRead]
ArtifactReader = Callable[[AuthenticatedActor, str, str], ArtifactRead | None]


class PolicyValidationReader(Protocol):
    """Read sealed CP3 outputs; a request cannot run code or choose a local path."""

    def snapshot(self) -> dict: ...

    def artifact(self, artifact_id: str) -> bytes: ...


class ExperimentRunner(Protocol):
    """Registered, isolated experiments; never a caller-supplied scenario or URL."""

    def catalog(self) -> list[dict]: ...

    def submit(self, *, actor: AuthenticatedActor, scenario_id: str,
               policy: str, seed: int = 0) -> EvidenceRun: ...

    def list_runs(self, actor: AuthenticatedActor) -> tuple[OwnedEvidence, ...]: ...

    def artifact(self, actor: AuthenticatedActor, run_id: str,
                 artifact_id: str) -> tuple[bytes, str]: ...


class QueryFailure(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def unavailable(name: str) -> QueryFailure:
    return QueryFailure(
        503, "dependency_unavailable", f"{name} is not connected to the authoritative service."
    )


def invoke(function: Callable[..., Any], *args: Any) -> Any:
    try:
        return function(*args)
    except QueryFailure:
        raise
    except PermissionError:
        raise QueryFailure(403, "forbidden", "The authenticated actor cannot read this scope.")
    except (NotImplementedError, ConnectionError, TimeoutError):
        raise unavailable("Read provider")


def validated_snapshot(value: Any, actor: AuthenticatedActor, global_scope: bool) -> ReadSnapshot:
    if not isinstance(value, ReadSnapshot):
        raise QueryFailure(502, "provider_contract_error", "Invalid authoritative snapshot.")
    try:
        snapshot = ReadSnapshot.model_validate_json(value.model_dump_json())
        for items, key in ((snapshot.allocations, "allocation_id"),
                           (snapshot.jobs, "job_id"), (snapshot.intents, "intent_id")):
            if len({getattr(item, key) for item in items}) != len(items):
                raise ValueError("duplicate record")
    except (ValueError, TypeError, AttributeError):
        raise QueryFailure(502, "provider_contract_error", "Invalid authoritative snapshot.")
    if snapshot.viewer != actor or snapshot.global_scope != global_scope:
        raise QueryFailure(502, "provider_contract_error", "Snapshot authorization mismatch.")
    return snapshot


def validated_inventory(value: Any, actor: AuthenticatedActor) -> InventoryRead:
    if not isinstance(value, InventoryRead) or value.viewer != actor:
        raise QueryFailure(502, "provider_contract_error", "Invalid inventory authorization.")
    try:
        version = VersionStamp.model_validate_json(value.version.model_dump_json())
        nodes = tuple(NodeInventory.model_validate_json(x.model_dump_json()) for x in value.nodes)
        if isinstance(value.now, bool) or not math.isfinite(value.now) or value.now < 0:
            raise ValueError("invalid time")
        if any(x.observed_at > value.now for x in nodes):
            raise ValueError("future inventory")
        if len({x.node_id for x in nodes}) != len(nodes):
            raise ValueError("duplicate inventory")
    except (ValueError, TypeError, AttributeError):
        raise QueryFailure(502, "provider_contract_error", "Invalid inventory snapshot.")
    return InventoryRead(viewer=actor, version=version, now=value.now, nodes=nodes)


@dataclass(frozen=True)
class QueryServices:
    """Register A's trusted providers explicitly; no fallback business fixture.

    ``clock_origin`` must come from the team's centralized semantics configuration.
    ``resolve_actor`` is an authentication adapter, not a caller-supplied role.
    If omitted, a previously authenticated ``request.state.actor`` is required.
    """

    read_port: ReadPort | None = None
    resolve_actor: ActorResolver | None = None
    inventory_reader: InventoryReader | None = None
    evidence_reader: EvidenceReader | None = None
    artifact_reader: ArtifactReader | None = None
    experiment_runner: ExperimentRunner | None = None
    policy_validation_reader: PolicyValidationReader | None = None
    clock_origin: str | None = None
    cp1_advisory_actions: bool = False
    overview_reader: OverviewReader | None = None

    def experiments(self, actor: AuthenticatedActor) -> ExperimentRunner:
        if actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        if self.experiment_runner is None:
            raise unavailable("Isolated experiment runner")
        return self.experiment_runner

    def policy_validation(self, actor: AuthenticatedActor) -> PolicyValidationReader:
        if actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        if self.policy_validation_reader is None:
            raise unavailable("Sealed policy validation reader")
        return self.policy_validation_reader

    def actor(self, request: Request) -> AuthenticatedActor:
        if self.resolve_actor is None:
            value = getattr(request.state, "actor", None)
            if value is None:
                raise unavailable("Authentication adapter")
        else:
            value = invoke(self.resolve_actor, request)
            if value is None:
                raise QueryFailure(401, "unauthorized", "An authenticated identity is required.")
        if not isinstance(value, AuthenticatedActor):
            raise QueryFailure(502, "provider_contract_error", "Invalid authenticated actor.")
        if value.role not in ("researcher", "contributor", "administrator"):
            raise QueryFailure(403, "forbidden", "Internal authorities cannot use user queries.")
        return value

    def origin(self) -> datetime:
        if self.clock_origin is None:
            raise unavailable("Logical clock origin")
        try:
            origin = datetime.fromisoformat(self.clock_origin.replace("Z", "+00:00"))
            if origin.tzinfo is None:
                raise ValueError("timezone required")
            return origin.astimezone(timezone.utc)
        except ValueError:
            raise QueryFailure(502, "provider_contract_error", "Invalid logical clock origin.")

    def read(
        self,
        actor: AuthenticatedActor,
        *,
        global_scope: bool | None = None,
        entities: tuple[EntityRef, ...] = (),
        after: EventCursor | None = None,
    ) -> ReadSnapshot:
        # A global administrator has no tenant to impersonate. This default is
        # derived only from the trusted auth adapter, never headers/UI profiles.
        # Owner-bearing identities retain their caller scope unless a dedicated
        # administrator endpoint explicitly requests the global projection.
        if global_scope is None:
            global_scope = actor.role == "administrator" and actor.owner_id is None
        if global_scope and actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        if not global_scope and actor.owner_id is None:
            raise QueryFailure(403, "forbidden", "An owner identity is required for caller scope.")
        if self.read_port is None:
            raise unavailable("ReadPort")
        try:
            value = invoke(
                self.read_port.read_snapshot,
                ReadQuery(actor=actor, global_scope=global_scope, entities=entities, after=after),
            )
        except ValueError as error:
            # CP1 rejects stale event epochs before producing a ReadSnapshot.
            if after is not None and str(error) == (
                "Event cursor belongs to an old epoch; refresh without the cursor"
            ):
                raise QueryFailure(409, "stale_epoch", "The resource epoch changed; restart event reading.") from error
            raise QueryFailure(502, "provider_contract_error", "Invalid authoritative snapshot.") from error
        snapshot = validated_snapshot(value, actor, global_scope)
        if after is not None and after.epoch != snapshot.version.epoch:
            raise QueryFailure(409, "stale_epoch", "The resource epoch changed; restart event reading.")
        return snapshot

    def inventory(self, actor: AuthenticatedActor) -> InventoryRead:
        if self.inventory_reader is None:
            raise unavailable("Authorized inventory provider")
        return validated_inventory(invoke(self.inventory_reader, actor), actor)

    def overview(self, actor: AuthenticatedActor) -> OverviewRead:
        if actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        if self.overview_reader is None:
            # Legacy injected providers remain supported only when their stamps
            # agree. The application binds a single-read provider below this seam.
            snapshot = self.read(actor, global_scope=True)
            inventory = self.inventory(actor)
        else:
            value = invoke(self.overview_reader, actor)
            if not isinstance(value, OverviewRead):
                raise QueryFailure(502, "provider_contract_error", "Invalid overview snapshot.")
            snapshot = validated_snapshot(value.snapshot, actor, True)
            inventory = validated_inventory(value.inventory, actor)
        if snapshot.version != inventory.version or snapshot.now != inventory.now:
            raise QueryFailure(409, "stale_version", "Overview providers returned different snapshots.")
        return OverviewRead(snapshot=snapshot, inventory=inventory)

    def evidence(self, actor: AuthenticatedActor) -> EvidenceRead:
        if self.evidence_reader is None:
            raise unavailable("Evidence provider")
        value = invoke(self.evidence_reader, actor)
        if not isinstance(value, EvidenceRead) or value.viewer != actor:
            raise QueryFailure(502, "provider_contract_error", "Invalid evidence authorization.")
        try:
            version = VersionStamp.model_validate_json(value.version.model_dump_json())
            if not math.isfinite(value.now) or value.now < 0:
                raise ValueError("invalid time")
            runs = tuple(
                OwnedEvidence(x.owner_id, EvidenceRun.model_validate_json(x.run.model_dump_json()))
                for x in value.runs
            )
            checks = tuple(
                EvidenceCheck.model_validate_json(x.model_dump_json()) for x in value.checks
            )
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise QueryFailure(502, "provider_contract_error", "Invalid evidence snapshot.")
        return EvidenceRead(
            viewer=actor, version=version, now=value.now, runs=runs, checks=checks,
            build_matches=value.build_matches,
        )
