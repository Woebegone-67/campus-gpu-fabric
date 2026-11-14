"""Isolated policy examples; not execution or competition acceptance evidence."""

from __future__ import annotations

import pytest

from campus_fabric.contracts.models import (
    MAX_SAFE_INTEGER,
    ContributorNotice,
    DecisionSnapshot,
    DemandSnapshot,
    GPUDevice,
    GPURequest,
    KeyValue,
    LeaseSnapshot,
    NodeInventory,
    Occupancy,
    Placement,
    PlacementSlice,
    ResourceAmounts,
    ResourceRequest,
    VersionStamp,
)
from campus_fabric.policies import make_policy


def device(name="gpu-a", model="A100", memory=40960, **changes):
    return GPUDevice.model_validate({
        "device_id": name, "physical_gpu_id": name, "model": model,
        "memory_mib": memory, "state": "available", **changes,
    })


def node(name="node-a", devices=None, **changes):
    devices = devices if devices is not None else (device(),)
    amounts = ResourceAmounts(cpu_millicores=8000, memory_mib=32768,
                              ephemeral_storage_mib=65536, gpu_count=len(devices),
                              gpu_memory_mib=sum(d.memory_mib for d in devices))
    return NodeInventory.model_validate({
        "node_id": name, "contributor_owner_id": "contributor-a", "state": "Ready",
        "capacity": amounts, "available": amounts, "observed_at": 0.0,
        "devices": devices, **changes,
    })


def demand(name="work-a", owner="owner-a", *, count=1, model="any", memory=1024,
           profile=None, arrived=0.0, **changes):
    return DemandSnapshot.model_validate({
        "demand_id": name, "owner_id": owner, "arrived_at": arrived,
        "request": ResourceRequest(cpu_millicores=1000, memory_mib=2048,
            gpu=GPURequest(count=count, model=model, memory_mib=memory, profile=profile)),
        **changes,
    })


def snapshot(*, demands=None, inventory=None, **changes):
    return DecisionSnapshot.model_validate({
        "version": VersionStamp(epoch=1, revision=3), "now": 10.0,
        "execution_mode": "logical_simulation",
        "demands": demands if demands is not None else (demand(),),
        "inventory": inventory if inventory is not None else (node(),), **changes,
    })


def selected(plan):
    return next((c for c in plan.candidates if c.candidate_id == plan.selected_candidate_id), None)


def test_b0_fifo_first_fit_is_stable_and_does_not_mutate_snapshot():
    state = snapshot(demands=(demand("later", arrived=2.0, priority=100),
                              demand("earlier", arrived=1.0)))
    original = state.model_dump_json()
    policy = make_policy("B0")
    plan = policy.plan(state)
    assert selected(plan).demand_id == "earlier"
    assert policy.plan(state) == plan
    assert state.model_dump_json() == original
    assert plan.snapshot_version == state.version


def test_fair_admission_uses_current_occupation_not_request_split_count():
    held = Placement(slices=(PlacementSlice(node_id="node-a",
        resources=ResourceAmounts(cpu_millicores=1000, memory_mib=1000, gpu_count=1),
        device_ids=("gpu-held",)),))
    state = snapshot(
        inventory=(node(devices=(device("gpu-held"), device("gpu-free"))),),
        demands=(demand("busy", "owner-a", arrived=0.0),
                 demand("newcomer", "owner-b", arrived=1.0)),
        occupancy=(Occupancy(allocation_id="held", owner_id="owner-a", placement=held,
                             since=0.0, status="executing"),),
    )
    assert selected(make_policy("B0").plan(state)).demand_id == "busy"
    assert selected(make_policy("B1").plan(state)).demand_id == "newcomer"


def test_b2_preserves_a_known_model_specific_opportunity():
    state = snapshot(
        inventory=(node("node-a", (device("gpu-a", "A100"),)),
                   node("node-z", (device("gpu-z", "H100"),))),
        demands=(demand("flexible", arrived=0.0),
                 demand("specific", owner="owner-b", model="A100", arrived=1.0)),
    )
    b1 = selected(make_policy("B1").plan(state))
    b2 = selected(make_policy("B2").plan(state))
    assert b1.demand_id == b2.demand_id == "flexible"
    assert b1.placement.slices[0].node_id == "node-a"
    assert b2.placement.slices[0].node_id == "node-z"


def test_all_resource_and_selector_constraints_are_explained():
    request = ResourceRequest(cpu_millicores=9000, memory_mib=40000,
        ephemeral_storage_mib=99999,
        gpu=GPURequest(count=1, model="H100", memory_mib=80000, profile="mig-2g.20gb"))
    state = snapshot(demands=(demand(request=request,
        node_selector=(KeyValue(key="rack", value="missing"),)),))
    plan = make_policy("B0").plan(state)
    assert selected(plan) is None
    reasons = {v.constraint for c in plan.candidates for v in c.violations}
    assert {"cpu", "memory", "storage", "gpu_model", "gpu_memory", "profile", "labels"} <= reasons
    assert not plan.asserts_pool_infeasible


