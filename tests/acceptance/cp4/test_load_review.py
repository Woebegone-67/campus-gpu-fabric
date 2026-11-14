"""Negative controls for the independent CP4 resource-history reviewer."""
import copy

import pytest
from _load_runner import (
    complete_query_sample,
    fold_ledger,
    inventory,
    reference_limits,
    verify_renewal,
)


def history():
    node = inventory()[0]
    device = node["devices"][0]["device_id"]
    delta = {"node_id": node["node_id"], "allocation_id": "a1", "cpu_millicores": 1000,
             "memory_mib": 1024, "ephemeral_storage_mib": 32,
             "device_ids_added": [device], "device_ids_removed": []}
    release = {**delta, "cpu_millicores": -1000, "memory_mib": -1024,
               "ephemeral_storage_mib": -32, "device_ids_added": [], "device_ids_removed": [device]}
    state = {"allocations": {}, "events": [
        {"sequence": 1, "event_id": "ev1", "resource_deltas": [delta]},
        {"sequence": 2, "event_id": "ev2", "resource_deltas": [release]}]}
    return [node], state


def test_complete_return_has_no_leak():
    nodes, state = history()
    assert fold_ledger(nodes, state)["final_held_physical_gpus"] == 0


def test_duplicate_physical_gpu_is_rejected():
    nodes, state = history()
    duplicate = copy.deepcopy(state["events"][0]["resource_deltas"][0])
    duplicate["allocation_id"] = "a2"
    state["events"][0]["resource_deltas"].append(duplicate)
    with pytest.raises(AssertionError, match="double allocation"):
        fold_ledger(nodes, state)


def test_capacity_oversell_is_rejected_even_if_later_released():
    nodes, state = history()
    state["events"][0]["resource_deltas"][0]["cpu_millicores"] = 32001
    with pytest.raises(AssertionError, match="oversell"):
        fold_ledger(nodes, state)


def test_duplicate_return_is_rejected():
    nodes, state = history()
    state["events"].append({**copy.deepcopy(state["events"][1]), "sequence": 3, "event_id": "ev3"})
    with pytest.raises(AssertionError, match="double return"):
        fold_ledger(nodes, state)


def test_missing_event_cannot_pass_zero_final_occupancy():
    nodes, state = history()
    state["events"][1]["sequence"] = 3
    with pytest.raises(AssertionError, match="order"):
        fold_ledger(nodes, state)


def test_final_state_disagreement_is_rejected():
    nodes, state = history()
    state["events"] = state["events"][:1]
    with pytest.raises(AssertionError, match="differs"):
        fold_ledger(nodes, state)


def test_claimed_reference_environment_needs_actual_limits():
    assert not reference_limits({"reference_4vcpu_8gib": True})
    assert not reference_limits({"cpu_max": "max 100000", "memory_max": "8589934592"})
    assert not reference_limits({"cpu_max": "0 0", "memory_max": "8589934592"})
    assert not reference_limits({"cpu_max": "400000 100000", "memory_max": "max"})
    assert reference_limits({"cpu_max": "400000 100000", "memory_max": "8589934592"})


def test_renewal_needs_an_actual_deadline_change():
    verify_renewal("2026-01-01T00:05:00Z", "2026-01-01T00:10:00Z")
    with pytest.raises(AssertionError, match="extend"):
        verify_renewal("2026-01-01T00:05:00Z", "2026-01-01T00:05:00Z")


def test_deleted_or_replaced_queries_cannot_hide_query_latency():
    flows = [{"index": i, "job_id": f"job-{i}"} for i in range(10000)]
    queries = {f"job-{i}" for i in range(0, 10000, 8)}
    assert complete_query_sample(flows, 1250, queries, 10000)
    assert not complete_query_sample(flows, 0, set(), 10000)
    assert not complete_query_sample(flows, 1249, queries - {"job-0"}, 10000)
    assert not complete_query_sample(flows, 1250, queries - {"job-0"} | {"job-1"}, 10000)
