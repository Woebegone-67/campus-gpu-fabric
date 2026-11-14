"""Offline phase summary of the sole successfully started diagnostic attempt."""

import collections
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "tests/acceptance/cp4"))
from _load_runner import fold_ledger, verify_renewal  # noqa: E402


def quantiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {"count": len(ordered), "sum_ms": sum(ordered), "mean_ms": statistics.mean(ordered),
            **{f"p{int(p * 100)}_ms": ordered[math.ceil(p * len(ordered)) - 1]
               for p in (.5, .95, .99)}, "max_ms": ordered[-1]}


def main():
    run_dir = HERE / "run-b19acae-64-retry1"
    run = json.loads((run_dir / "run.json").read_text())
    events = json.loads((HERE / "service-events-retry1.json").read_text())["events"]
    with gzip.open(run_dir / "http_history.jsonl.gz", "rt") as handle:
        rows = [json.loads(line) for line in handle]
    with gzip.open(run_dir / "final_state.json.gz", "rt") as handle:
        state = json.load(handle)
    nodes = json.loads((run_dir / "inventory.json").read_text())["nodes"]
    expected = {"allocate": 64, "create_job": 64, "renew": 64,
                "cancel": 64, "release": 64, "job_detail": 8}
    counts = dict(collections.Counter(row["operation"] for row in rows))
    assert len(run["completed_flows"]) == 64 and not run["errors"]
    assert counts == expected and all(not row["error"] for row in rows)
    assert all(row["status"] == {"allocate": 201, "create_job": 201, "renew": 200,
                                "cancel": 202, "release": 202, "job_detail": 200}[row["operation"]]
               for row in rows)
    expiries = {row["response"]["id"]: row["response"]["expires_at"]
                for row in rows if row["operation"] == "allocate"}
    for row in rows:
        if row["operation"] == "renew":
            verify_renewal(expiries[row["response"]["id"]], row["response"]["expires_at"])
    ledger = fold_ledger(nodes, state)
    assert ledger["final_held_physical_gpus"] == 0
    assert all(item["state"] == "Released" for item in state["allocations"].values())
    assert all(item["state"] == "Cancelled" for item in state["jobs"].values())
    by_id = {row["request_id"]: row for row in rows}
    transactions = [event for event in events
                    if event["kind"] == "transaction" and event["request_id"] in by_id]
    assert len(transactions) == 320
    assert len({event["request_id"] for event in transactions}) == 320
    measurements = []
    for event in transactions:
        assert "error_type" not in event
        values = {"request_id": event["request_id"], "operation": by_id[event["request_id"]]["operation"],
                  "client_ms": by_id[event["request_id"]]["elapsed_ms"],
                  "connection_setup_ms": (event["begin_started_ns"] - event["started_ns"]) / 1e6,
                  "begin_acquire_ms": (event["begin_finished_ns"] - event["begin_started_ns"]) / 1e6,
                  "held_ms": (event["commit_finished_ns"] - event["begin_finished_ns"]) / 1e6,
                  "body_ms": (event["body_finished_ns"] - event["body_started_ns"]) / 1e6,
                  "commit_ms": (event["commit_finished_ns"] - event["commit_started_ns"]) / 1e6,
                  "cached_nodes": event["cached_entities"].get("nodes", 0),
                  "cached_allocations": event["cached_entities"].get("allocations", 0),
                  "inventory_calls": len(event["phases"].get("inventory", []))}
        for phase in ("inventory", "save", "changed_units"):
            values[f"{phase}_ms"] = sum(event["phases"].get(phase, [])) / 1e6
        values["other_body_ms"] = values["body_ms"] - values["inventory_ms"] - values["save_ms"]
        measurements.append(values)
    fields = [key for key in measurements[0] if key.endswith("_ms")]
    acquisition_order = sorted(transactions, key=lambda event: event["begin_finished_ns"])
    arrival_order = sorted(transactions, key=lambda event: event["begin_started_ns"])
    arrival_rank = {event["request_id"]: index for index, event in enumerate(arrival_order)}
    acquired_ranks = [arrival_rank[event["request_id"]] for event in acquisition_order]
    inversions = sum(a > b for index, a in enumerate(acquired_ranks) for b in acquired_ranks[index + 1:])
    longest = sorted(transactions,
                     key=lambda event: event["begin_finished_ns"] - event["begin_started_ns"],
                     reverse=True)[:10]
    overtaken = [{"request_id": event["request_id"], "operation": by_id[event["request_id"]]["operation"],
                  "begin_acquire_ms": (event["begin_finished_ns"] - event["begin_started_ns"]) / 1e6,
                  "later_arrivals_acquiring_first": sum(
                      other["begin_started_ns"] > event["begin_started_ns"]
                      and other["begin_finished_ns"] < event["begin_finished_ns"]
                      for other in transactions)} for event in longest]
    dispatches = [event for event in events
                  if event["kind"] == "thread_dispatch" and event["request_id"] in by_id]
    dispatch_summary = {
        "queue_ms": quantiles([(event["entered_ns"] - event["enqueued_ns"]) / 1e6 for event in dispatches]),
        "return_delivery_ms": quantiles([(event["returned_ns"] - event["function_finished_ns"]) / 1e6
                                         for event in dispatches]),
    }
    total_held = sum(value["held_ms"] for value in measurements)
    span_ms = (acquisition_order[-1]["commit_finished_ns"] - acquisition_order[0]["begin_finished_ns"]) / 1e6
    summary = {
        "kind": "BOUNDED_INSTRUMENTED_DIAGNOSTIC_NOT_ACCEPTANCE",
        "successful_attempt": "run-b19acae-64-retry1",
        "failed_start_preserved": "first-attempt-status.json",
        "elapsed_seconds": run["elapsed_seconds"], "counts": counts,
        "completed_flows": len(run["completed_flows"]), "ledger": ledger,
        "source_unchanged": run["build"] == run["build_at_finish"], "build": run["build"],
        "client": quantiles([row["elapsed_ms"] for row in rows]),
        "write_phases": {field: quantiles([value[field] for value in measurements]) for field in fields},
        "write_phases_by_operation": {operation: {
            field: quantiles([value[field] for value in measurements if value["operation"] == operation])
            for field in fields} for operation in expected if operation != "job_detail"},
        "thread_dispatch": dispatch_summary,
        "writer_span_ms": span_ms, "total_held_ms": total_held,
        "writer_busy_fraction_in_span": total_held / span_ms,
        "acquisition_rank_inversions": inversions, "longest_acquisitions": overtaken,
        "inventory_fraction_of_body": sum(value["inventory_ms"] for value in measurements)
                                      / sum(value["body_ms"] for value in measurements),
        "save_fraction_of_body": sum(value["save_ms"] for value in measurements)
                                 / sum(value["body_ms"] for value in measurements),
        "cached_nodes_range": [min(value["cached_nodes"] for value in measurements),
                               max(value["cached_nodes"] for value in measurements)],
        "cached_allocations_range": [min(value["cached_allocations"] for value in measurements),
                                     max(value["cached_allocations"] for value in measurements)],
        "inventory_calls_by_operation": {operation: sorted({value["inventory_calls"] for value in measurements
                                                            if value["operation"] == operation})
                                         for operation in expected if operation != "job_detail"},
        "phase_semantics": "BEGIN acquisition is actual execute wall time, not a subtraction from host latency; held includes body and COMMIT; changed_units is nested in save; aggregate waits overlap",
    }
    (HERE / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (HERE / "transactions.json").write_text(json.dumps(measurements, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in ("write_phases_by_operation",)}, indent=2))


if __name__ == "__main__":
    main()
