"""Composition adapter: delegate to A without adding another operating ledger."""

from __future__ import annotations

import json
from typing import Any

from campus_fabric.contracts.models import AuthenticatedActor, NodeInventory, VersionStamp
from campus_fabric.projections.read_models import InventoryRead

from .service import (
    ArtifactReader,
    EvidenceReader,
    ExperimentRunner,
    PolicyValidationReader,
    QueryFailure,
    QueryServices,
)


def make_kernel_services(
    kernel: Any,
    identity: Any,
    *,
    clock_origin: str | None,
    evidence_reader: EvidenceReader | None = None,
    artifact_reader: ArtifactReader | None = None,
    experiment_runner: ExperimentRunner | None = None,
    policy_validation_reader: PolicyValidationReader | None = None,
) -> QueryServices:
    """Bind A's Kernel and HTTPIdentity to B's read adapters.

    The host supplies ``bootstrap_epoch.value.clock_origin`` from the shared
    semantics configuration. Evidence/artifacts stay unavailable unless the host
    supplies actual persisted readers. The signed CPU artifact URI route remains
    A-owned. Public inventory is the shared discovery surface; private ownership is redacted by
    the public capacity route.
    """

    def inventory_reader(actor: AuthenticatedActor) -> InventoryRead:
        # export_state is an A-owned, actor-scoped read of one SQLite snapshot.
        # Only inventory/version/time cross this adapter; no tenant work is copied.
        state = kernel.export_state(actor)
        try:
            return InventoryRead(
                viewer=actor,
                version=VersionStamp(epoch=state["epoch"], revision=state["revision"]),
                now=state["now"],
                nodes=tuple(NodeInventory.model_validate_json(json.dumps(value))
                            for value in state["nodes"].values()),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise QueryFailure(502, "provider_contract_error", "Invalid CP1 inventory snapshot.") from error

    return QueryServices(
        read_port=kernel, resolve_actor=identity.actor, inventory_reader=inventory_reader,
        evidence_reader=evidence_reader, artifact_reader=artifact_reader,
        experiment_runner=experiment_runner,
        policy_validation_reader=policy_validation_reader,
        clock_origin=clock_origin, cp1_advisory_actions=True,
    )
