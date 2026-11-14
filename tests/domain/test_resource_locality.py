"""Resource induction, lazy first-fit equivalence and conservative fallback."""

import copy
import sqlite3

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.domain.placement import (
    choose_placement,
    choose_placement_ordered,
    iter_inventory,
)
from campus_fabric.storage import journal
from campus_fabric.storage.sqlite import SQLiteStore
from campus_fabric.storage.state import LazyMap, materialize

ALICE = m.AuthenticatedActor(actor_id="alice", role="member", owner_id="alice")
TEST = m.AuthenticatedActor(actor_id="test", role="test_driver")


def node(index=0, *, state="Ready"):
    name, gpu = f"n{index:03}", f"g{index:03}"
    capacity = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                                ephemeral_storage_mib=1024, gpu_count=1,
                                gpu_memory_mib=16384)
    return m.NodeInventory(node_id=name, contributor_owner_id="supplier", state=state,
        capacity=capacity, available=capacity, observed_at=0.0,
        labels=(m.KeyValue(key="zone", value="east" if index % 2 == 0 else "west"),),
        devices=(m.GPUDevice(device_id=gpu, physical_gpu_id=gpu, model="A100",
                            memory_mib=16384, state="available"),
                 *(m.GPUDevice(device_id=f"{gpu}-s{x}", physical_gpu_id=gpu, model="A100",
                               memory_mib=8192, profile="half", state="available")
                   for x in range(2))))


def commit(kernel, params, actor=ALICE):
    receipt = kernel.commit(m.CommandEnvelope(request_id="locality", actor=actor,
                                               operation=params.kind, parameters=params))
    assert receipt.outcome != "rejected", receipt
    return receipt


def allocate(kernel, *, cpu=1000, gpu=0):
    receipt = commit(kernel, m.AllocationCreateParameters(kind="allocation.create",
        owner_id="alice", ttl_seconds=300, request=m.ResourceRequest(
            cpu_millicores=cpu, memory_mib=256, gpu=m.GPURequest(count=gpu))))
    return next(x.entity_id for x in receipt.objects if x.kind == "allocation")


def track_node_decodes(monkeypatch):
    decoded = []
    original = LazyMap.__getitem__

    def tracked(self, key):
        if self.name == "nodes" and key not in self._cache:
            decoded.append(key)
        return original(self, key)

    monkeypatch.setattr(LazyMap, "__getitem__", tracked)
    return decoded


def test_projected_changes_cover_nested_delete_insert_and_detach_observations(tmp_path):
    store = SQLiteStore(tmp_path / "projection.sqlite")
    store.initialize({"rows": {"a": {"resource": {"cpu": 1}, "metadata": 0},
                              "b": {"resource": {"cpu": 2}}}})
    def project(row):
        return row["resource"]

    with store.transaction() as conn:
        state = store.load(conn)
        assert state.projected_map_changes("rows", project) == ()
        state["rows"]["a"]["metadata"] = 99
        assert state.projected_map_changes("rows", project) == ()
        state["rows"]["a"]["resource"]["cpu"] = 3
        del state["rows"]["b"]
        state["rows"]["c"] = {"resource": {"cpu": 4}}
        observed = state.projected_map_changes("rows", project)
        assert observed == (("a", {"cpu": 1}, {"cpu": 3}),
                            ("b", {"cpu": 2}, None), ("c", None, {"cpu": 4}))
        observed[0][1]["cpu"] = 800
        observed[0][2]["cpu"] = 900
        assert state.projected_map_changes("rows", project)[0] == ("a", {"cpu": 1}, {"cpu": 3})
        store.save(conn, state)
        assert state.projected_map_changes("rows", project) == ()
        state["rows"]["a"]["resource"]["cpu"] = 5
        assert state.projected_map_changes("rows", project)[0] == ("a", {"cpu": 3}, {"cpu": 5})


