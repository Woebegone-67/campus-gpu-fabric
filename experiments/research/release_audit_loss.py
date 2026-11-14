"""Read-only recomputation of loss-study receipts; never execute a workload."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.research.release_loss_accounting import ROOT, digest, oracle, write


def audit(directory: Path):
    hashes = json.loads((directory / "manifest-sha256.json").read_text())
    assert all(digest(directory / name) == expected for name, expected in hashes.items())
    manifest = json.loads((directory / "manifest.json").read_text())
    assert all(digest(ROOT / name) == expected for name, expected in manifest["source_hashes"].items())
    report = {"recorded_files_match": True, "recorded_sources_match": True,
              "runs": [], "all_denominators": {}}
    for name, _, _ in manifest["cases"]:
        for arm in ("continuation", "cold", "finish_fits"):
            row = json.loads((directory / f"{name}-{arm}.json").read_text())
            assert not row["errors"], row["errors"]
            computed = 0
            for attempt in row["measurements"]:
                position = attempt["initial_step"]
                for batch in attempt["compute"]:
                    assert batch["start_step"] == position
                    computed += batch["end_step"] - batch["start_step"]
                    position = batch["end_step"]
                assert position == attempt["final_step"]
                started = next(w for w in attempt["writes"] if w["filename"] == "started.json")
                assert started["step"] == attempt["initial_step"]
                assert attempt["stop_confirmed"]
            assert computed == row["metrics"]["executed_steps"]
            for evidence in row["artifacts"]:
                state = evidence["payload"]["state"]
                assert state == oracle(manifest["config"]["seed"], state["step"])
            if row["status"] == "completed":
                assert row["metrics"]["redundant_steps_if_completed"] == (
                    computed - manifest["config"]["total_steps"])
                assert row["artifacts"][-1]["payload"]["state"]["step"] == manifest["config"]["total_steps"]
            else:
                assert row["metrics"]["truncated_completion_cost_seconds"] == manifest["horizon"]
            if row.get("corruption_probe"):
                assert row["corruption_probe"]["rejected"]
                assert row["corruption_probe"]["new_attempts"] == 0
            reserved_ns = 0
            for dispatch in row["dispatches"]:
                if dispatch["action"] not in {"start", "resume"}:
                    continue
                terminal = [e["received_ns"] for e in row["events"]
                            if e["event"]["attempt"]["generation"] == dispatch["generation"]
                            and e["event"]["kind"] in {"completed", "failed", "stopped"}]
                assert terminal, "No observed terminal confirmation"
                reserved_ns += min(terminal) - dispatch["start_ns"]
            report["runs"].append({
                "case": name, "arm": arm, "status": row["status"],
                "independent_artifact_checks": len(row["artifacts"]),
                "contiguous_steps_and_initial_sources_verified": True,
                "executor_slot_dispatch_to_confirmed_exit_seconds": reserved_ns / 1e9,
                "slot_scope": "conservative reference-slot reservation; not measured GPU utilization",
                "notice_to_stop_confirmed_seconds": row["metrics"]["notice_to_stop_confirmed_seconds"],
                "baseline_forecast": row["baseline_opportunity"]["action"],
                "actual_baseline_branch": row.get("actual_branch"),
            })
    raw_summary = json.loads((directory / "summary.json").read_text())
    for arm in ("continuation", "cold", "finish_fits"):
        rows = [json.loads((directory / f"{name}-{arm}.json").read_text())
                for name, _, _ in manifest["cases"]]
        actual = {"completed": sum(x["status"] == "completed" for x in rows),
                  "failed": sum(x["status"] == "failed" for x in rows),
                  "unfinished": sum(x["status"] == "unfinished" for x in rows),
                  "F_seconds": sum(x["metrics"]["truncated_completion_cost_seconds"] for x in rows)}
        assert actual == raw_summary["arms"][arm]
        report["all_denominators"][arm] = actual
    report["status"] = "PASS"
    report["scope_corrections"] = [
        "finish_fits executes fixed-boundary closeout regardless of the diagnostic estimate; not predictive optimization.",
        "The forecast's raw scope text says finish was not executed. That refers only to the diagnostic forecast, not the separately executed third arm.",
        "worker_occupied_wall_seconds excludes interpreter/import startup and final measurement write/exit; use slot intervals for reservation.",
        "No notice overrun was observed in these 12 arms; 100ms is not a proved worst-case bound.",
        "Immediate post-release authorization isolates computation; real contributor self-use and resource wait are not modeled in the 12 arms.",
        "The third-arm fault case avoids the save operation; it did not survive an actual checkpoint write failure.",
    ]
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refuse to overwrite an audit")
    result = audit(args.directory)
    write(args.output, result)
    print(json.dumps({"status": result["status"], "runs": len(result["runs"]),
                      "denominators": result["all_denominators"]}))
