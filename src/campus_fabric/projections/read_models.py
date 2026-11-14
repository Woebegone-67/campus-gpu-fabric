"""Pure B-side read projections; these objects never own authoritative state."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from campus_fabric.contracts.models import (
    AllocationView,
    AuthenticatedActor,
    EvidenceCheck,
    EvidenceRun,
    JobView,
    NodeInventory,
    ReadSnapshot,
    ResourceAmounts,
    VersionStamp,
)


@dataclass(frozen=True)
class InventoryRead:
    """An A-authorized inventory slice, never a raw global DecisionSnapshot.

    The provider decides which shared resources the caller may discover. The
    contributions projection further restricts this slice to the contributor.
    """

    viewer: AuthenticatedActor
    version: VersionStamp
    now: float
    nodes: tuple[NodeInventory, ...]


@dataclass(frozen=True)
class OwnedEvidence:
    owner_id: str
    run: EvidenceRun


@dataclass(frozen=True)
class EvidenceRead:
    viewer: AuthenticatedActor
    version: VersionStamp
    now: float
    runs: tuple[OwnedEvidence, ...] = ()
    checks: tuple[EvidenceCheck, ...] = ()
    build_matches: bool | None = None


@dataclass(frozen=True)
class ArtifactRead:
    """A resolved, registered artifact returned by A, not a URL/path proxy."""

    owner_id: str
    intent_id: str
    artifact_id: str
    content: bytes
    media_type: str = "application/octet-stream"


class ProjectionError(ValueError):
    """The authoritative values cannot be represented by the public read shape."""


def timestamp(origin: datetime, logical_time: float) -> str:
    try:
        return (origin + timedelta(seconds=logical_time)).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        raise ProjectionError("Logical time cannot be represented as an RFC3339 date.")


def allocation_json(item: AllocationView, origin: datetime) -> dict[str, Any]:
    """Preserve frozen public field names and expose logical times separately."""
    result = item.model_dump(mode="json")
    result.update(
        id=item.allocation_id,
        owner=item.owner_id,
        created_at=timestamp(origin, item.created_at),
        expires_at=None if item.expires_at is None else timestamp(origin, item.expires_at),
        created_at_logical=item.created_at,
        expires_at_logical=item.expires_at,
    )
    return result


def job_json(item: JobView, origin: datetime) -> dict[str, Any]:
    return {
        **item.model_dump(mode="json"), "id": item.job_id, "owner": item.owner_id,
        "created_at": timestamp(origin, item.created_at), "created_at_logical": item.created_at,
    }


def metrics_json(snapshot: ReadSnapshot) -> dict[str, Any]:
    """Counts over actual authorized records, not invented utilization or speed."""
    return {
        "scope": "global" if snapshot.global_scope else "owner",
        "owner_id": None if snapshot.global_scope else snapshot.viewer.owner_id,
        "allocations_by_state": dict(sorted(Counter(x.state for x in snapshot.allocations).items())),
        "jobs_by_state": dict(sorted(Counter(x.state for x in snapshot.jobs).items())),
        "work_by_state": dict(sorted(Counter(x.state for x in snapshot.intents).items())),
        "counts": {
            "allocations": len(snapshot.allocations),
            "jobs": len(snapshot.jobs),
            "work_intents": len(snapshot.intents),
        },
        "measurement_kind": "authorized_record_counts",
        "performance_measurements": None,
        "allocations": len(snapshot.allocations),
        "jobs": len(snapshot.jobs),
        "running_jobs": sum(x.state == "Running" for x in snapshot.jobs),
        "succeeded_jobs": sum(x.state == "Succeeded" for x in snapshot.jobs),
        "failed_jobs": sum(x.state == "Failed" for x in snapshot.jobs),
    }


def resource_totals(nodes: tuple[NodeInventory, ...]) -> dict[str, dict[str, int]]:
    return {
        kind: {
            field: sum(getattr(getattr(node, kind), field) for node in nodes)
            for field in ResourceAmounts.model_fields
        }
        for kind in ("capacity", "available")
    }


def visible_entity_keys(snapshot: ReadSnapshot) -> set[tuple[str, str]]:
    keys = {("allocation", x.allocation_id) for x in snapshot.allocations}
    keys.update(("job", x.job_id) for x in snapshot.jobs)
    keys.update(("intent", x.intent_id) for x in snapshot.intents)
    keys.update(("decision", x.decision_id) for x in snapshot.decisions)
    for intent in snapshot.intents:
        keys.update(("attempt", x.ref.attempt_id) for x in intent.attempts)
        keys.update(
            ("checkpoint", x.checkpoint.checkpoint_id)
            for x in intent.attempts
            if x.checkpoint is not None
        )
    return keys


def legal_actions_json(
    snapshot: ReadSnapshot, related: set[tuple[str, str]] | None = None,
    *, include_cp1: bool = False,
) -> list[dict[str, Any]]:
    visible = visible_entity_keys(snapshot)
    if related is not None:
        visible &= related
    actions = [
        action.model_dump(mode="json")
        for action in snapshot.legal_actions
        if (action.entity.kind, action.entity.entity_id) in visible
    ]
    if not include_cp1:
        return actions
    if snapshot.viewer.role not in ("researcher", "administrator"):
        return []
    # CP1 hints are derived only from the authorized causal snapshot. They are
    # advisory: a concurrent commit can invalidate them before A checks a write.
    found = {(x["operation"], x["entity"]["kind"], x["entity"]["entity_id"]) for x in actions}

    def add(operation: str, kind: str, key: str, explanation: str) -> None:
        if (kind, key) in visible and (operation, kind, key) not in found:
            actions.append({"operation": operation, "entity": {"kind": kind, "entity_id": key},
                            "explanation": explanation, "advisory": True})
            found.add((operation, kind, key))

    for allocation in snapshot.allocations:
        if (allocation.state in {"Allocated", "Running"} and allocation.expires_at is not None
                and allocation.expires_at > snapshot.now):
            add("allocation.renew", "allocation", allocation.allocation_id, "Renew this live lease")
    for job in snapshot.jobs:
        if job.state in {"Pending", "Running"}:
            add("job.cancel", "job", job.job_id, "Cancel the current job")
    allocations = {x.allocation_id: x for x in snapshot.allocations}
    for intent in snapshot.intents:
        if intent.state in {"completed", "failed", "cancelled"}:
            continue
        if not intent.allocation_ids:
            add("workflow.create_allocation_and_bind", "intent", intent.intent_id,
                "Create and bind the allocation stage")
        elif not intent.job_ids:
            allocation = allocations.get(intent.allocation_ids[0])
            if (allocation is not None and allocation.state == "Allocated"
                    and allocation.expires_at is not None and allocation.expires_at > snapshot.now):
                add("workflow.create_job_and_bind", "intent", intent.intent_id,
                    "Create and bind the job stage")
    return actions