def test_multi_gpu_request_gets_distinct_devices_and_exact_amounts():
    state = snapshot(demands=(demand(count=2),),
                     inventory=(node(devices=(device("g1"), device("g2"), device("g3"))),))
    choice = selected(make_policy("B1").plan(state))
    piece = choice.placement.slices[0]
    assert piece.device_ids == ("g1", "g2")
    assert piece.resources.gpu_count == 2
    assert piece.resources.gpu_memory_mib == 81920


def test_individually_valid_device_amounts_do_not_overflow_candidate_dto():
    amounts = ResourceAmounts(cpu_millicores=8000, memory_mib=32768,
                              gpu_count=2, gpu_memory_mib=MAX_SAFE_INTEGER)
    inventory = NodeInventory(node_id="huge-node", contributor_owner_id="contributor-a",
        state="Ready", capacity=amounts, available=amounts, observed_at=0.0,
        devices=(device("huge-a", memory=MAX_SAFE_INTEGER),
                 device("huge-b", memory=MAX_SAFE_INTEGER)))
    plan = make_policy("B1").plan(snapshot(demands=(demand(count=2),), inventory=(inventory,)))
    assert selected(plan) is None
    assert plan.candidates[0].placement is None
    assert any(v.constraint == "gpu_memory" for v in plan.candidates[0].violations)


def test_mig_and_parent_cannot_coexist_with_active_occupancy():
    whole = device("parent", state="occupied")
    child = device("slice-a", profile="mig-2g.20gb", physical_gpu_id="parent", memory=20480)
    plan = make_policy("B0").plan(snapshot(
        demands=(demand(profile="mig-2g.20gb"),), inventory=(node(devices=(whole, child)),)))
    assert selected(plan) is None
    assert any("cannot coexist" in v.detail for c in plan.candidates for v in c.violations)


def test_equal_profile_slices_can_share_parent_when_capacity_allows():
    whole = device("parent", state="unavailable")
    children = tuple(device(f"slice-{i}", profile="mig-2g.20gb", physical_gpu_id="parent",
                            memory=20480) for i in range(2))
    plan = make_policy("B0").plan(snapshot(demands=(demand(count=2, profile="mig-2g.20gb"),),
        inventory=(node(devices=(whole, *children)),)))
    assert selected(plan).placement.slices[0].device_ids == ("slice-0", "slice-1")


def test_recall_blocks_new_placement_from_notice_until_window_end():
    notice = ContributorNotice(notice_id="notice", contributor_owner_id="contributor-a",
        device_ids=("gpu-a",), issued_at=5.0, deadline=15.0, unavailable_until=30.0)
    assert selected(make_policy("B0").plan(snapshot(notices=(notice,)))) is None
    assert selected(make_policy("B0").plan(snapshot(notices=(notice,), now=30.0))) is not None


@pytest.mark.parametrize("state", ["Draining", "NotReady"])
def test_unready_nodes_are_not_selected(state):
    assert selected(make_policy("B0").plan(snapshot(inventory=(node(state=state),)))) is None


def test_search_budget_and_single_node_limit_are_explicit():
    state = snapshot(inventory=(node(devices=(device("g1"), device("g2"))),))
    plan = make_policy("B0", max_candidates=1).plan(state)
    assert plan.coverage.examined_count == 1
    assert plan.coverage.scope == "partial"
    assert "budget" in plan.coverage.truncation_reason
    assert not plan.asserts_pool_infeasible
    split = snapshot(demands=(demand(count=2),),
        inventory=(node("one", (device("g1"),)), node("two", (device("g2"),))))
    assert selected(make_policy("B0").plan(split)) is None
    assert not make_policy("B0").plan(split).asserts_pool_infeasible


def test_previously_granted_lease_does_not_invent_recovery_budget():
    placement = Placement(slices=(PlacementSlice(node_id="node-a", resources=ResourceAmounts()),))
    state = snapshot(demands=(demand(allocation_id="old-lease"),),
        leases=(LeaseSnapshot(allocation_id="old-lease", owner_id="owner-a", starts_at=0.0,
                              expires_at=100.0, placement=placement),))
    plan = make_policy("B2").plan(state)
    assert selected(plan) is None
    assert plan.candidates[0].feasibility == "unexamined"
    assert plan.coverage.examined_count == 0


def test_cpu_only_request_does_not_require_a_gpu():
    plan = make_policy("B0").plan(snapshot(demands=(demand(count=0),),
                                           inventory=(node(devices=()),)))
    assert selected(plan).placement.slices[0].resources.gpu_count == 0
