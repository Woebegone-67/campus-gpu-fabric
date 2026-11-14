"""Independent, deliberately bounded fold of committed resource deltas.

No production reducer, placement checker, Kernel or policy is imported here.
The input is frozen initial inventory plus committed events. This reconstructs
resource claims and explicit state facts, NOT complete database objects.
"""
from __future__ import annotations

DIMENSIONS = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")


def fold_events(inventory, events):
    capacities = {node["node_id"]: node["capacity"] for node in inventory}
    devices = {node["node_id"]: {d["device_id"] for d in node["devices"]} for node in inventory}
    claims, explicit_states, seen_ids = {}, {}, set()
    previous_version, previous_time = (0, 0), 0.0
    for expected_sequence, event in enumerate(events, start=1):
        assert event["sequence"] == expected_sequence, "Missing, duplicated or reordered committed event"
        assert event["event_id"] not in seen_ids, "Duplicate event identity"
        seen_ids.add(event["event_id"])
        version = (event["version"]["epoch"], event["version"]["revision"])
        assert version[0] == 0 and version >= previous_version, "Unexpected epoch or version reversal"
        assert event["logical_time"] >= previous_time, "Logical time reversal"
        previous_version, previous_time = version, event["logical_time"]
        for change in event["state_changes"]:
            entity = change["entity"]
            # A stage_bound event uses its intent state for every referenced
            # entity; checkpoint.ready is not an allocation lifecycle state.
            # Only record facts, never blindly overwrite another entity state.
            if event["event_type"].startswith(entity["kind"] + "."):
                explicit_states[(entity["kind"], entity["entity_id"])] = change["after"]
        for delta in event["resource_deltas"]:
            node, allocation = delta["node_id"], delta["allocation_id"]
            assert node in capacities, "Delta refers to an unknown initial node"
            claim = claims.setdefault((node, allocation), {**dict.fromkeys(DIMENSIONS, 0), "devices": set()})
            for dimension in DIMENSIONS:
                claim[dimension] += delta[dimension]
                assert claim[dimension] >= 0, "Resource release exceeds the held claim"
            added, removed = set(delta["device_ids_added"]), set(delta["device_ids_removed"])
            assert added <= devices[node] and removed <= devices[node], "Unknown device"
            assert not (added & claim["devices"]), "Device added twice to one allocation"
            assert removed <= claim["devices"], "Device release has no preceding grant"
            claim["devices"].difference_update(removed)
            claim["devices"].update(added)
            # Check every intermediate state, including multiple deltas per event.
            node_claims = [v for (n, _), v in claims.items() if n == node]
            for dimension in DIMENSIONS:
                assert sum(c[dimension] for c in node_claims) <= capacities[node][dimension], "Capacity exceeded"
            held_devices = [device for c in node_claims for device in c["devices"]]
            assert len(held_devices) == len(set(held_devices)), "Two allocations hold the same device"
    live = {f"{node}/{allocation}": {**{d: c[d] for d in DIMENSIONS}, "devices": sorted(c["devices"])}
            for (node, allocation), c in claims.items()
            if any(c[d] for d in DIMENSIONS) or c["devices"]}
    return {"claims": live, "explicit_states": [
        {"kind": k, "id": i, "state": s} for (k, i), s in sorted(explicit_states.items())],
        "event_count": len(events), "last_event_time": previous_time,
        "last_event_version": list(previous_version)}


def claims_from_final_snapshot(snapshot):
    """Extract a comparison projection; never used as fold input."""
    result = {}
    for allocation in snapshot["allocations"]:
        placement = allocation.get("placement")
        if not placement:
            continue
        for piece in placement["slices"]:
            if not any(piece["resources"][d] for d in DIMENSIONS) and not piece["device_ids"]:
                continue
            result[f"{piece['node_id']}/{allocation['allocation_id']}"] = {
                **{d: piece["resources"][d] for d in DIMENSIONS}, "devices": sorted(piece["device_ids"])}
    return result
