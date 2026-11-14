#!/usr/bin/env python3
"""CP3 independent model gate; preserve complete histories and exact source identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/reference"))

from check_checkpoint import current_build  # noqa: E402
from cp3_history import (  # noqa: E402
    boundary_scenarios,
    oracle_controls,
    run_concurrent,
    run_sequential,
)


def positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "evidence/cp3/independent_model_receipt.json")
    parser.add_argument("--seeds", type=positive_int, default=32)
    parser.add_argument("--steps", type=positive_int, default=100)
    parser.add_argument("--concurrent", type=positive_int, default=30)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence_dir = output.parent / (output.stem + "_histories")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    build = current_build(ROOT)
    receipt = {
        "schema_version": "1.0.0", "check": "CP3-INDEPENDENT-MODEL",
        "evidence_kind": "independent_history", "build": build, "status": "NOT_RUN",
        "observed_at": datetime.now(timezone.utc).isoformat(), "checks": [],
        "configuration": {"seeds": list(range(args.seeds)), "steps_per_seed": args.steps,
                          "concurrent_seeds": list(range(args.concurrent)),
                          "concurrent_operations_per_history": 6},
        "scope": {
            "oracle": "Standalone anonymous resource model with unit time ticks; no production placement, counters or event reducer reused",
            "fixture": "One homogeneous two-GPU node; CPU=4, RAM=8, disk=6, each A10 GPU VRAM=8",
            "executor": "Built-in scheduler and logical simulation, separate real SQLite connections in worker threads",
            "semantics": "Owner isolation, request replay/conflict, four-dimensional capacity, GPU model/VRAM, grant-based TTL, timeout/completion precedence, cancellation, release, node failures, restart persistence",
            "linearizability": "Exhaustive search over observed operations preserving invocation/response precedence and matching final normalized snapshot; source revision not used as oracle",
            "identity": "Creation replies bind opaque IDs only; all states, resource amounts and deadlines are calculated independently",
            "limits": "Bounded tested histories only; not a universal linearizability proof, multi-node/MIG proof, HTTP acceptance or CPU executor continuity validation",
            "counterexamples": "Unmodified failing operation history and traceback retained; not claimed minimized",
        },
    }

    def retain(name, data):
        path = evidence_dir / (name + ".json")
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        try:
            label = path.relative_to(ROOT).as_posix()
        except ValueError:
            label = str(path)
        return {"path": label, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    current_case = "oracle-controls"
    with tempfile.TemporaryDirectory(prefix="campus-cp3-model-") as temporary:
        databases = Path(temporary)
        try:
            controls = oracle_controls()
            receipt["checks"].append({"id": current_case, "status": "PASS", **controls,
                                      "evidence": retain(current_case, controls)})
            current_case = "deadline-and-resource-boundaries"
            boundaries = boundary_scenarios(databases / "boundaries.sqlite")
            receipt["checks"].append({"id": current_case, "status": "PASS",
                                      "commands": len(boundaries["history"]),
                                      "evidence": retain(current_case, boundaries)})
            command_counts, response_counts, sequential = Counter(), Counter(), []
            for seed in range(args.seeds):
                current_case = f"sequential-{seed}"
                result = run_sequential(databases / f"seq-{seed}.sqlite", seed, args.steps)
                for event in result["history"]:
                    command_counts[event["operation"]["kind"]] += 1
                    response_counts[event["result"].get("error", "success")] += 1
                sequential.append(retain(current_case, result))
            receipt["checks"].append({"id": "generated-sequential-histories", "status": "PASS",
                "histories": args.seeds, "commands": args.seeds * args.steps,
                "command_counts": dict(command_counts), "response_counts": dict(response_counts),
                "evidence": sequential})
            concurrent, nodes, edges, overlaps = [], 0, 0, 0
            for seed in range(args.concurrent):
                current_case = f"concurrent-{seed}"
                result = run_concurrent(databases / f"concurrent-{seed}.sqlite", seed)
                nodes += result["search"]["search_nodes"]
                edges += result["search"]["real_time_edges"]
                overlaps += result["search"]["overlapping_pairs"]
                concurrent.append(retain(current_case, result))
            assert overlaps > 0, "No observed overlapping calls; concurrency gate inconclusive"
            receipt["checks"].append({"id": "real-concurrent-linearizable-histories",
                "status": "PASS", "histories": args.concurrent, "commands": args.concurrent * 6,
                "search_nodes": nodes, "real_time_edges": edges,
                "overlapping_pairs": overlaps, "evidence": concurrent})
            receipt["status"] = "PASS"
        except Exception:
            receipt["status"] = "FAIL"
            failure = {"case": current_case, "traceback": traceback.format_exc()}
            retained = [retain("failure", failure)]
            for path in databases.glob("*.failure.json"):
                destination = evidence_dir / path.name
                shutil.copy2(path, destination)
                retained.append({"path": str(destination),
                                 "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
            receipt["checks"].append({"id": current_case, "status": "FAIL",
                                      "evidence": retained, **failure})
    receipt["build_at_finish"] = current_build(ROOT)
    if receipt["status"] == "PASS" and receipt["build_at_finish"] != build:
        receipt["status"] = "STALE"
        receipt["reason"] = "Source changed during execution; rerun against a stable tree"
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "checks": len(receipt["checks"]),
                      "receipt": str(output), "histories": str(evidence_dir)}))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
