"""Read-only reproduction and candidate-set headroom audit of sealed v3 results."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

from experiments.research.reclaim_comparison import ROOT, make_report, sha256


def diagnose() -> dict:
    folder = ROOT / "evidence/reclaim-aware-20261115"
    protocol_path = folder / "protocol.json"
    protocol = json.loads(protocol_path.read_text())
    sealed = json.loads((folder / "report.json").read_text())
    reproduced = make_report(protocol, protocol_path)
    if reproduced["runs"] != sealed["runs"]:
        raise RuntimeError("Reproduction differs from the sealed runs; investigate first")
    rows = reproduced["runs"]
    results = []
    for preference in protocol["preferences"]:
        subset = [r for r in rows if r["preference"] == preference]
        baseline = [r for r in subset if r["policy"] == "finish_or_save"]
        adaptive = [r for r in subset if r["policy"] == "adaptive"]
        oracle = {case["case"]: min(r["cost"] for r in subset if r["case"] == case["case"])
                  for case in protocol["cases"]}
        base_cost, cost = mean(r["cost"] for r in baseline), mean(r["cost"] for r in adaptive)
        oracle_cost = mean(oracle.values())
        results.append({
            "preference": preference,
            "baseline_cost": base_cost, "adaptive_cost": cost,
            "adaptive_reduction_percent": 100 * (base_cost - cost) / base_cost,
            "candidate_set_hindsight_oracle_cost": oracle_cost,
            "candidate_set_headroom_percent": 100 * (base_cost - oracle_cost) / base_cost,
            "adaptive_distance_to_oracle_percent": 100 * (cost - oracle_cost) / cost,
            "baseline_mean_completion_seconds": mean(r["truncated_completion_seconds"] for r in baseline),
            "adaptive_mean_completion_seconds": mean(r["truncated_completion_seconds"] for r in adaptive),
            "adaptive_deadline_misses": sum(r["deadline_misses"] for r in adaptive),
            "oracle_cost_by_case": oracle,
        })
    return {
        "sealed_runs_reproduced_exactly": True,
        "protocol_sha256": sha256(protocol_path),
        "sealed_report_sha256": sha256(folder / "report.json"),
        "diagnostic_source_sha256": sha256(Path(__file__)),
        "results": results,
        "limitations": [
            "Oracle chooses among five existing arms after observing outcomes; not a deployable policy.",
            "Candidate-set headroom is not an upper bound over all possible mechanisms.",
            "Fourteen fixed conditions are not random draws of campus workloads.",
            "Weighted cost improvement does not mean completion time or throughput improvement.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result["results"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
