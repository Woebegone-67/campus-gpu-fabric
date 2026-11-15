"""Deterministic, bounded placement baselines over an immutable causal snapshot.

This module proposes at most one placement. It never commits, reserves, reads a
scenario, or predicts a duration. The kernel must recheck all constraints inside
its transaction. Search is explicitly single-node and never proves pool-wide
infeasibility. B1/B2 use current occupied dominant share, not a historical fair
service guarantee. Recovery eligibility is kernel-owned: a resume validated by A re-enters Pending
without placement and is treated as a normal placement demand. Currently held
leases are never allocated a second time.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Literal

from campus_fabric.contracts.models import (
    MAX_SAFE_INTEGER,
    CandidateAction,
    ConstraintViolation,
    DecisionPlan,
    DecisionSnapshot,
    DemandSnapshot,
    EntityRef,
    GPUDevice,
    NodeInventory,
    Placement,
    PlacementSlice,
    Precondition,
    ResourceAmounts,
    ScoreComponent,
    SearchCoverage,
    VersionedArtifact,
)

PolicyName = Literal["B0", "B1", "B2", "P"]
_RESOURCE_FIELDS = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_hash() -> str:
    # Provenance belongs to the imported implementation, not a later disk edit.
    return _LOADED_SOURCE_SHA256


def _matches(device: GPUDevice, demand: DemandSnapshot) -> bool:
    gpu = demand.request.gpu
    return (
        device.profile == gpu.profile
        and (gpu.model == "any" or device.model == gpu.model)
        and device.memory_mib >= gpu.memory_mib
    )


def _owner_shares(snapshot: DecisionSnapshot) -> dict[str, float]:
    # Physical VRAM, rather than a count of whole/slice representations, keeps
    # MIG slicing from inflating the owner's occupied share denominator.
    fields = (*_RESOURCE_FIELDS, "gpu_memory_mib")
    capacity = {f: sum(getattr(n.capacity, f) for n in snapshot.inventory) for f in fields}
    usage: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(fields, 0))
    for hold in snapshot.occupancy:
        for piece in hold.placement.slices:
            for field in fields:
                usage[hold.owner_id][field] += getattr(piece.resources, field)
    return {
        owner: max(
            (values[f] / capacity[f] if capacity[f] else float(values[f] > 0))
            for f in fields
        )
        for owner, values in usage.items()
    }


def _shape(demand: DemandSnapshot) -> str:
    return _digest(
        {
            "request": demand.request.model_dump(mode="json"),
            "selector": sorted((x.key, x.value) for x in demand.node_selector),
        }
    )


def _label_match(node: NodeInventory, demand: DemandSnapshot) -> bool:
    labels = {item.key: item.value for item in node.labels}
    return all(labels.get(item.key) == item.value for item in demand.node_selector)


def _scarcity_cost(
    snapshot: DecisionSnapshot,
    demand: DemandSnapshot,
    node: NodeInventory,
    selected: tuple[GPUDevice, ...],
) -> float:
    """Known compatibility pressure; no binary current-feasibility assumption.

    Deduplicate equivalent requests within each owner, then give each other owner
    total weight one. This limits inflation from simply cloning identical queued
    requests. It is a static compatibility proxy, not completion-time prediction.
    """
    groups: dict[str, dict[str, DemandSnapshot]] = defaultdict(dict)
    for other in snapshot.demands:
        if other.demand_id != demand.demand_id and other.request.gpu.count:
            groups[other.owner_id][_shape(other)] = other
    cost = 0.0
    for shapes in groups.values():
        for other in shapes.values():
            matching = [
                (pool_node, device)
                for pool_node in snapshot.inventory
                if _label_match(pool_node, other)
                for device in pool_node.devices
                if _matches(device, other)
            ]
            if not matching or not _label_match(node, other):
                continue
            consumed = sum(_matches(device, other) for device in selected)
            cost += consumed / len(matching) / len(shapes)
    return cost


def _candidate(
    snapshot: DecisionSnapshot,
    demand: DemandSnapshot,
    node: NodeInventory,
    devices: tuple[GPUDevice, ...],
    shares: dict[str, float],
    policy: PolicyName,
) -> CandidateAction:
    candidate_id = (
        f"candidate-{_digest([demand.demand_id, node.node_id, [d.device_id for d in devices]])[:24]}"
    )
    node_ref = EntityRef(kind="node", entity_id=node.node_id)
    violations: list[ConstraintViolation] = []

    def fail(kind: str, detail: str, entity: EntityRef = node_ref) -> None:
        violations.append(ConstraintViolation(constraint=kind, detail=detail, entity=entity))

    if node.state != "Ready":
        fail("node_state", f"Node is {node.state}, not Ready.")
    if not _label_match(node, demand):
        fail("labels", "Node labels do not satisfy the request selector.")
    for field, name in zip(_RESOURCE_FIELDS, ("cpu", "memory", "storage"), strict=True):
        if getattr(node.available, field) < getattr(demand.request, field):
            fail(name, f"Available {field} is below the requested amount.")
    if len(devices) != demand.request.gpu.count:
        fail("gpu_count", "Node has fewer device records than the requested GPU count.")
    if demand.request.gpu.profile is None and node.available.gpu_count < demand.request.gpu.count:
        fail("gpu_count", "Node's available GPU count is below the request.")
    if sum(d.memory_mib for d in devices) > node.available.gpu_memory_mib:
        fail("gpu_memory", "Selected device capacity exceeds available GPU memory accounting.")

    occupied_ids = {
        device_id
        for hold in snapshot.occupancy
        for piece in hold.placement.slices
        for device_id in piece.device_ids
    }
    for device in node.devices:
        if device.state in ("occupied", "quarantined"):
            occupied_ids.add(device.device_id)
    for device in devices:
        ref = EntityRef(kind="gpu", entity_id=device.device_id)
        if device.state != "available" or device.device_id in occupied_ids:
            fail("quarantine" if device.state == "quarantined" else "gpu_count",
                 f"Device {device.device_id} is not free for a new placement.", ref)
        if device.profile != demand.request.gpu.profile:
            fail("profile", "GPU profile does not exactly match the requested shape.", ref)
        if demand.request.gpu.model not in ("any", device.model):
            fail("gpu_model", "GPU model does not match the request.", ref)
        if device.memory_mib < demand.request.gpu.memory_mib:
            fail("gpu_memory", "Per-device memory is below the requested minimum.", ref)
        for sibling in node.devices:
            if sibling.physical_gpu_id != device.physical_gpu_id:
                continue
            if sibling.device_id != device.device_id and (
                sibling in devices or sibling.device_id in occupied_ids
            ) and (sibling.profile is None or device.profile is None):
                fail("profile", "Whole GPU and one of its profile slices cannot coexist.", ref)
                break
        for notice in snapshot.notices:
            if notice.issued_at <= snapshot.now < notice.unavailable_until and (
                device.device_id in notice.device_ids
                or device.physical_gpu_id in notice.device_ids
                or any(
                    sibling.device_id in notice.device_ids
                    and sibling.physical_gpu_id == device.physical_gpu_id
                    for sibling in node.devices
                )
            ):
                fail("lease", f"Contributor notice {notice.notice_id} blocks new placement.", ref)
                break

    # A declared whole-device record provides an additional parent capacity bound.
    for parent_id in {d.physical_gpu_id for d in devices if d.profile is not None}:
        parent = next((d for d in node.devices if d.device_id == parent_id), None)
        if parent is not None:
            slices = {
                d.device_id: d for d in node.devices
                if d.physical_gpu_id == parent_id and d.profile is not None
                and (d in devices or d.device_id in occupied_ids)
            }
            if sum(d.memory_mib for d in slices.values()) > parent.memory_mib:
                fail("profile", f"Selected and held slices exceed parent {parent_id} memory.")

    device_memory = sum(d.memory_mib for d in devices)
    if device_memory > MAX_SAFE_INTEGER:
        # Every individual DTO can be valid while the proposed sum cannot fit
        # its aggregate contract. Such a candidate also exceeds the node's
        # bounded available memory. Explain it without constructing an invalid
        # Placement or silently clamping a resource amount.
        return CandidateAction(
            candidate_id=candidate_id, action="allocate", owner_id=demand.owner_id,
            demand_id=demand.demand_id, intent_id=demand.intent_id,
            allocation_id=demand.allocation_id, feasibility="infeasible",
            violations=tuple(violations),
        )
    placement = Placement(slices=(PlacementSlice(
        node_id=node.node_id,
        resources=ResourceAmounts(
            cpu_millicores=demand.request.cpu_millicores,
            memory_mib=demand.request.memory_mib,
            ephemeral_storage_mib=demand.request.ephemeral_storage_mib,
            gpu_count=len(devices),
            gpu_memory_mib=device_memory,
        ),
        device_ids=tuple(d.device_id for d in devices),
    ),))
    wasted_gpu_memory = sum(d.memory_mib - demand.request.gpu.memory_mib for d in devices)
    residual_gpu_count = max(0, node.available.gpu_count - demand.request.gpu.count)
    residual_cpu = max(0, node.available.cpu_millicores - demand.request.cpu_millicores)
    residual_memory = max(0, node.available.memory_mib - demand.request.memory_mib)
    scores = [
        ScoreComponent(name="owner_current_share", value=shares.get(demand.owner_id, 0.0),
                       explanation="Current occupied dominant share; lower sorts first in B1/B2."),
        ScoreComponent(name="remaining_gpu_count", value=float(residual_gpu_count),
                       explanation="Lower residual device count sorts first in best-fit."),
        ScoreComponent(name="excess_gpu_memory_mib", value=float(max(0, wasted_gpu_memory)),
                       explanation="Per-device memory above requested minimum; lower sorts first."),
        ScoreComponent(name="remaining_cpu_millicores", value=float(residual_cpu),
                       explanation="CPU remainder after placement; stable best-fit tie component."),
        ScoreComponent(name="remaining_memory_mib", value=float(residual_memory),
                       explanation="Memory remainder after placement; stable best-fit tie component."),
    ]
    if policy == "B2":
        scores.append(ScoreComponent(
            name="compatibility_pressure", value=(
                _scarcity_cost(snapshot, demand, node, devices) if not violations else 0.0
            ),
            explanation="Known queued owners' compatible device opportunities consumed; lower first.",
        ))
    return CandidateAction(
        candidate_id=candidate_id,
        action="allocate", owner_id=demand.owner_id, demand_id=demand.demand_id,
        intent_id=demand.intent_id, allocation_id=demand.allocation_id,
        placement=placement, feasibility="infeasible" if violations else "legal",
        violations=tuple(violations),
        preconditions=(
            (
                Precondition(kind="owner", entity=EntityRef(
                    kind="allocation", entity_id=demand.allocation_id),
                    expected_value=demand.owner_id),
                Precondition(kind="allocation_state", entity=EntityRef(
                    kind="allocation", entity_id=demand.allocation_id), expected_value="Pending"),
            ) if demand.allocation_id is not None else ()
        ),
        scores=tuple(scores),
    )


class BaselinePolicy:
    """Bounded one-action baseline implementing PolicyPort.

    B0 scans oldest known demands first and selects the first feasible placement.
    B1/B2 scan by current owner share, descending request priority, arrival and ID.
    Best-fit uses residual GPU count, excess VRAM, residual CPU, then RAM. B2 adds
    known compatibility pressure before those keys. This is a heuristic, not DRF
    or a starvation-free guarantee. Search truncation is a visible decision fact.
    """

    def __init__(self, name: PolicyName = "B0", *, max_candidates: int = 512):
        if name not in ("B0", "B1", "B2"):
            raise ValueError("Unknown baseline policy")
        if not 1 <= max_candidates <= 100_000:
            raise ValueError("max_candidates must be between 1 and 100000")
        self.name = name
        self.max_candidates = max_candidates

    def plan(self, snapshot: DecisionSnapshot) -> DecisionPlan:
        shares = _owner_shares(snapshot)
        demands = sorted(snapshot.demands, key=lambda d: (
            (d.arrived_at, d.demand_id) if self.name == "B0" else
            (shares.get(d.owner_id, 0.0), -d.priority, d.arrived_at, d.demand_id)
        ))
        candidates: list[CandidateAction] = []
        truncated = False
        lease_ids = {lease.allocation_id for lease in snapshot.leases}
        for demand in demands:
            if len(candidates) >= self.max_candidates:
                truncated = True
                break
            if demand.allocation_id in lease_ids:
                candidates.append(CandidateAction(
                    candidate_id=f"recovery-{_digest(demand.demand_id)[:24]}", action="resume",
                    owner_id=demand.owner_id, demand_id=demand.demand_id,
                    intent_id=demand.intent_id, allocation_id=demand.allocation_id,
                    feasibility="unexamined",
                ))
                continue
            for node in sorted(snapshot.inventory, key=lambda n: n.node_id):
                devices = tuple(sorted(node.devices, key=lambda d: d.device_id))
                choices = itertools.combinations(devices, demand.request.gpu.count)
                if len(devices) < demand.request.gpu.count:
                    choices = iter(((),))
                for choice in choices:
                    if len(candidates) >= self.max_candidates:
                        truncated = True
                        break
                    candidates.append(_candidate(snapshot, demand, node, choice, shares, self.name))
                if truncated:
                    break
            if truncated:
                break

        selected: CandidateAction | None = None
        for demand in demands:
            feasible = [c for c in candidates
                        if c.demand_id == demand.demand_id and c.feasibility == "legal"]
            if not feasible:
                continue
            if self.name == "B0":
                selected = feasible[0]
            else:
                def key(candidate: CandidateAction) -> tuple:
                    scores = {s.name: s.value for s in candidate.scores}
                    return (
                        *((scores["compatibility_pressure"],) if self.name == "B2" else ()),
                        scores["remaining_gpu_count"], scores["excess_gpu_memory_mib"],
                        scores["remaining_cpu_millicores"], scores["remaining_memory_mib"],
                        candidate.placement.slices[0].node_id,
                        candidate.placement.slices[0].device_ids,
                    )
                selected = min(feasible, key=key)
            break

        coverage_reason = (
            "Single-node placements only; no cross-node or recovery-budget exhaustiveness."
            + (f" Candidate budget {self.max_candidates} reached." if truncated else "")
        )
        fingerprint = _digest({"name": self.name, "limit": self.max_candidates,
                               "source": _source_hash()})
        return DecisionPlan(
            decision_id=f"decision-{_digest([fingerprint, snapshot.model_dump(mode='json')])[:32]}",
            snapshot_version=snapshot.version, decision_at=snapshot.now,
            policy=VersionedArtifact(name=self.name, version="1.0.0", sha256=fingerprint),
            model=VersionedArtifact(name="single-node-current-snapshot", version="1.0.0",
                                    sha256=_source_hash()),
            candidates=tuple(candidates),
            coverage=SearchCoverage(scope="partial", domain_description=(
                "Recorded known-demand × node × device-count combinations, bounded by max_candidates."
            ), examined_count=sum(c.feasibility != "unexamined" for c in candidates),
                truncation_reason=coverage_reason),
            selected_candidate_id=selected.candidate_id if selected else None,
            rationale=(
                f"{self.name} proposes one current-snapshot placement; kernel revalidation required."
                if selected else
                "No legal placement selected within the recorded search; not a whole-pool proof."
            ),
            tie_break_rule=(
                "B0: arrival, demand ID, node ID, device IDs. B1/B2: occupied owner share, "
                "descending priority, arrival, demand ID; B2 compatibility pressure; "
                "best-fit residual GPU count, excess VRAM, residual CPU, RAM, node/device IDs."
            ),
            asserts_pool_infeasible=False,
        )


def make_policy(name: PolicyName, *, max_candidates: int = 512):
    if name == "P":
        artifact_path = os.environ.get("FABRIC_AVAILABILITY_MODEL_PATH")
        artifact_sha = os.environ.get("FABRIC_AVAILABILITY_MODEL_SHA256")
        if bool(artifact_path) != bool(artifact_sha):
            raise ValueError("Learned availability requires both FABRIC_AVAILABILITY_MODEL_PATH "
                             "and FABRIC_AVAILABILITY_MODEL_SHA256")
        if artifact_path and artifact_sha:
            from .learned import LearnedAvailabilityPolicy
            return LearnedAvailabilityPolicy(artifact_path, expected_sha256=artifact_sha,
                                             max_candidates=max_candidates)
        from .candidate import CandidatePolicy
        return CandidatePolicy(max_candidates=max_candidates)
    return BaselinePolicy(name, max_candidates=max_candidates)
