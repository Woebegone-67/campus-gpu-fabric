"""Composition adapter: delegate to A without adding another operating ledger."""

from __future__ import annotations

import json
from typing import Any

from campus_fabric.contracts.models import (
    AllocationView,
    AuthenticatedActor,
    JobView,
    NodeInventory,
    ReadSnapshot,
    VersionStamp,
    WorkIntent,
)
from campus_fabric.projections.read_models import InventoryRead, OverviewRead

from .service import ArtifactReader, EvidenceReader, ExperimentRunner, QueryFailure, QueryServices


def make_kernel_services(
    kernel: Any,
    identity: Any,
    *,
    clock_origin: str | None,
    evidence_reader: EvidenceReader | None = None,
    artifact_reader: ArtifactReader | None = None,
    experiment_runner: ExperimentRunner | None = None,
) -> QueryServices:
    """Bind A's Kernel and HTTPIdentity to B's read adapters.

    The host supplies ``bootstrap_epoch.value.clock_origin`` from the shared
    semantics configuration. Evidence/artifacts stay unavailable unless the host
    supplies actual persisted readers. The signed CPU artifact URI route remains
    A-owned. Public inventory is the shared discovery surface; private ownership is redacted by
    the public capacity route.
    """

    def records(state: dict, collection: str, model: Any, id_field: str) -> tuple:
        values = state[collection]
        if not isinstance(values, dict):
            raise ValueError("invalid collection")
        items = []
        for key, value in values.items():
            if not isinstance(value, dict) or value.get(id_field) != key:
                raise ValueError("record key does not match its identity")
            # A's exported jobs/leases can contain extra authoritative fields.
            # Only the public read model crosses the projection boundary.
            fields = ({k: v for k, v in value.items() if k in model.model_fields}
                      if model in (AllocationView, JobView) else value)
            items.append(model.model_validate_json(json.dumps(fields)))
        return tuple(items)

    def inventory_from_state(state: dict, actor: AuthenticatedActor) -> InventoryRead:
        return InventoryRead(
            viewer=actor,
            version=VersionStamp(epoch=state["epoch"], revision=state["revision"]),
            now=state["now"],
            nodes=records(state, "nodes", NodeInventory, "node_id"),
        )

    def inventory_reader(actor: AuthenticatedActor) -> InventoryRead:
        # export_state is an A-owned, actor-scoped read of one SQLite snapshot.
        try:
            return inventory_from_state(kernel.export_state(actor), actor)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise QueryFailure(502, "provider_contract_error", "Invalid inventory snapshot.") from error

    def overview_reader(actor: AuthenticatedActor) -> OverviewRead:
        if actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        try:
            # Exactly one authorized read: a concurrent clock/worker commit can
            # advance the database, but cannot split counts from capacity here.
            state = kernel.export_state(actor)
            inventory = inventory_from_state(state, actor)
            snapshot = ReadSnapshot(
                viewer=actor, global_scope=True, version=inventory.version, now=inventory.now,
                allocations=records(state, "allocations", AllocationView, "allocation_id"),
                jobs=records(state, "jobs", JobView, "job_id"),
                intents=records(state, "intents", WorkIntent, "intent_id"),
            )
            return OverviewRead(snapshot=snapshot, inventory=inventory)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise QueryFailure(502, "provider_contract_error", "Invalid overview snapshot.") from error

    return QueryServices(
        read_port=kernel, resolve_actor=identity.actor, inventory_reader=inventory_reader,
        evidence_reader=evidence_reader, artifact_reader=artifact_reader,
        experiment_runner=experiment_runner,
        clock_origin=clock_origin, cp1_advisory_actions=True, overview_reader=overview_reader,
    )
