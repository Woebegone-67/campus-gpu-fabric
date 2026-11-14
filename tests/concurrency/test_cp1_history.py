"""Concurrent command histories checked without the scheduler or ledger implementation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from campus_fabric.domain.kernel import Kernel

from campus_fabric.contracts.models import (
    AllocationCreateParameters,
    AllocationTargetParameters,
    AuthenticatedActor,
    CommandEnvelope,
    GPUDevice,
    GPURequest,
    NodeInventory,
    ReadQuery,
    ResourceAmounts,
    ResourceRequest,
)

ADMIN = AuthenticatedActor(actor_id="audit", role="administrator")


def node():
    capacity = ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                               ephemeral_storage_mib=1024, gpu_count=1,
                               gpu_memory_mib=8192)
    return NodeInventory(
        node_id="n1", contributor_owner_id="supplier", state="Ready",
        capacity=capacity, available=capacity, observed_at=0.0,
        devices=(GPUDevice(device_id="g1", physical_gpu_id="g1", model="A10",
                           memory_mib=8192, state="available"),),
    )


def actor(owner):
    return AuthenticatedActor(actor_id=owner, owner_id=owner, role="researcher")


def create(kernel, owner, request_id, key, *, cpu=1000):
    parameters = AllocationCreateParameters(
        kind="allocation.create", owner_id=owner, ttl_seconds=300,
        request=ResourceRequest(cpu_millicores=cpu, memory_mib=1024,
                                ephemeral_storage_mib=32,
                                gpu=GPURequest(count=1, model="A10", memory_mib=4096)),
    )
    return kernel.commit(CommandEnvelope(
        request_id=request_id, actor=actor(owner), operation="allocation.create",
        parameters=parameters, idempotency_key=key,
    ))


def all_state(kernel):
    return kernel.read_snapshot(ReadQuery(actor=ADMIN, global_scope=True))


def audit_snapshot(kernel):
    snapshot = all_state(kernel)
    inventory = kernel.decision_snapshot().inventory
    for n in inventory:
        held = {"cpu_millicores": 0, "memory_mib": 0, "ephemeral_storage_mib": 0}
        device_ids = []
        for allocation in snapshot.allocations:
            if allocation.state not in ("Allocated", "Running"):
                continue
            assert allocation.placement is not None
            for placement in allocation.placement.slices:
                if placement.node_id != n.node_id:
                    continue
                for resource in held:
                    held[resource] += getattr(placement.resources, resource)
                device_ids.extend(placement.device_ids)
        for resource, quantity in held.items():
            assert 0 <= quantity <= getattr(n.capacity, resource)
            assert quantity + getattr(n.available, resource) == getattr(n.capacity, resource)
        assert len(device_ids) == len(set(device_ids)), "The same GPU is held twice"
        assert len(device_ids) + n.available.gpu_count == n.capacity.gpu_count
    sequences = [event.sequence for event in snapshot.events]
    assert sequences == sorted(set(sequences))
    assert len({event.event_id for event in snapshot.events}) == len(snapshot.events)
    return snapshot


def simultaneous(kernels, fn):
    barrier = Barrier(len(kernels))

    def call(index):
        barrier.wait(timeout=30)
        return fn(kernels[index], index)

    with ThreadPoolExecutor(max_workers=len(kernels)) as pool:
        return list(pool.map(call, range(len(kernels))))


def test_32_independent_connections_compete_for_last_gpu(tmp_path):
    path = tmp_path / "last.sqlite3"
    Kernel(path, inventory=(node(),))
    kernels = [Kernel(path) for _ in range(32)]
    receipts = simultaneous(kernels, lambda k, i: create(k, f"owner{i}", f"request{i}", "key"))
    assert all(r.outcome == "committed" for r in receipts)
    snapshot = audit_snapshot(kernels[0])
    assert len(snapshot.allocations) == 32
    assert sum(a.state == "Allocated" for a in snapshot.allocations) == 1
    assert sum(a.state == "Pending" for a in snapshot.allocations) == 31
    assert len({r.objects[0].entity_id for r in receipts}) == 32


def test_32_identical_keys_create_one_effect_and_survive_restart(tmp_path):
    path = tmp_path / "identical.sqlite3"
    Kernel(path, inventory=(node(),))
    kernels = [Kernel(path) for _ in range(32)]
    receipts = simultaneous(kernels, lambda k, i: create(k, "alice", f"request{i}", "stable"))
    assert all(r.outcome != "rejected" for r in receipts)
    ids = {next(o.entity_id for o in r.objects if o.kind == "allocation") for r in receipts}
    assert len(ids) == 1
    assert len(audit_snapshot(kernels[0]).allocations) == 1
    reopened = Kernel(path)
    replay = create(reopened, "alice", "response-was-lost", "stable")
    assert next(o.entity_id for o in replay.objects if o.kind == "allocation") in ids
    assert len(audit_snapshot(reopened).allocations) == 1


def test_same_key_conflicting_payload_cannot_race_into_two_effects(tmp_path):
    path = tmp_path / "conflict.sqlite3"
    Kernel(path, inventory=(node(),))
    kernels = [Kernel(path) for _ in range(32)]
    receipts = simultaneous(kernels, lambda k, i: create(
        k, "alice", f"request{i}", "stable", cpu=1000 if i % 2 else 2000))
    rejected = [r for r in receipts if r.outcome == "rejected"]
    accepted = [r for r in receipts if r.outcome != "rejected"]
    assert len(accepted) == len(rejected) == 16
    assert all(r.error.code == "idempotency_conflict" for r in rejected)
    assert len(audit_snapshot(kernels[0]).allocations) == 1


def test_repeated_release_and_waiter_grants_conserve_event_ledger(tmp_path):
    path = tmp_path / "history.sqlite3"
    current = Kernel(path, inventory=(node(),))
    for i in range(8):
        create(current, f"owner{i}", f"create{i}", "request")
    for i in range(8):
        snapshot = audit_snapshot(current)
        allocation = next(a for a in snapshot.allocations if a.state == "Allocated")
        for retry in range(2):
            current.commit(CommandEnvelope(
                request_id=f"release{i}-{retry}", actor=actor(allocation.owner_id),
                operation="allocation.release",
                parameters=AllocationTargetParameters(
                    kind="allocation.release", allocation_id=allocation.allocation_id),
            ))
        audit_snapshot(current)
    snapshot = all_state(current)
    # Reconstruct HELD quantities from event deltas, never read production free counters.
    held = {"cpu_millicores": 0, "memory_mib": 0, "ephemeral_storage_mib": 0}
    active = set()
    total_claims = 0
    for event in snapshot.events:
        for delta in event.resource_deltas:
            assert delta.node_id == "n1"
            for resource in held:
                held[resource] += getattr(delta, resource)
                assert 0 <= held[resource] <= getattr(node().capacity, resource)
            for device_id in delta.device_ids_added:
                assert device_id not in active
                active.add(device_id)
                total_claims += 1
            for device_id in delta.device_ids_removed:
                assert device_id in active
                active.remove(device_id)
    assert total_claims == 8, "Every actual claim must have a ledger event"
    assert not active and all(v == 0 for v in held.values())
    assert all(a.state == "Released" for a in snapshot.allocations)
