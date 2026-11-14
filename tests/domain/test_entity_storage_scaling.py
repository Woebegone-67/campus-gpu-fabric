"""Synthetic terminal history probes storage work, not completed HTTP workload."""

import pytest

from campus_fabric.storage.sqlite import SQLiteStore


@pytest.mark.parametrize("history", [0, 256, 4096, 10000])
def test_one_live_entity_does_not_scan_terminal_history(tmp_path, history):
    store = SQLiteStore(tmp_path / "scaling.sqlite3")
    jobs = {f"old-{number:05}": {"state": "Succeeded", "owner_id": "alice", "progress": 10}
            for number in range(history)}
    jobs["live"] = {"state": "Running", "owner_id": "alice", "progress": 0}
    store.initialize({"revision": 0, "jobs": jobs, "events": []})
    # Initialization has a deliberately full baseline journal record. Replace
    # only its head with one real durable metadata update, retaining that record.
    with store.transaction() as conn:
        state = store.load(conn)
        state["revision"] += 1
        store.save(conn, state)
    with store.transaction() as conn:
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        conn.set_progress_handler(progress, 1)
        state = store.load(conn)
        active = list(state["jobs"].values_where(state="Running"))
        assert len(active) == 1
        active[0]["progress"] += 1
        state["revision"] += 1
        state["events"].append({"kind": "progress", "job_id": "live"})
        store.save(conn, state)
        conn.set_progress_handler(None, 0)
        assert len(state["jobs"]._cache) == 1
        assert steps < 12000, (history, steps, store.last_write_stats)
        assert store.last_write_stats["changed_units"] == 4
    # Opening a fresh connection verifies the committed outcome, retaining every
    # terminal record; this explicit audit is outside the measured transaction.
    result = store.read()
    assert len(result["jobs"]) == history + 1
    assert result["jobs"]["live"]["progress"] == 1
    assert all(row["progress"] == 10 for key, row in result["jobs"].items() if key != "live")