@pytest.mark.parametrize("change", ["replace", "delete", "alias", "move", "detach"])
def test_complex_container_changes_have_no_bounded_proof(tmp_path, change):
    store = SQLiteStore(tmp_path / "complex.sqlite")
    store.initialize({"rows": {"a": {"x": 1}}})
    with store.transaction() as conn:
        state = store.load(conn)
        if change == "replace":
            state["rows"] = materialize(state["rows"])
        elif change == "delete":
            del state["rows"]
        elif change == "alias":
            state["other"] = state["rows"]
        elif change == "move":
            state["other"] = state.pop("rows")
        else:
            state["rows"].detach()
        assert state.projected_map_changes("rows", lambda row: row) is None


def test_projection_keeps_canonical_numeric_distinctions(tmp_path):
    store = SQLiteStore(tmp_path / "numbers.sqlite")
    store.initialize({"rows": {"a": {"value": 0.0}}})
    with store.transaction() as conn:
        state = store.load(conn)
        state["rows"]["a"]["value"] = -0.0
        changes = state.projected_map_changes("rows", lambda row: row)
        assert journal.encode(changes) == '[["a",{"value":0.0},{"value":-0.0}]]'


@pytest.mark.parametrize("count,profile,model,cpu,zone", [
    (0, None, "any", 1000, None), (1, None, "A100", 1000, None),
    (1, "half", "A100", 1000, None), (2, "half", "any", 1000, "west"),
    (1, None, "H100", 1000, None), (1, None, "any", 4001, None),
    (1, None, "any", 1000, "missing"), (0, None, "any", 4000, "east"),
])
def test_streaming_first_fit_preserves_full_reference_choice(count, profile, model, cpu, zone):
    nodes = {n.node_id: n.model_dump(mode="json")
             for n in (node(3), node(2, state="NotReady"), node(1), node(0))}
    occupied = choose_placement(m.ResourceRequest(cpu_millicores=1000, memory_mib=256,
        gpu=m.GPURequest(count=1, profile="half")), (), (node(0),))
    claims = {"a": {"state": "Running", "placement": occupied.model_dump(mode="json")}}
    request = m.ResourceRequest(cpu_millicores=cpu, memory_mib=256,
                                gpu=m.GPURequest(count=count, profile=profile, model=model))
    selectors = () if zone is None else (m.KeyValue(key="zone", value=zone),)
    complete = tuple(iter_inventory(nodes, claims, 7.0))
    assert choose_placement_ordered(request, selectors, iter_inventory(nodes, claims, 7.0)) == (
        choose_placement(request, selectors, complete))


def test_first_fit_decodes_one_of_128_nodes_then_metadata_writes_decode_none(tmp_path, monkeypatch):
    kernel = Kernel(tmp_path / "pool.sqlite", inventory=tuple(node(i) for i in reversed(range(128))))
    decoded = track_node_decodes(monkeypatch)
    aid = allocate(kernel)
    assert decoded == ["n000"]
    decoded.clear()
    receipt = commit(kernel, m.JobCreateParameters(kind="job.create", allocation_id=aid,
                                                   image="reference", command=("run",)))
    jid = next(x.entity_id for x in receipt.objects if x.kind == "job")
    commit(kernel, m.AllocationRenewParameters(kind="allocation.renew", allocation_id=aid,
                                               ttl_seconds=100))
    commit(kernel, m.ClockAdvanceParameters(kind="clock.advance", advance_seconds=1.0,
                                             source="test_control"), TEST)
    commit(kernel, m.JobCancelParameters(kind="job.cancel", job_id=jid))
    commit(kernel, m.FaultParameters(kind="test.fault", target_operation="create",
                                     mode="503", count=1), TEST)
    assert kernel.take_fault("create") == {"mode": "503", "delay_ms": 0}
    assert decoded == []
    state = kernel.store.read()
    assert state["allocations"][aid]["state"] == "Allocated"
    assert state["allocations"][aid]["placement"]["slices"][0]["node_id"] == "n000"


