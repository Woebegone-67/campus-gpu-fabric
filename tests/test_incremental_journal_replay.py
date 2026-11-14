"""Independent verifier controls; synthetic histories are not acceptance evidence."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "tests/acceptance/cp3/cp3_state_replay.py"
spec = importlib.util.spec_from_file_location("standalone_incremental_replay", PATH)
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)


def journal(states, legacy_count=0, *, export_schema=None):
    entries, before, previous = [], None, None
    for number, state in enumerate(states, 1):
        record = {"sequence": number, "origin": "initialize" if number == 1 else "write",
                  "previous_record_sha256": previous}
        if number <= legacy_count:
            record.update(schema=reducer.V1,
                          before_sha256=None if before is None else reducer.checksum(before),
                          after_sha256=reducer.checksum(state),
                          changes=[{"op": "set", "path": [], "value": copy.deepcopy(state)}])
        else:
            old = {} if before is None else reducer.cells_from_state(before)
            new = reducer.cells_from_state(state)
            changes = [{"op": "delete", "key": key} for key in sorted(old.keys() - new.keys())]
            changes += [{"op": "set", "key": key, "value": copy.deepcopy(new[key])}
                        for key in sorted(new) if key not in old
                        or reducer.canonical(old[key]) != reducer.canonical(new[key])]
            record.update(schema=reducer.V2, changes=changes,
                          before_root=None if before is None else reducer.cell_root(old),
                          after_root=reducer.cell_root(new))
        record["record_sha256"] = reducer.checksum(record)
        entries.append(record)
        before, previous = state, record["record_sha256"]
    result = {"schema": export_schema or reducer.V2, "entries": entries,
              "head_sequence": len(entries), "head_record_sha256": previous,
              "state_sha256": reducer.checksum(states[-1])}
    if result["schema"] == reducer.V2:
        result["state_root"] = reducer.cell_root(reducer.cells_from_state(states[-1]))
    return result


def resign(export):
    previous = None
    for record in export["entries"]:
        record["previous_record_sha256"] = previous
        record["record_sha256"] = reducer.checksum({k: v for k, v in record.items() if k != "record_sha256"})
        previous = record["record_sha256"]
    export["head_record_sha256"] = previous


def reference_root(cells):
    """Small recursive mathematical oracle, distinct from the stack implementation."""
    if not cells:
        return hashlib.sha256(b"fabric-state-empty-v2").hexdigest()
    key = min(cells, key=lambda value: (
        hashlib.sha256(b"fabric-state-priority-v2\0" + value.encode()).hexdigest(), value))
    value_hash = hashlib.sha256(b"fabric-state-value-v2\0" + reducer.canonical(cells[key]).encode()).hexdigest()
    left = reference_root({k: v for k, v in cells.items() if k < key})
    right = reference_root({k: v for k, v in cells.items() if k > key})
    return hashlib.sha256(b"fabric-state-node-v2\0" + reducer.canonical([key, value_hash, left, right]).encode()).hexdigest()


def test_standalone_reducer_has_no_production_import_or_serializer():
    tree = ast.parse(PATH.read_text())
    imports = {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imports <= {"__future__", "copy", "hashlib", "json"}


def test_cells_preserve_all_fields_types_empty_containers_and_ambiguous_keys():
    state = {"unknown": {"nested": {"never_whitelisted": [False, 0, 0.0, -0.0]}},
             "empty_map": {}, "empty_list": [], "null": None,
             "a/b": {"0": "string key", "[\"x\"]": [1]}, "点": [{"x": 1}, []]}
    cells = reducer.cells_from_state(state)
    assert reducer.checksum(reducer.state_from_cells(cells)) == reducer.checksum(state)
    assert reducer.cell_root(cells) == reference_root(cells)
    assert reducer.cell_root(dict(reversed(list(cells.items())))) == reducer.cell_root(cells)
    roots = {reducer.cell_root(reducer.cells_from_state({"value": value}))
             for value in [None, False, 0, 0.0, -0.0, [], {}]}
    assert len(roots) == 7


@pytest.mark.parametrize("cells", [
    {'["x","orphan"]': 1},
    {'[ "x" ]': ["map"]},
    {'["x"]': ["map"], '["x",0]': 1},
    {'["x"]': ["list", 1], '["x","0"]': 1},
    {'["x"]': ["list", True]},
    {'["x"]': ["list", 2], '["x",0]': 1},
    {'["x"]': ["list", 0], '["x",0]': 1},
    {'["x"]': ["scalar", {}]},
    {'["x"]': ["scalar", 1], '["x","extra"]': 2},
    {'["x"]': ["unknown"]},
])
def test_reconstruction_rejects_malformed_or_unclaimed_cells(cells):
    with pytest.raises(ValueError):
        reducer.state_from_cells(cells)


@pytest.mark.parametrize("legacy_count", [0, 1, 2, 3])
def test_v1_v2_mixed_replay_checks_each_observed_prefix(legacy_count):
    states = [{"epoch": 0, "events": [], "private": {"outbox": False}},
              {"epoch": 0, "events": [1], "private": {"outbox": True}, "unknown": -0.0},
              {"epoch": 1, "events": [], "replacement": {}}]
    export = journal(states, legacy_count)
    original = copy.deepcopy(export["entries"][:legacy_count])
    observations = {i: reducer.checksum(state) for i, state in enumerate(states, 1)}
    assert reducer.replay(export, observed_prefixes=observations) == states[-1]
    assert export["entries"][:legacy_count] == original
    for number, state in enumerate(states, 1):
        prefix = {**export, "entries": export["entries"][:number], "head_sequence": number,
                  "head_record_sha256": export["entries"][number - 1]["record_sha256"],
                  "state_root": reducer.cell_root(reducer.cells_from_state(state)),
                  "state_sha256": reducer.checksum(state)}
        assert reducer.checksum(reducer.replay(prefix)) == observations[number]
    legacy = journal(states, len(states), export_schema=reducer.V1)
    assert reducer.replay(legacy) == states[-1]


@pytest.mark.parametrize("damage", ["bridge", "after_root", "duplicate_change", "omit_field", "downgrade", "omit_record", "head_root"])
def test_rehashed_record_damage_is_rejected(damage):
    states = [{"private": {"outbox": False}}, {"private": {"outbox": True}, "unknown": 0.0}]
    bad = journal(states, legacy_count=1)
    if damage == "bridge":
        bad["entries"][1]["before_root"] = "0" * 64
    elif damage == "after_root":
        bad["entries"][1]["after_root"] = "0" * 64
    elif damage == "duplicate_change":
        bad["entries"][1]["changes"].append(copy.deepcopy(bad["entries"][1]["changes"][0]))
    elif damage == "omit_field":
        bad["entries"][1]["changes"] = [row for row in bad["entries"][1]["changes"] if row["key"] != '["unknown"]']
    elif damage == "downgrade":
        bad = journal([states[0], states[1], states[0]])
        bad["entries"][-1] = journal([states[0]], 1)["entries"][0]
        bad["entries"][-1].update(sequence=3, origin="write")
    elif damage == "omit_record":
        bad["entries"].pop(0)
    else:
        bad["state_root"] = "0" * 64
    resign(bad)
    with pytest.raises(ValueError):
        reducer.replay(bad)


def test_fully_rehashed_alternate_history_cannot_replace_independent_observation_or_head():
    actual = [{"outbox": {"dispatch": "pending"}}, {"outbox": {"dispatch": "delivered"}}]
    altered = [actual[0], {"outbox": {}, "pretend_success": True}]
    original, forged = journal(actual), journal(altered)
    observed = {i: reducer.checksum(value) for i, value in enumerate(actual, 1)}
    with pytest.raises(ValueError, match="independently observed"):
        reducer.replay(forged, observed_prefixes=observed)
    anchor = {key: original[key] for key in ("head_sequence", "head_record_sha256", "state_root")}
    with pytest.raises(ValueError, match="independently pinned"):
        reducer.replay(forged, expected_head=anchor)
    # Unkeyed hashes alone only establish internal consistency, never authorship.
    assert reducer.replay(forged) == altered[-1]


def test_sql_observation_uses_all_cells_and_ignores_legacy_copy_and_cached_root():
    state = {"epoch": 0, "events": [], "private_unknown": {"saved": -0.0}}
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE fabric_state(singleton INTEGER,payload TEXT)")
        connection.execute("INSERT INTO fabric_state VALUES(1,?)", (json.dumps({"stale": True}),))
        connection.execute("CREATE TABLE fabric_meta(root_hash TEXT)")
        connection.execute("INSERT INTO fabric_meta VALUES('forged-root')")
        connection.execute("CREATE TABLE fabric_units(key TEXT PRIMARY KEY,payload TEXT)")
        connection.executemany("INSERT INTO fabric_units VALUES(?,?)", [
            (key, reducer.canonical(value)) for key, value in reducer.cells_from_state(state).items()])
        assert reducer.checksum(reducer.read_sql_state(connection)) == reducer.checksum(state)
        connection.execute("DELETE FROM fabric_units WHERE key=?", ('["private_unknown"]',))
        with pytest.raises(ValueError, match="marker"):
            reducer.read_sql_state(connection)


def load_validator(name, relative):
    specification = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


fold = load_validator("independent_fold_control", "tests/acceptance/cp3/cp3_event_fold.py")
reviewer = load_validator("prefix_review_control", "tests/acceptance/cp4/_reliability_review.py")


def synthetic_prefix_records():
    """Small internally consistent records to attack the reviewer, not a real run."""
    inventory = [{"node_id": "n", "capacity": {"cpu_millicores": 4, "memory_mib": 8,
                  "ephemeral_storage_mib": 16}, "devices": [{"device_id": "g"}, {"device_id": "other"}]}]
    base = {"scenario_id": "cp3-prefix-attack-v1", "horizon": 25, "inventory": inventory,
            "works": [{"intent_id": "visible", "owner_id": "alice", "arrived_at": 0., "gpu_model": "A10"},
                      {"intent_id": "future", "owner_id": "bob", "arrived_at": 8., "gpu_model": "A10"}],
            "events": [{"at": 9, "notice_seconds": 2, "duration_seconds": 4}],
            "model": {"duration_seconds": 10}}
    inputs = {name: copy.deepcopy(base) for name in (
        "baseline", "changed-future-arrival", "deleted-future-arrival", "changed-future-recall",
        "deleted-future-recall", "changed-hidden-duration", "visible-control")}
    inputs["changed-future-arrival"]["works"][1].update(arrived_at=12., gpu_model="A100")
    inputs["deleted-future-arrival"]["works"] = inputs["deleted-future-arrival"]["works"][:1]
    inputs["changed-future-recall"]["events"][0].update(at=11, notice_seconds=1, duration_seconds=7)
    inputs["deleted-future-recall"]["events"] = []
    inputs["changed-hidden-duration"]["model"]["duration_seconds"] = 40
    inputs["visible-control"]["works"][0]["gpu_model"] = "A100"

    def observation(now, device):
        allocation = {"allocation_id": "a", "owner_id": "alice", "state": "Running",
                      "request": {}, "created_at": 0., "expires_at": 300., "preemption": None,
                      "checkpoint": None, "restart_count": 0, "pending_terminal": None,
                      "placement": {"slices": [{"node_id": "n", "device_ids": [device],
                          "resources": {"cpu_millicores": 1, "memory_mib": 2, "ephemeral_storage_mib": 3}}]}}
        event = {"event_id": "ev1", "sequence": 1, "version": {"epoch": 0, "revision": 1},
                 "logical_time": 0., "event_type": "allocation.granted", "state_changes": [],
                 "resource_deltas": [{"node_id": "n", "allocation_id": "a", "cpu_millicores": 1,
                     "memory_mib": 2, "ephemeral_storage_mib": 3,
                     "device_ids_added": [device], "device_ids_removed": []}]}
        state = {"epoch": 0, "revision": 1, "now": now, "allocations": {"a": allocation},
                 "jobs": {}, "intents": {}, "decisions": {}, "events": [event],
                 "unknown_private": {"callback": {"delivered": False}}}
        snapshot = {"version": {"epoch": 0, "revision": 1}, "now": now,
                    "allocations": [copy.deepcopy(allocation)], "jobs": [], "intents": [],
                    "decisions": [], "events": [copy.deepcopy(event)], "legal_actions": []}
        audit = {"journal": journal([state]), "observed_commit_prefixes": {"1": reducer.checksum(state)},
                 "final_persisted_state": state, "rebuilt_sha256": reducer.checksum(state)}
        return snapshot, audit

    records = {}
    for policy in ("B0", "B1", "B2", "P"):
        for name, value in inputs.items():
            device = "other" if name == "visible-control" else "g"
            snapshot, audit = observation(25., device)
            record = {"inputs": copy.deepcopy(value), "decisions": [],
                      "final_snapshot": snapshot, "state_replay": audit}
            if name == "visible-control":
                record["prefix"] = {"events": copy.deepcopy(snapshot["events"]), "decisions": []}
            else:
                cutoff, cutoff_audit = observation(5., device)
                record.update(cutoff_snapshot=cutoff, cutoff_state_replay=cutoff_audit,
                              fold=fold.fold_events(inventory, snapshot["events"]),
                              cutoff_fold=fold.fold_events(inventory, cutoff["events"]))
            records[f"{policy}-{name}"] = record
    return records


def test_prefix_reviewer_recomputes_complete_synthetic_control():
    reviewer.validate_prefix_records(synthetic_prefix_records(), reducer, fold)


@pytest.mark.parametrize("damage", [
    "future_prefix", "cutoff_projection", "visible_control", "visible_summary",
    "final_fold", "cutoff_fold", "public_object", "missing_sql_prefix", "wrong_attack",
])
def test_prefix_reviewer_rejects_semantic_damage_after_outer_artifacts_are_rehashed(damage):
    records = synthetic_prefix_records()
    record = records["B0-changed-future-arrival"]
    if damage == "future_prefix":
        # A deferred proposal is not in committed decisions. The old reviewer
        # accepted this alteration because all state replay evidence still agrees.
        record["decisions"].append({
            "snapshot": {"now": 2., "version": {"epoch": 0, "revision": 1}, "demands": [], "notices": []},
            "plan": {"policy": {"name": "B0"}, "snapshot_version": {"epoch": 0, "revision": 1},
                     "selected_candidate_id": None, "decision_id": "invented-visible-proposal"},
        })
    elif damage == "cutoff_projection":
        record["cutoff_snapshot"]["legal_actions"] = [{"unexpected": True}]
    elif damage == "visible_control":
        control = copy.deepcopy(records["B0-baseline"])
        control["inputs"] = records["B0-visible-control"]["inputs"]
        control["prefix"] = {"events": control["final_snapshot"]["events"], "decisions": []}
        records["B0-visible-control"] = control
    elif damage == "visible_summary":
        records["B0-visible-control"]["prefix"]["events"] = []
    elif damage == "final_fold":
        record["fold"]["claims"] = {}
    elif damage == "cutoff_fold":
        record["cutoff_fold"]["claims"] = {}
    elif damage == "public_object":
        record["final_snapshot"]["allocations"][0]["owner_id"] = "bob"
    elif damage == "missing_sql_prefix":
        record["state_replay"]["observed_commit_prefixes"] = {}
    else:
        record["inputs"]["works"][1]["arrived_at"] = 4.
    # This helper receives the parsed artifacts after outer reference checks;
    # those hashes cannot rescue contradictory retained facts.
    reencoded = json.loads(json.dumps(records))
    with pytest.raises((AssertionError, ValueError)):
        reviewer.validate_prefix_records(reencoded, reducer, fold)
