"""Single-node feasibility and derived inventory; no independent mutable free ledger."""

import json

from campus_fabric.contracts.models import (
    GPUDevice,
    KeyValue,
    NodeInventory,
    Placement,
    PlacementSlice,
    ResourceAmounts,
    ResourceRequest,
)

SCALARS = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")
HELD_STATES = {"Allocated", "Running", "PreemptRequested"}


def _model(cls, value):
    return value if isinstance(value, cls) else cls.model_validate_json(json.dumps(value))


def validate_node(node: NodeInventory, existing_nodes: tuple[NodeInventory, ...] = ()) -> None:
    """Validate newly contributed inventory, including explicit physical parents."""
    if node.node_id in {other.node_id for other in existing_nodes}:
        raise ValueError("Node identity already registered")
    if node.available != node.capacity or any(d.state != "available" for d in node.devices):
        raise ValueError("New inventory must be unclaimed; availability is kernel-derived")
    existing_ids = {d.device_id for other in existing_nodes for d in other.devices}
    existing_parents = {d.physical_gpu_id for other in existing_nodes for d in other.devices}
    if any(d.device_id in existing_ids or d.physical_gpu_id in existing_parents for d in node.devices):
        raise ValueError("GPU identities must be globally unique")
    parents = {d.device_id: d for d in node.devices if d.profile is None}
    if node.capacity.gpu_count != len(parents):
        raise ValueError("GPU capacity must count physical parents exactly once")
    if node.capacity.gpu_memory_mib != sum(d.memory_mib for d in parents.values()):
        raise ValueError("GPU memory capacity must equal physical parent memory")
    slices: dict[str, int] = {}
    for device in node.devices:
        if device.profile is None:
            continue
        parent = parents.get(device.physical_gpu_id)
        if parent is None or device.model != parent.model:
            raise ValueError("A slice needs an explicit, same-model physical parent")
        if device.memory_mib <= 0:
            raise ValueError("Slice memory must be positive")
        slices[parent.device_id] = slices.get(parent.device_id, 0) + device.memory_mib
    if any(amount > parents[parent].memory_mib for parent, amount in slices.items()):
        raise ValueError("Configured slices exceed physical GPU memory")


def iter_inventory(nodes, allocations, now: float, node_ids=None):
    """Derive free resources from unique live claims, detecting corrupt overcommit.

    A physical parent's whole-GPU view is unavailable while any of its slices is
    occupied. Free GPU count counts completely free physical parents; slice
    placement uses the device inventory instead. Free VRAM counts physical memory
    once, even when both a whole device and its slice representations are exposed.
    """
    # Index current claims once. Re-parsing every placement for every node made
    # a single inventory read grow as nodes * allocations, including unrelated
    # nodes. The index is local to this derivation, never a second mutable ledger.
    # Key enumeration does not decode lazy node payloads. Checking membership on
    # the lazy mapping itself would fetch every claimed node before iteration.
    known_nodes = set(nodes)
    claims_by_node: dict[str, list[PlacementSlice]] = {}
    for allocation in allocations.values():
        if allocation["state"] in HELD_STATES and allocation.get("placement") is not None:
            placement = _model(Placement, allocation["placement"])
            for part in placement.slices:
                if part.node_id not in known_nodes:
                    raise ValueError("Live claim refers to an unknown node")
                claims_by_node.setdefault(part.node_id, []).append(part)
    selected = known_nodes if node_ids is None else set(node_ids)
    if not selected <= known_nodes:
        raise ValueError("Resource validation refers to an unknown node")
    for key in sorted(selected):
        node = _model(NodeInventory, nodes[key])
        if key != node.node_id:
            raise ValueError("Inventory node key mismatch")
        used = dict.fromkeys(SCALARS, 0)
        claimed: set[str] = set()
        devices = {d.device_id: d for d in node.devices}
        for part in claims_by_node.get(node.node_id, ()):
            for dimension in SCALARS:
                used[dimension] += getattr(part.resources, dimension)
            for device_id in part.device_ids:
                if device_id not in devices or device_id in claimed:
                    raise ValueError("Unknown or multiply claimed GPU")
                claimed.add(device_id)
        parent_claims: dict[str, list[GPUDevice]] = {}
        for device_id in claimed:
            device = devices[device_id]
            parent_claims.setdefault(device.physical_gpu_id, []).append(device)
        for parent_id, claims in parent_claims.items():
            if any(d.profile is None for d in claims) and len(claims) > 1:
                raise ValueError("Whole GPU and slice claims overlap")
            if sum(d.memory_mib for d in claims) > devices[parent_id].memory_mib:
                raise ValueError("GPU memory overcommitted")
        free = {name: getattr(node.capacity, name) - used[name] for name in SCALARS}
        if min(free.values()) < 0:
            raise ValueError("Node resources overcommitted")
        view_devices = []
        for device in node.devices:
            claims = parent_claims.get(device.physical_gpu_id, [])
            parent = devices[device.physical_gpu_id]
            blocked = (
                node.state != "Ready"
                or device.state in {"quarantined", "unavailable"}
                or parent.state in {"quarantined", "unavailable"}
            )
            if device.device_id in claimed:
                state = "occupied"
            elif blocked:
                state = "quarantined" if device.state == "quarantined" else "unavailable"
            elif (device.profile is None and claims) or any(d.profile is None for d in claims):
                state = "unavailable"
            else:
                state = "available"
            view_devices.append(device.model_copy(update={"state": state}))
        available_parents = {
            d.device_id for d in view_devices if d.profile is None and d.state == "available"
        }
        free_memory = 0
        if node.state == "Ready":
            for parent in (d for d in node.devices if d.profile is None):
                if parent.state == "available":
                    free_memory += parent.memory_mib - sum(
                        d.memory_mib for d in parent_claims.get(parent.device_id, [])
                    )
        if node.state != "Ready":
            free = dict.fromkeys(SCALARS, 0)
        free.update(gpu_count=len(available_parents), gpu_memory_mib=free_memory)
        yield node.model_copy(update={
            "available": ResourceAmounts(**free), "devices": tuple(view_devices), "observed_at": now,
        })


