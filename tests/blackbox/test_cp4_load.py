"""Reject overclaims and exercise the diagnostic's actual TCP workload."""

import copy
import hashlib
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiments.cp4_load import (
    ROOT,
    LoadHarness,
    check_state,
    make_inventory,
    quantile,
    run_load,
    write_ledger_snapshot,
)


def state_with_claims():
    node = make_inventory(1, 2)["nodes"][0]
    parent = node["devices"][0]["device_id"]
    return {"epoch": 0, "revision": 1, "nodes": {node["node_id"]: node}, "jobs": {},
            "events": [], "allocations": {"a1": {"state": "Allocated", "placement": {
                "slices": [{"node_id": node["node_id"], "device_ids": [parent],
                            "resources": {"cpu_millicores": 1000, "memory_mib": 1024,
                                          "ephemeral_storage_mib": 32}}]}}}}


def test_physical_parent_count_and_duplicate_claim_detection():
    state = state_with_claims()
    observed = check_state(state)
    assert observed["physical_gpus"] == 2
    assert observed["device_representations"] == 6
    state["allocations"]["a2"] = copy.deepcopy(state["allocations"]["a1"])
    with pytest.raises(ValueError, match="Duplicate"):
        check_state(state)


def test_whole_slice_overlap_and_ghost_placement_rejected():
    state = state_with_claims()
    second = copy.deepcopy(state["allocations"]["a1"])
    second["placement"]["slices"][0]["device_ids"][0] += "-slice-0"
    state["allocations"]["a2"] = second
    with pytest.raises(ValueError, match="overlaps"):
        check_state(state)
    del state["allocations"]["a2"]
    state["allocations"]["a1"]["state"] = "Released"
    with pytest.raises(ValueError, match="retains placement"):
        check_state(state)


@pytest.mark.parametrize("key", ["cpu_millicores", "memory_mib", "ephemeral_storage_mib"])
@pytest.mark.parametrize("value", [-1, True, False, 1.5, "1"])
def test_individual_scalar_claims_require_nonnegative_integers(key, value):
    state = state_with_claims()
    state["allocations"]["a1"]["placement"]["slices"][0]["resources"][key] = value
    with pytest.raises(ValueError, match=f"Invalid scalar claim.*{key}"):
        check_state(state)


@pytest.mark.parametrize("key", ["cpu_millicores", "memory_mib"])
def test_negative_slice_cannot_cancel_positive_overcommit(key):
    state = state_with_claims()
    node = next(iter(state["nodes"].values()))
    slices = state["allocations"]["a1"]["placement"]["slices"]
    second = copy.deepcopy(slices[0])
    second["device_ids"] = [node["devices"][3]["device_id"]]
    slices[0]["resources"][key] = node["capacity"][key] + 1000
    second["resources"][key] = -1000
    slices.append(second)
    # The aggregate equals capacity, so an aggregate-only nonnegative check misses this.
    assert sum(part["resources"][key] for part in slices) == node["capacity"][key]
    with pytest.raises(ValueError, match=f"Invalid scalar claim.*{key}"):
        check_state(state)