def test_move_and_delete_validate_old_and_new_nodes_against_current_claims(tmp_path, monkeypatch):
    kernel = Kernel(tmp_path / "move.sqlite", inventory=(node(0), node(1), node(2)))
    aid = allocate(kernel)
    decoded = track_node_decodes(monkeypatch)
    with kernel.store.transaction() as conn:
        state = kernel.store.load(conn)
        state["allocations"][aid]["placement"]["slices"][0]["node_id"] = "n001"
        kernel._save_state(conn, state)
    assert decoded == ["n000", "n001"]
    decoded.clear()
    with kernel.store.transaction() as conn:
        state = kernel.store.load(conn)
        del state["allocations"][aid]
        kernel._save_state(conn, state)
    assert decoded == ["n001"]
    final = kernel.store.read()
    assert all(n.available == n.capacity for n in kernel._inventory(final))


def test_nested_capacity_overclaim_rolls_back_state_and_journal(tmp_path):
    kernel = Kernel(tmp_path / "overclaim.sqlite", inventory=(node(0), node(1)))
    aid = allocate(kernel)
    before, log = kernel.store.read(), kernel.store.export_journal()
    with pytest.raises(ValueError, match="overcommitted"):
        with kernel.store.transaction() as conn:
            state = kernel.store.load(conn)
            state["allocations"][aid]["placement"]["slices"][0]["resources"]["cpu_millicores"] = 4001
            kernel._save_state(conn, state)
    assert kernel.store.read() == before
    assert kernel.store.export_journal() == log


def test_plain_state_and_structural_replacement_take_full_audit(tmp_path, monkeypatch):
    kernel = Kernel(tmp_path / "full.sqlite", inventory=(node(0), node(1)))
    original, audited = kernel._audit_resources, []

    def audit(state):
        audited.append(True)
        return original(state)

    monkeypatch.setattr(kernel, "_audit_resources", audit)
    kernel._validate_resources(kernel.store.read())
    with kernel.store.transaction() as conn:
        state = kernel.store.load(conn)
        state["allocations"] = materialize(state["allocations"])
        kernel._save_state(conn, state)
    assert len(audited) == 2


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("corruption", ["overclaim", "duplicate_physical", "invalid_slices"])
def test_startup_and_migration_refuse_integrity_valid_resource_corruption(tmp_path, legacy, corruption):
    kernel = Kernel(tmp_path / "source.sqlite", inventory=(node(0), node(1)))
    aid = allocate(kernel)
    state = kernel.store.read()
    if corruption == "overclaim":
        state["allocations"][aid]["placement"]["slices"][0]["resources"]["cpu_millicores"] = 4001
    elif corruption == "duplicate_physical":
        state["nodes"]["n001"]["devices"] = copy.deepcopy(state["nodes"]["n000"]["devices"])
    else:
        state["nodes"]["n001"]["devices"][1]["memory_mib"] = 16000
    path = tmp_path / "corrupt.sqlite"
    if legacy:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE fabric_state (singleton INTEGER PRIMARY KEY "
                         "CHECK(singleton=1), payload TEXT NOT NULL)")
            conn.execute("INSERT INTO fabric_state VALUES(1,?)", (journal.encode(state),))
            conn.execute("PRAGMA user_version=1")
    else:
        SQLiteStore(path).initialize(state)
    with pytest.raises(ValueError):
        Kernel(path)
    # Integrity alone is deliberately insufficient: no data are overwritten.
    assert SQLiteStore(path).read() == state


def test_restart_accepts_legitimate_recall_and_unavailable_devices(tmp_path):
    path = tmp_path / "recall.sqlite"
    kernel = Kernel(path, inventory=(node(0),))
    aid = allocate(kernel, gpu=1)
    commit(kernel, m.RecallParameters(kind="test.contributor_demand", device_ids=("g000",),
                                       notice_seconds=10, duration_seconds=20), TEST)
    before = kernel.store.read()
    assert before["allocations"][aid]["state"] == "PreemptRequested"
    reopened = Kernel(path)
    assert reopened.store.read() == before
    assert reopened.decision_snapshot().inventory[0].available.gpu_count == 0
