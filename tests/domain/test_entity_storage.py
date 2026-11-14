"""Lossless schema-3 entities, migration, indexed selection and transactional views."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from campus_fabric.storage import journal
from campus_fabric.storage.sqlite import SQLiteStore
from campus_fabric.storage.state import flatten, materialize, unflatten, unit_key


def test_normalization_keeps_every_json_type_and_unknown_field():
    value = {"n": None, "bool": True, "int": 1, "float": 1.0, "negative_zero": -0.0,
             "zero": 0.0, "text": "同一个字段", "map": {}, "list": [],
             "unknown": {"x": {"foo": [None, True, 1, 1.0, -0.0]}},
             "events": [{"nested": {"arbitrary": [1, 2]}}, "a", None]}
    assert journal.encode(unflatten(flatten(value))) == journal.encode(value)
    assert flatten(value)[unit_key(("negative_zero",))] == '["scalar",-0.0]'


@pytest.mark.parametrize("units", [
    {'["x"]': '["list",2]', '["x",0]': 'null'},
    {'["x"]': '["list",1]', '["x",true]': 'null'},
    {'["x"]': '["map"]', '["x",0]': 'null'},
    {'["x"]': '["scalar",{}]'},
    {'["x",0]': 'null'},
    {'[ "x" ]': '["map"]'},
])
def test_invalid_normalized_cells_fail_closed(units):
    with pytest.raises(RuntimeError):
        unflatten(units)


def test_nested_inplace_updates_and_repeated_saves_are_not_lost(tmp_path):
    store = SQLiteStore(tmp_path / "nested.sqlite")
    store.initialize({"none": None, "jobs": {"a": {"steps": [1], "attrs": {"x": 1}}}, "events": []})
    with store.transaction() as conn:
        state = store.load(conn)
        state["jobs"]["a"]["steps"].append(2)
        state["jobs"]["a"]["attrs"].update(y=2)
        state["events"].append({"kind": "update"})
        store.save(conn, state)
        state["jobs"]["a"]["attrs"].pop("x")
        state["events"][0]["sequence"] = 1
        state["jobs"]["b"] = {"steps": [3]}
        store.save(conn, state)
    assert store.read() == {"none": None, "jobs": {"a": {"steps": [1, 2], "attrs": {"y": 2}},
                                                  "b": {"steps": [3]}},
                            "events": [{"kind": "update", "sequence": 1}]}
    assert SQLiteStore(store.path).read() == store.read()


def test_ordinary_dict_save_replacements_deletions_and_reset(tmp_path):
    store = SQLiteStore(tmp_path / "replace.sqlite")
    store.initialize({"map": {"a": 1}, "list": [1, 2, 3], "scalar": None})
    with store.transaction() as conn:
        state = store.load(conn)
        del state["map"]
        state["list"][1:] = ["new"]
        state["scalar"] = {"nested": [1]}
        state["added"] = None
        store.save(conn, state)
    assert store.read() == {"list": [1, "new"], "scalar": {"nested": [1]}, "added": None}
    with store.transaction() as conn:
        store.save(conn, {"list": {}, "scalar": -0.0})
    assert journal.encode(store.read()) == '{"list":{},"scalar":-0.0}'
    with store.transaction() as conn:
        state = store.load(conn)
        state.clear()
        state.update(epoch=2, events=[], jobs={})
        store.save(conn, state)
    assert store.read() == {"epoch": 2, "events": [], "jobs": {}}


@pytest.mark.parametrize("value,replacement", [({"original": {"count": 1}}, {"other": 2}),
                                                ([{"count": 1}], ["other"])])
def test_root_move_keeps_references_without_following_recreated_source(tmp_path, value, replacement):
    store = SQLiteStore(tmp_path / "move.sqlite")
    store.initialize({"source": value})
    with store.transaction() as conn:
        state = store.load(conn)
        retained = state.pop("source")
        state.update(destination=retained)
        store.save(conn, state)
        state["source"] = replacement
        store.save(conn, state)
        if isinstance(value, dict):
            retained["original"]["count"] = 3
            retained["added"] = 4
            expected = {"original": {"count": 3}, "added": 4}
        else:
            retained[0]["count"] = 3
            retained.append(4)
            expected = [{"count": 3}, 4]
        store.save(conn, state)
        assert materialize(state["destination"]) == expected
    assert store.read() == {"source": replacement, "destination": expected}
    assert SQLiteStore(store.path).read() == store.read()


@pytest.mark.parametrize("value", [{"one": 1}, [1]])
def test_aliased_root_values_keep_both_names_and_later_local_mutations(tmp_path, value):
    store = SQLiteStore(tmp_path / "alias.sqlite")
    store.initialize({"source": value})
    with store.transaction() as conn:
        state = store.load(conn)
        retained = state["source"]
        state.setdefault("alias", retained)
        store.save(conn, state)
        if isinstance(value, dict):
            retained["two"] = 2
            expected = {"one": 1, "two": 2}
        else:
            retained.append(2)
            expected = [1, 2]
        store.save(conn, state)
        assert state["alias"] is state["source"] is retained
    assert store.read() == {"source": expected, "alias": expected}


@pytest.mark.parametrize("value", [{"a": {"n": 1}}, [{"n": 1}]])
@pytest.mark.parametrize("preload", [False, True])
@pytest.mark.parametrize("remove", [False, True])
def test_retained_root_survives_replacement_or_deletion_then_restore(tmp_path, value, preload, remove):
    store = SQLiteStore(tmp_path / "restore.sqlite")
    store.initialize({"source": value})
    with store.transaction() as conn:
        state = store.load(conn)
        retained = state["source"]
        if preload:
            assert materialize(retained) == value
        if remove:
            del state["source"]
        else:
            state["source"] = 0
        store.save(conn, state)
        assert materialize(retained) == value
        state["source"] = retained
        store.save(conn, state)
        retained["a" if isinstance(value, dict) else 0]["n"] = 2
        store.save(conn, state)
    expected = {"a": {"n": 2}} if isinstance(value, dict) else [{"n": 2}]
    assert store.read() == {"source": expected}
    assert SQLiteStore(store.path).read() == store.read()


def test_append_and_single_entity_write_do_not_load_completed_history(tmp_path):
    store = SQLiteStore(tmp_path / "lazy.sqlite")
    store.initialize({"jobs": {str(i): {"state": "Completed", "value": i} for i in range(256)},
                      "events": [{"number": i} for i in range(256)], "now": 0})
    with store.transaction() as conn:
        state = store.load(conn)
        assert state["jobs"]._cache == state["events"]._cache == {}
        assert len(state["events"]) == 256
        state["jobs"]["1"]["value"] = 1000
        state["events"].append({"number": 256})
        state["now"] = 1
        store.save(conn, state)
        assert len(state["jobs"]._cache) == len(state["events"]._cache) == 1
    edits = store.export_journal()["entries"][-1]["changes"]
    assert {edit["key"] for edit in edits} == {
        '["jobs","1"]', '["events",256]', '["events"]', '["now"]'}


def test_filtered_maps_merge_current_inplace_changes_insertions_and_deletions(tmp_path):
    store = SQLiteStore(tmp_path / "filters.sqlite")
    store.initialize({"jobs": {
        "a": {"state": "Running", "owner_id": "alice"},
        "b": {"state": "Completed", "owner_id": "alice"},
        "c": {"state": "Running", "owner_id": "bob"},
        "d": {"state": "Running", "owner_id": "alice"}},
        "outbox": {"cmd": {"command": {"action": "start", "attempt": {"attempt_id": "j1-a1"}},
                           "status": "pending"}},
        "recalls": {"past": {"unavailable_until": 9.0}, "future": {"unavailable_until": 11.0}}})
    with store.transaction() as conn:
        state = store.load(conn)
        jobs = state["jobs"]
        jobs["a"]["state"] = "Completed"
        jobs["b"]["state"] = "Running"
        del jobs["d"]
        jobs["e"] = {"state": "Running", "owner_id": "alice"}
        assert list(jobs.values_where(state="Running", owner_id="alice")) == [jobs["b"], jobs["e"]]
        assert len(list(jobs.values_where(state=("Running", "Completed")))) == 4
        assert len(jobs.touched_values()) == 4
        assert len(list(state["outbox"].values_where(attempt_id="j1-a1", action="start"))) == 1
        assert list(state["recalls"].values_where(unavailable_until__gt=10)) == [{"unavailable_until": 11.0}]
        with pytest.raises(ValueError):
            list(jobs.values_where(arbitrary="x"))
        store.save(conn, state)
    assert len(store.read()["jobs"]) == 4
    with store.snapshot() as state:
        assert len(list(state["jobs"].values_where(state="Running", owner_id="alice"))) == 2


def test_filtered_type_exactness_and_signed_zero(tmp_path):
    store = SQLiteStore(tmp_path / "types.sqlite")
    store.initialize({"items": {"a": {"state": True}, "b": {"state": 1},
                                "c": {"state": 1.0}, "d": {"state": -0.0}, "e": {"state": 0.0}}})
    with store.snapshot() as state:
        for expected in (True, 1, 1.0, -0.0, 0.0):
            values = list(state["items"].values_where(state=expected))
            assert len(values) == 1
            assert journal.equal(values[0]["state"], expected)


def test_attempt_and_allocation_selection_vm_work_stays_bounded_with_history(tmp_path):
    """Synthetic complexity probe, not a production throughput or CP4 result."""
    measurements = []
    for count in (0, 256, 4096):
        store = SQLiteStore(tmp_path / f"selection-{count}.sqlite")
        state = {"outbox": {}, "jobs": {}}
        for index in range(count):
            state["outbox"][f"old-{index}"] = {
                "command": {"action": "stop", "attempt": {"attempt_id": f"old-{index}"}},
                "status": "delivered",
            }
            state["jobs"][f"old-{index}"] = {
                "state": "Succeeded", "allocation_id": f"old-lease-{index}",
            }
        state["outbox"]["current"] = {
            "command": {"action": "stop", "attempt": {"attempt_id": "current-attempt"}},
            "status": "pending",
        }
        state["jobs"]["current"] = {"state": "Running", "allocation_id": "current-lease"}
        store.initialize(state)
        with store.snapshot() as lazy:
            steps = 0

            def record_step():
                nonlocal steps
                steps += 1
                return 0

            lazy.connection.set_progress_handler(record_step, 1)
            try:
                assert len(list(lazy["outbox"].values_where(
                    attempt_id="current-attempt", action="stop"))) == 1
                assert len(list(lazy["jobs"].values_where(
                    allocation_id="current-lease", state=("Pending", "Running", "Paused")))) == 1
            finally:
                lazy.connection.set_progress_handler(None, 0)
            measurements.append(steps)
            assert len(lazy["outbox"]._cache) == len(lazy["jobs"]._cache) == 1
    assert max(measurements) <= measurements[0] + 32, measurements


def test_read_snapshot_keeps_old_entities_while_another_connection_commits(tmp_path):
    store = SQLiteStore(tmp_path / "snapshot.sqlite")
    store.initialize({"jobs": {"a": {"state": "Running"}}, "events": []})
    with store.snapshot() as snapshot:
        assert snapshot["jobs"]._cache == {}
        with store.transaction() as conn:
            state = store.load(conn)
            state["jobs"]["a"]["state"] = "Completed"
            state["events"].append("completed")
            store.save(conn, state)
        assert materialize(snapshot) == {"jobs": {"a": {"state": "Running"}}, "events": []}
    assert store.read()["jobs"]["a"]["state"] == "Completed"


def legacy_database(path, version, states):
    payloads, previous, before = [], None, None
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fabric_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)")
        conn.execute("INSERT INTO fabric_state VALUES(1,?)", (journal.encode(states[-1]),))
        if version == 2:
            conn.execute("CREATE TABLE fabric_journal (sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
            for sequence, state in enumerate(states, 1):
                record = {"schema": journal.SCHEMA, "sequence": sequence,
                          "origin": "initialize" if sequence == 1 else "write",
                          "previous_record_sha256": previous,
                          "before_sha256": None if before is None else journal.digest(before),
                          "after_sha256": journal.digest(state), "changes": journal.changes(before, state)}
                record["record_sha256"] = journal.digest(record)
                # Deliberately noncanonical record formatting must survive migration byte-for-byte.
                payload = json.dumps(record, indent=3)
                conn.execute("INSERT INTO fabric_journal VALUES(?,?)", (sequence, payload))
                payloads.append(payload)
                previous, before = record["record_sha256"], state
        conn.execute(f"PRAGMA user_version={version}")
    return payloads


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_migration_preserves_records_snapshot_and_then_appends_v2(tmp_path, version):
    path = tmp_path / f"legacy-{version}.sqlite"
    state = {"jobs": {"a": {"state": "Completed"}}, "events": [1, 2], "float": -0.0}
    payloads = legacy_database(path, version, [{"jobs": {}, "events": [1]}, state])
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: SQLiteStore(path), range(4)))
    store = stores[0]
    assert journal.encode(store.read()) == journal.encode(state)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT payload FROM fabric_state").fetchone()[0] == journal.encode(state)
        persisted = [row[0] for row in conn.execute("SELECT payload FROM fabric_journal ORDER BY sequence")]
    if version == 2:
        assert persisted == payloads
    else:
        assert json.loads(persisted[0])["origin"] == "migration-baseline"
    initial_root = store.export_journal()["state_root"]
    with store.transaction() as conn:
        current = store.load(conn)
        current["events"].append(3)
        store.save(conn, current)
    export = store.export_journal()
    assert export["entries"][-1]["schema"] == "fabric-committed-state-v2"
    assert export["entries"][-1]["before_root"] == initial_root
    assert SQLiteStore(path).read()["events"] == [1, 2, 3]
    with sqlite3.connect(path) as conn:
        assert [row[0] for row in conn.execute("SELECT payload FROM fabric_journal ORDER BY sequence")][:-1] == persisted


def test_migration_rejects_broken_legacy_middle_and_rolls_back(tmp_path):
    path = tmp_path / "bad-legacy.sqlite"
    legacy_database(path, 2, [{"counter": 0}, {"counter": 1}, {"counter": 2}])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE fabric_journal SET payload=replace(payload, 'initialize', 'invalid') WHERE sequence=1")
    with pytest.raises(RuntimeError, match="journal"):
        SQLiteStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='fabric_units'").fetchone() is None


@pytest.mark.parametrize("tamper", ["cell", "index", "root", "journal_prefix"])
def test_reopen_rejects_cell_index_root_or_journal_tampering(tmp_path, tamper):
    path = tmp_path / "tamper.sqlite"
    store = SQLiteStore(path)
    store.initialize({"jobs": {"a": {"state": "Running"}}, "counter": 0})
    with store.transaction() as conn:
        state = store.load(conn)
        state["counter"] = 1
        store.save(conn, state)
    with sqlite3.connect(path) as conn:
        if tamper == "cell":
            conn.execute("UPDATE fabric_units SET payload='[\"scalar\",999]' WHERE key='[\"counter\"]'")
        elif tamper == "index":
            conn.execute("UPDATE fabric_units SET state='\"Completed\"' WHERE key='[\"jobs\",\"a\"]'")
        elif tamper == "root":
            conn.execute("UPDATE fabric_meta SET root_hash='bad'")
        else:
            conn.execute("UPDATE fabric_journal SET payload=replace(payload, 'initialize', 'invalid') WHERE sequence=1")
    with pytest.raises(RuntimeError):
        SQLiteStore(path)