def inventory(nodes: dict, allocations: dict, now: float) -> tuple[NodeInventory, ...]:
    """Fully fold all current claims; retained for reads and conservative audits."""
    return tuple(iter_inventory(nodes, allocations, now))


def _fits_node(request: ResourceRequest, selectors: tuple[KeyValue, ...], node: NodeInventory):
    labels = {item.key: item.value for item in node.labels}
    return (
        node.state == "Ready"
        and all(labels.get(item.key) == item.value for item in selectors)
        and all(getattr(request, key) <= getattr(node.available, key) for key in SCALARS)
    )


def _eligible(device: GPUDevice, request: ResourceRequest) -> bool:
    return (
        device.state == "available"
        and (request.gpu.model == "any" or device.model == request.gpu.model)
        and device.profile == request.gpu.profile
        and device.memory_mib >= request.gpu.memory_mib
    )


def _resources(request: ResourceRequest, devices: tuple[GPUDevice, ...]) -> ResourceAmounts:
    return ResourceAmounts(
        **{name: getattr(request, name) for name in SCALARS},
        gpu_count=len(devices), gpu_memory_mib=sum(d.memory_mib for d in devices),
    )


def choose_placement(
    request: ResourceRequest,
    node_selector: tuple[KeyValue, ...],
    inventory: tuple[NodeInventory, ...],
) -> Placement | None:
    """Deterministic first-fit node/device order; no workload duration input."""
    return choose_placement_ordered(
        request, node_selector, sorted(inventory, key=lambda item: item.node_id)
    )


def choose_placement_ordered(request: ResourceRequest, node_selector, nodes) -> Placement | None:
    """First-fit over an already ordered stream, deriving no unused node views."""
    for node in nodes:
        if not _fits_node(request, node_selector, node):
            continue
        choices = tuple(
            device for device in sorted(node.devices, key=lambda item: item.device_id)
            if _eligible(device, request)
        )
        if len(choices) < request.gpu.count:
            continue
        selected = choices[:request.gpu.count]
        placement = Placement(slices=(PlacementSlice(
            node_id=node.node_id, resources=_resources(request, selected),
            device_ids=tuple(d.device_id for d in selected),
        ),))
        validate_placement(request, node_selector, placement, (node,))
        return placement
    return None


def validate_placement(
    request: ResourceRequest,
    selectors: tuple[KeyValue, ...],
    placement: Placement,
    inventory: tuple[NodeInventory, ...],
) -> None:
    """Independently recheck all hard constraints before committing a proposed plan."""
    if len(placement.slices) != 1:
        raise ValueError("CP1 placement must keep all requested resources on one node")
    part = placement.slices[0]
    node = next((n for n in inventory if n.node_id == part.node_id), None)
    if node is None or not _fits_node(request, selectors, node):
        raise ValueError("Node cannot satisfy resources or selectors")
    devices = {d.device_id: d for d in node.devices}
    if len(part.device_ids) != request.gpu.count:
        raise ValueError("GPU count does not match request")
    selected = []
    for device_id in part.device_ids:
        device = devices.get(device_id)
        if device is None or not _eligible(device, request):
            raise ValueError("GPU shape, memory, profile or availability does not match")
        selected.append(device)
    if part.resources != _resources(request, tuple(selected)):
        raise ValueError("Placement scalar resources or device memory do not match request")
