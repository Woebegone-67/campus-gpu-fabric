"""Post-outcome diagnosis of a sealed neural study; no fitting or policy changes.

The generator-distribution oracle is privileged diagnostic information. It is
never passed to the deployed forecaster. Its expected cost still inherits the
frozen renewal approximation; a separate hindsight oracle uses actual calendars.
Neither is an implementable baseline or a claim of statistical significance.
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from statistics import mean

from experiments.research import neural_availability_study as study


def analyze(directory: Path) -> dict:
    freeze, data = study.verify_frozen(directory)
    manifest = json.loads((directory / "result-manifest.json").read_text())
    for name in ("report", "freeze", "model"):
        if study.sha256(directory / f"{name}.json") != manifest[f"{name}_sha256"]:
            raise ValueError("Sealed neural result changed: " + name)
    report = json.loads((directory / "report.json").read_text())
    indexed = {(r["group"], r["index"], r["policy"]): r for r in report["raw_runs"]}
    rows, replayed = [], 0
    for case in data["cases"]:
        obs = case["observed"]
        # Privileged source is made explicit; all original choices stay sealed.
        truth = [study.distribution(int(d["device_id"].split("-")[1]), case["day"],
                                    obs["slot"], case["group"]) for d in obs["devices"]]
        matrix = study.matrix_from_forecasts(obs, truth)
        best_expected = study.assign(matrix, "batch")
        oracle_expected = sum(matrix[i][j] for i, j in enumerate(best_expected)) / 4
        hindsight = []
        for assignment in itertools.permutations(range(4)):
            out = study.execute_assignment(case, "hindsight_diagnostic", assignment, matrix)
            hindsight.append({"assignment": assignment,
                "restricted": out["restricted_mean_completion_seconds"], "completed": out["completed"]})
        best_realized = min(hindsight, key=lambda row: (row["restricted"], row["assignment"]))
        max_completed = max(row["completed"] for row in hindsight)
        for policy in study.POLICIES:
            sealed = indexed[case["group"], case["index"], policy]
            repeat = study.execute_assignment(case, policy, sealed["assignment"], sealed["predicted_matrix"])
            if repeat != sealed:
                # JSON represents the tuple-valued matrix as arrays.
                if json.loads(json.dumps(repeat)) != sealed:
                    raise ValueError("Stored outcome did not reproduce")
            replayed += 1
            expected = sum(matrix[i][j] for i, j in enumerate(sealed["assignment"])) / 4
            rows.append({"group": case["group"], "index": case["index"], "policy": policy,
                "oracle_distribution_expected_seconds": expected,
                "distribution_oracle_regret_seconds": expected - oracle_expected,
                "realized_restricted_seconds": sealed["restricted_mean_completion_seconds"],
                "hindsight_restricted_seconds": best_realized["restricted"],
                "hindsight_completion_headroom": max_completed - sealed["completed"],
                "hindsight_restricted_headroom_seconds": sealed["restricted_mean_completion_seconds"]
                    - best_realized["restricted"]})
    tables = []
    for group in ("all_groups", *study.GROUPS):
        for policy in study.POLICIES:
            chosen = [row for row in rows if row["policy"] == policy
                      and (group == "all_groups" or row["group"] == group)]
            tables.append({"group": group, "policy": policy,
                **{field: mean(row[field] for row in chosen) for field in (
                    "oracle_distribution_expected_seconds", "distribution_oracle_regret_seconds",
                    "realized_restricted_seconds", "hindsight_restricted_seconds",
                    "hindsight_restricted_headroom_seconds")},
                "hindsight_completion_headroom_total": sum(row["hindsight_completion_headroom"] for row in chosen)})
    return {"post_outcome_diagnostic_only": True, "original_runs_replayed": replayed,
            "hindsight_assignments_evaluated": len(data["cases"]) * 24,
            "frozen_result_manifest": manifest, "source_sha256": study.sha256(Path(__file__)),
            "freeze_sha256": study.sha256(directory / "freeze.json"), "rows": rows, "tables": tables,
            "limitations": ["No policy or model is changed or retrained.",
                "Expected oracle uses privileged generator probabilities and the same approximate cost transform.",
                "Realized hindsight has future calendars; each objective has its own optimum.",
                "This does not add new independent test days or establish field effectiveness."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite diagnostic evidence")
    result = analyze(args.study)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"output": str(args.output), "replayed": result["original_runs_replayed"]}))


if __name__ == "__main__":
    main()