def test_harness_pins_subprocess_source_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/old-checkout/src:/unrelated/scripts")
    harness = LoadHarness(tmp_path, {"nodes": 1, "gpus_per_node": 1, "owners": 1},
                          io.StringIO(), request_timeout=1)
    assert harness.env["PYTHONPATH"].split(os.pathsep) == [
        str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")]


def test_http_peaks_measure_overlapping_calls_and_separate_commands(tmp_path):
    harness = LoadHarness(tmp_path, {"nodes": 1, "gpus_per_node": 1, "owners": 1},
                          io.StringIO(), request_timeout=1)
    entered = threading.Barrier(4)
    release = threading.Event()

    class Response:
        status_code = 200
        content = b"{}"

        def json(self):
            return {}

    class BlockingClient:
        def request(self, *args, **kwargs):
            entered.wait(timeout=5)
            assert release.wait(timeout=5)
            return Response()

    harness.client = BlockingClient()
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = [pool.submit(harness.measured, "GET", "/test", operation="test",
                              category=category, actor="admin", expected=(200,))
                   for category in ("command", "query", "command")]
        try:
            entered.wait(timeout=5)
            assert harness.active_http_requests == harness.peak_http_requests == 3
            assert harness.active_http_commands == harness.peak_http_commands == 2
            assert harness.peak_active_sequences == 0
        finally:
            release.set()
        for future in pending:
            future.result(timeout=5)
    assert harness.active_http_requests == harness.active_http_commands == 0
    assert max(row["http_requests_in_flight_at_start"] for row in harness.samples) == 3
    assert max(row["http_commands_in_flight_at_start"] for row in harness.samples) == 2
    assert all(row["http_finished_monotonic"] >= row["started_monotonic"]
               for row in harness.samples)


def test_transport_error_releases_inflight_counters_and_retains_failed_sample(tmp_path):
    harness = LoadHarness(tmp_path, {"nodes": 1, "gpus_per_node": 1, "owners": 1},
                          io.StringIO(), request_timeout=1)

    class FailedClient:
        def request(self, *args, **kwargs):
            raise TimeoutError("bounded failure")

    harness.client = FailedClient()
    with pytest.raises(RuntimeError, match="bounded failure"):
        harness.measured("POST", "/test", operation="test", category="command",
                         actor="admin", expected=(200,))
    assert harness.active_http_requests == harness.active_http_commands == 0
    assert harness.peak_http_requests == harness.peak_http_commands == 1
    assert len(harness.samples) == 1 and not harness.samples[0]["ok"]


def test_raw_ledger_snapshot_refuses_credentials(tmp_path):
    harness = LoadHarness(tmp_path, {"nodes": 1, "gpus_per_node": 1, "owners": 1},
                          io.StringIO(), request_timeout=1)
    state = state_with_claims()
    state["accidental_secret"] = harness.tokens["admin"]
    with pytest.raises(RuntimeError, match="containing credentials"):
        write_ledger_snapshot(tmp_path, "failure", state, harness)
    assert not (tmp_path / "ledger-failure.json").exists()


def test_nearest_rank_percentile_keeps_empty_groups_unknown():
    assert quantile([], .95) is None
    assert quantile(list(range(1, 101)), .95) == 95


def test_real_http_smoke_counts_mutations_and_preserves_reference_gate(tmp_path):
    output = tmp_path / "load"
    result = run_load(output, profile_name="smoke", max_wall_seconds=60)
    assert result["status"] == "PASS", result["failure"]
    assert result["actual"]["jobs_created"] == 8
    assert result["actual"]["successful_commands"] == 40
    assert result["actual"]["completed_lifecycles"] == 8
    assert result["actual"]["effective_renewals"] == 8
    assert result["workload"]["version"] == "effective-renewal-v2"
    assert result["actual"]["final_snapshot"]["allocation_states"] == {"Released": 8}
    assert result["actual"]["final_snapshot"]["job_states"] == {"Cancelled": 8}
    assert result["cp4_reference_latency"] == "NOT_RUN"
    assert result["cp4_scale"] == "NOT_MET"
    assert 1 <= result["actual"]["peak_http_commands_in_flight"] <= 4
    assert result["cp4_concurrency"]["required_command_http_peak"] == 32
    assert result["cp4_concurrency"]["status"] == "NOT_MET"
    records = [json.loads(line) for line in (output / "requests.jsonl").read_text().splitlines()]
    assert len([r for r in records if r["category"] == "query"]) == 16
    assert not any("Authorization" in json.dumps(row) for row in records)
    assert set(result["ledger_snapshots"]) == {"initial", "final"}
    for label, metadata in result["ledger_snapshots"].items():
        content = (output / metadata["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
        observed = check_state(json.loads(content))
        assert observed["state_sha256"] == metadata["state_sha256"]
        assert observed["jobs"] == (0 if label == "initial" else 8)


def test_deadline_retains_partial_result_and_cannot_pass_full_scale(tmp_path):
    result = run_load(tmp_path / "stopped", profile_name="smoke", max_wall_seconds=0.000001)
    assert result["status"] == "FAIL"
    assert result["actual"]["completed_lifecycles"] == 0
    assert result["cp4_scale"] == "NOT_MET"
    assert result["latency"].get("command") is None
    assert set(result["ledger_snapshots"]) == {"initial", "final", "failure"}
    assert json.loads((tmp_path / "stopped/ledger-failure.json").read_text())["jobs"] == {}


def test_early_failure_stop_is_not_reported_as_elapsed_deadline(tmp_path, monkeypatch):
    harness = LoadHarness(tmp_path, {"nodes": 1, "gpus_per_node": 1, "owners": 1},
                          io.StringIO(), request_timeout=1)
    monkeypatch.setattr("experiments.cp4_load.time.monotonic", lambda: 100.0)
    harness.deadline = 200.0
    harness.stop_load.set()
    with pytest.raises(RuntimeError, match="earlier failure or stop signal"):
        harness.measured("GET", "/test", operation="test", category="query",
                         actor="admin", expected=(200,))
    harness.deadline = 99.0
    with pytest.raises(TimeoutError, match="wall-time budget exhausted"):
        harness.measured("GET", "/test", operation="test", category="query",
                         actor="admin", expected=(200,))
    assert harness.samples == []


def test_existing_output_is_never_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        run_load(tmp_path)


@pytest.mark.parametrize("renew_expiry,final_expiry,error", [
    ("2026-01-01T00:05:00Z", "2026-01-01T00:10:00Z", "Renewal did not extend"),
    ("2026-01-01T00:10:00Z", "2026-01-01T00:05:00Z", "renewed deadline"),
])
def test_noop_or_unpersisted_renewal_cannot_count_as_completed(tmp_path, monkeypatch,
                                                            renew_expiry, final_expiry, error):
    harness = LoadHarness(tmp_path, {"nodes": 1, "gpus_per_node": 1, "owners": 1},
                          io.StringIO(), request_timeout=1)

    def measured(method, path, **kwargs):
        if kwargs["operation"] == "allocation.create":
            return {"id": "a1", "owner": "owner-000", "expires_at": "2026-01-01T00:05:00Z"}
        if kwargs["operation"] == "allocation.renew":
            assert kwargs["body"]["ttl_seconds"] == 600
            return {"id": "a1", "owner": "owner-000", "expires_at": renew_expiry}
        if kwargs["operation"] == "allocation.get":
            return {"id": "a1", "owner": "owner-000", "state": "Released", "expires_at": final_expiry}
        return {"id": "j1", "owner": "owner-000", "allocation_id": "a1"}

    monkeypatch.setattr(harness, "measured", measured)
    harness.sequence(0)
    assert harness.completed_sequences == 0
    assert harness.stop_load.is_set()
    assert error in harness.sequence_failures[0]["error"]
