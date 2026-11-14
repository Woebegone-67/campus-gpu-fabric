"""Seal, execute and audit a CP3 logical policy suite.

python -m experiments.cp3_suite freeze --output evidence/cp3/policy-suite
python -m experiments.cp3_suite run --suite evidence/cp3/policy-suite
python -m experiments.cp3_suite analyze --suite evidence/cp3/policy-suite

Freeze is one-shot. Validation cannot be re-sealed or silently replace failed runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from campus_fabric.contracts import models as m
from campus_fabric.workflows.acceptance_evidence import _gate_module
from experiments.compare_runs import aggregate_pairs, compare_runs, validate_run
from experiments.cp3_inputs import (
    DEVELOPMENT_SEEDS,
    FAMILIES,
    LIMITATIONS,
    MODEL,
    POLICIES,
    VALIDATION_SEEDS,
    scenario_input,
)
from experiments.cp3_runner import execute_scenario, policy_for, summarize_run

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_IDS = ("input", "summary", "events", "decisions", "receipts", "commands", "final-snapshot", "run-metadata", "resource-samples", "external-injections", "final-decision-snapshot")
THRESHOLDS = {"macro_area_ratio_max": .85, "work_harm_seconds": 300, "owner_service_fraction": .8,
              "completion_count_not_lower_each_pair": True, "target_baseline": "B2"}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded(data))
    temporary.replace(path)


def read_artifact_bytes(suite: Path, relative_path: str, sha256: str) -> bytes:
    """Administrator API helper: only bounded, hash-matching non-symlink JSON."""
    suite = Path(suite)
    relative = Path(relative_path)
    if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ValueError("Invalid artifact hash")
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.suffix != ".json":
        raise ValueError("Invalid artifact path")
    if suite.is_symlink():
        raise ValueError("Suite symlinks are not accepted")
    path = suite / relative
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent == suite or parent.is_relative_to(suite)):
        raise ValueError("Artifact symlinks are not accepted")
    if not path.resolve().is_relative_to(suite.resolve()) or path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("Artifact outside suite or too large")
    payload = path.read_bytes()
    if digest(payload) != sha256:
        raise ValueError(f"Artifact hash mismatch: {relative_path}")
    return payload


def read_artifact(suite: Path, relative_path: str, sha256: str) -> dict | list:
    return json.loads(read_artifact_bytes(suite, relative_path, sha256))


def _run_id(family, seed, policy):
    return f"{family}--{seed}--{policy}"


def _seal(suite):
    path = suite / "manifest.sha256"
    if path.is_symlink() or path.stat().st_size > 128:
        raise ValueError("Invalid manifest seal")
    return path.read_text().strip()


def load_suite_index(suite: Path, root: Path | None = None) -> dict:
    suite = Path(suite)
    manifest_sha = _seal(suite)
    manifest = read_artifact(suite, "manifest.json", manifest_sha)
    index_path = suite / "suite-index.json"
    if index_path.is_symlink() or index_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Invalid suite index")
    index = json.loads(index_path.read_bytes())
    if manifest.get("schema") != "cp3-manifest-v1" or index.get("schema") != "cp3-suite-index-v1":
        raise ValueError("Unsupported suite schema")
    if index.get("manifest_sha256") != manifest_sha or index.get("build") != manifest["build"]:
        raise ValueError("Suite index does not match sealed manifest")
    if root is not None and manifest["build"] != _gate_module(root).current_build(root):
        raise ValueError("Suite build differs from current checkout")
    seeds = DEVELOPMENT_SEEDS if manifest["partition"] == "development" else VALIDATION_SEEDS
    if (manifest["families"] != list(FAMILIES) or manifest["seeds"] != list(seeds)
            or manifest["policies"] != list(POLICIES) or manifest["model"] != MODEL
            or manifest["thresholds"] != THRESHOLDS):
        raise ValueError("Sealed suite protocol differs from registered protocol")
    expected = {_run_id(f, s, p): (f, s, p) for f in FAMILIES for s in seeds for p in POLICIES}
    if index.get("partition") != manifest["partition"] or index.get("expected_runs") != len(expected):
        raise ValueError("Suite index cardinality/partition mismatch")
    if len(index.get("runs", [])) != len(expected) or {r.get("run_id") for r in index["runs"]} != set(expected):
        raise ValueError("Suite index omits or duplicates frozen runs")
    for row in index["runs"]:
        if (row["scenario_family"], row["seed"], row["policy"]) != expected[row["run_id"]]:
            raise ValueError("Run label differs from frozen identity")
        if row["status"] not in {"not_run", "running", "complete", "failed"}:
            raise ValueError("Invalid run status")
        for label, artifact in row.get("artifacts", {}).items():
            if label not in ARTIFACT_IDS or artifact.get("path") != f"runs/{row['run_id']}/{label}.json":
                raise ValueError("Unexpected run artifact path")
    analysis = index.get("analysis")
    if analysis:
        if analysis.get("path") != "analysis.json":
            raise ValueError("Unexpected analysis path")
        read_artifact(suite, analysis["path"], analysis["sha256"])
    return index


def freeze_suite(root: Path, output: Path, partition: str = "validation") -> dict:
    root, output = Path(root).resolve(), Path(output)
    if partition not in {"development", "validation"}:
        raise ValueError("Unknown partition")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Refuse to re-seal a nonempty suite; preserve its original inputs and negative results")
    output.mkdir(parents=True, exist_ok=True)
    seeds = DEVELOPMENT_SEEDS if partition == "development" else VALIDATION_SEEDS
    inputs = []
    for family in FAMILIES:
        for seed in seeds:
            value = scenario_input(root, family, seed, development=partition == "development")
            label = f"inputs/{family}--{seed}.json"
            write_json(output / label, value)
            inputs.append({"scenario_family": family, "seed": seed, "path": label,
                           "sha256": digest((output / label).read_bytes())})
    empty = m.DecisionSnapshot(version=m.VersionStamp(epoch=0, revision=0), now=0., execution_mode="logical_simulation")
    manifest = {"schema": "cp3-manifest-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "partition": partition, "build": _gate_module(root).current_build(root), "model": MODEL,
        "families": list(FAMILIES), "seeds": list(seeds), "policies": list(POLICIES), "inputs": inputs,
        "policy_artifacts": {name: policy_for(name).plan(empty).policy.model_dump(mode="json") for name in POLICIES},
        "thresholds": THRESHOLDS, "limitations": LIMITATIONS,
        "freeze_rule": "Generator, model, policies, source, input bytes and thresholds sealed before validation. Existing suites cannot be replaced. Development seeds are disjoint.",
        "opportunity_ablation": "P-no-opportunity disables the active placement component. Recovery cost is inactive and has no fictitious ablation.",
        "scope": "Bounded logical integration experiment; final validation may contain negative or inconclusive results."}
    write_json(output / "manifest.json", manifest)
    sha = digest((output / "manifest.json").read_bytes())
    (output / "manifest.sha256").write_text(sha + "\n")
    index = {"schema": "cp3-suite-index-v1", "manifest_sha256": sha, "build": manifest["build"],
        "partition": partition, "expected_runs": len(FAMILIES)*len(seeds)*len(POLICIES), "analysis": None,
        "runs": [{"run_id": _run_id(f, s, p), "scenario_family": f, "seed": s, "policy": p,
                  "status": "not_run", "artifacts": {}} for f in FAMILIES for s in seeds for p in POLICIES]}
    write_json(output / "suite-index.json", index)
    return index


def _execute_run(root, suite, row, inputs, input_sha256, manifest, manifest_sha):
    """Independent process owns one temporary Kernel and one reserved run folder."""
    root, suite = Path(root), Path(suite)
    if _gate_module(root).current_build(root) != manifest["build"]:
        raise ValueError("Source changed before worker execution")
    with tempfile.TemporaryDirectory(prefix="isolated-cp3-") as temp:
        result = execute_scenario(inputs, row["policy"], db_path=Path(temp) / "kernel.sqlite")
    summary = summarize_run(inputs, result, row["run_id"], row["policy"], input_sha256)
    build_after = _gate_module(root).current_build(root)
    values = {"input": inputs, "summary": summary,
        "events": [e.model_dump(mode="json") for e in result["snapshot"].events],
        "decisions": result["decisions"], "receipts": result["receipts"], "commands": result["commands"],
        "final-snapshot": result["snapshot"].model_dump(mode="json"), "resource-samples": result["resource_samples"],
        "external-injections": result["external_injections"], "final-decision-snapshot": result["final_decision_snapshot"],
        "run-metadata": {"run_id": row["run_id"], "build": manifest["build"], "build_at_finish": build_after,
            "input_sha256": input_sha256, "manifest_sha256": manifest_sha,
            "model": manifest["model"], "policy_artifact": manifest["policy_artifacts"][row["policy"]],
            "python": sys.version, "platform": platform.platform(), "elapsed_wall_seconds": result["elapsed_wall_seconds"],
            "physical_memory_measurement": "not_measured", "runner_failures": result["failures"],
            "cost_measurement_limit": "Elapsed timings are diagnostic only; parallel run workers and other host processes may contend."}}
    directory = suite / "runs" / row["run_id"]
    artifacts = {}
    for name, value in values.items():
        path = directory / f"{name}.json"
        write_json(path, value)
        artifacts[name] = {"path": path.relative_to(suite).as_posix(), "sha256": digest(path.read_bytes())}
    return {**row, "artifacts": artifacts,
            "status": "failed" if result["failures"] or build_after != manifest["build"] else "complete",
            "elapsed_wall_seconds": result["elapsed_wall_seconds"]}


def run_suite(root: Path, suite: Path, *, limit: int | None = None, workers: int = 1):
    import fcntl
    import multiprocessing
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be 1..4")
    root, suite = Path(root), Path(suite)
    with (suite / ".run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        index = load_suite_index(suite, root=root)
        manifest = read_artifact(suite, "manifest.json", index["manifest_sha256"])
        input_map = {(i["scenario_family"], i["seed"]): i for i in manifest["inputs"]}
        for row in index["runs"]:
            if row["status"] == "running":
                row.update(status="failed", failure="Previous runner stopped; partial files retained without automatic replacement.")
        eligible = [r for r in index["runs"] if r["status"] == "not_run"]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be nonnegative")
            eligible = eligible[:limit]
        index["analysis"] = None
        write_json(suite / "suite-index.json", index)
        def prepare(row):
            if _gate_module(root).current_build(root) != manifest["build"]:
                raise ValueError("Source changed; refusing to start another validation run")
            ref = input_map[row["scenario_family"], row["seed"]]
            inputs = read_artifact(suite, ref["path"], ref["sha256"])
            (suite / "runs" / row["run_id"]).mkdir(parents=True, exist_ok=False)
            row["status"] = "running"
            write_json(suite / "suite-index.json", index)
            return (root, suite, row.copy(), inputs, ref["sha256"], manifest, index["manifest_sha256"])
        def retain(row, result=None, error=None):
            if error is not None:
                row.update(status="failed", failure=f"{type(error).__name__}: {error}")
                write_json(suite / "runs" / row["run_id"] / "worker-failure.json", {"error": row["failure"]})
            else:
                row.update(result)
            write_json(suite / "suite-index.json", index)
            print(json.dumps({"run": row["run_id"], "status": row["status"],
                              "seconds": row.get("elapsed_wall_seconds")}), flush=True)
        if workers == 1:
            for row in eligible:
                arguments = prepare(row)
                try:
                    result = _execute_run(*arguments)
                except Exception as exc:
                    retain(row, error=exc)
                else:
                    retain(row, result=result)
        else:
            waiting = iter(eligible)
            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                pending = {}
                def submit_one():
                    row = next(waiting, None)
                    if row is not None:
                        pending[pool.submit(_execute_run, *prepare(row))] = row
                for _ in range(workers):
                    submit_one()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        row = pending.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            retain(row, error=exc)
                        else:
                            retain(row, result=result)
                        submit_one()
        return index


def _validate_trace(artifacts, final):
    """Bidirectional accounting, without pretending this reconstructs A's state."""
    receipt_rows = artifacts["receipts"]
    receipts = {}
    for row in receipt_rows:
        m.CommitReceipt.model_validate_json(json.dumps(row))
        key = row["request_id"]
        if key in receipts and receipts[key] != row:
            raise ValueError("Different receipts reuse one request identity")
        receipts[key] = row
    final_events = {e.event_id: e for e in final.events}
    returned_ids = {event_id for r in receipt_rows for event_id in r["event_ids"]}
    if returned_ids != set(final_events):
        raise ValueError("Receipts do not account for all authoritative committed events")
    for receipt in receipt_rows:
        for event_id in receipt["event_ids"]:
            event = final_events[event_id]
            if event.version.model_dump(mode="json") != receipt["version"]:
                raise ValueError("Receipt and event versions disagree")
    command_rows = artifacts["commands"]
    commands = {r["command"]["request_id"]: r for r in command_rows}
    if len(commands) != len(command_rows):
        raise ValueError("Duplicate recorded direct command identity")
    expected_commands = {r for r in receipts if r.startswith("cp3-command-")}
    if set(commands) != expected_commands:
        raise ValueError("Direct command log does not match returned command receipts")
    for identity, row in commands.items():
        m.CommandEnvelope.model_validate_json(json.dumps(row["command"]))
        if row["receipt"] != receipts[identity]:
            raise ValueError("Direct command receipt mismatch")
    actual = {p.decision_id: p.model_dump(mode="json") for p in final.decisions}
    traced = {}
    for decision in artifacts["decisions"]:
        receipt, plan = decision["commit_receipt"], decision["plan"]
        if receipt is None:
            if plan["selected_candidate_id"] is not None:
                raise ValueError("Selected plan has no recorded commit result")
            continue
        direct = commands.get(receipt["request_id"])
        if direct is None or direct["receipt"] != receipt or direct["command"]["parameters"].get("plan") != plan:
            raise ValueError("Decision plan is not paired to its actual command/receipt")
        if receipt["outcome"] == "committed":
            if plan["decision_id"] in traced:
                raise ValueError("Duplicate accepted decision trace")
            traced[plan["decision_id"]] = plan
    if actual != traced:
        raise ValueError("Accepted plans and authoritative decision history differ")
    expected_external = Counter(encoded(e) for e in artifacts["input"]["events"] if e["at"] <= artifacts["input"]["horizon"])
    observed_external = Counter(encoded(p["input"]) for p in artifacts["external-injections"])
    if expected_external != observed_external:
        raise ValueError("External inputs lack a unique injection or explicit uncovered record")
    for probe in artifacts["external-injections"]:
        if probe["status"] == "injected":
            receipt = probe["receipt"]
            if receipt != receipts.get(receipt["request_id"]):
                raise ValueError("External injection has no matching returned receipt")
        elif probe["status"] != "not_injected_target_not_eligible":
            raise ValueError("Unknown external injection status")
    final_decision = m.DecisionSnapshot.model_validate_json(json.dumps(artifacts["final-decision-snapshot"]))
    if final_decision.version != final.version or final_decision.now != final.now:
        raise ValueError("Final snapshots describe different states")
    active = {a.allocation_id: a.placement for a in final.allocations if a.placement is not None}
    if {lease.allocation_id: lease.placement for lease in final_decision.leases} != active:
        raise ValueError("Final lease snapshot differs from authoritative allocation placement")


def audit_suite(root: Path, suite: Path) -> dict:
    """Recalculate from hash-checked final states and traces, never trust green labels."""
    root, suite = Path(root), Path(suite)
    index = load_suite_index(suite, root=root)
    manifest = read_artifact(suite, "manifest.json", index["manifest_sha256"])
    frozen = {(i["scenario_family"], i["seed"]): i for i in manifest["inputs"]}
    summaries, errors, counts, aggregate_instrumentation, injection_counts = {}, [], Counter(), Counter(), Counter()
    observed_by_family = {family: Counter() for family in FAMILIES}
    for row in index["runs"]:
        counts[row["status"]] += 1
        if set(row["artifacts"]) != set(ARTIFACT_IDS):
            errors.append({"run_id": row["run_id"], "reason": "missing run artifacts", "status": row["status"]})
            continue
        try:
            artifacts = {name: read_artifact(suite, ref["path"], ref["sha256"]) for name, ref in row["artifacts"].items()}
            inp = frozen[row["scenario_family"], row["seed"]]
            if digest(encoded(artifacts["input"])) != inp["sha256"]:
                raise ValueError("Run input differs from frozen input")
            read_artifact(suite, inp["path"], inp["sha256"])
            metadata = artifacts["run-metadata"]
            if (metadata["build"] != manifest["build"] or metadata["build_at_finish"] != manifest["build"]
                    or metadata["manifest_sha256"] != index["manifest_sha256"]
                    or metadata["policy_artifact"] != manifest["policy_artifacts"][row["policy"]]):
                raise ValueError("Run provenance differs from sealed build/model/policy")
            final = m.ReadSnapshot.model_validate_json(json.dumps(artifacts["final-snapshot"]))
            if artifacts["events"] != [e.model_dump(mode="json") for e in final.events]:
                raise ValueError("Event artifact differs from final authoritative snapshot")
            _validate_trace(artifacts, final)
            result = {"snapshot": final, "decisions": artifacts["decisions"], "failures": metadata["runner_failures"],
                      "resource_samples": artifacts["resource-samples"]}
            recomputed = summarize_run(artifacts["input"], result, row["run_id"], row["policy"], inp["sha256"])
            if recomputed != artifacts["summary"]:
                raise ValueError("Supplied summary differs from authoritative final-state recalculation")
            validate_run(recomputed)
            if final.now != artifacts["input"]["horizon"]:
                raise ValueError("Runner did not reach the sealed observation horizon")
            committed = {p.decision_id: p.model_dump(mode="json") for p in final.decisions}
            for decision in artifacts["decisions"]:
                plan = decision["plan"]
                if plan["policy"] != manifest["policy_artifacts"][row["policy"]]:
                    raise ValueError("Actual policy identity differs from frozen policy")
                receipt = decision["commit_receipt"]
                if receipt and receipt["outcome"] == "committed" and committed.get(plan["decision_id"]) != plan:
                    raise ValueError("Accepted decision is absent from authoritative snapshot")
            summaries[row["scenario_family"], row["seed"], row["policy"]] = recomputed
            aggregate_instrumentation.update(recomputed["instrumentation"])
            observed_by_family[row["scenario_family"]].update(recomputed["event_counts"])
            for command in artifacts["commands"]:
                parameters = command["command"]["parameters"]
                if parameters["kind"] == "allocation.checkpoint" and command["receipt"]["outcome"] == "committed":
                    observed_by_family[row["scenario_family"]]["checkpoint_report_status:"+parameters["status"]] += 1
            for probe in artifacts["external-injections"]:
                injection_counts[f"{probe['input']['kind']}:{probe['status']}"] += 1
            if row["status"] != "complete" or recomputed["runner_failures"]:
                errors.append({"run_id": row["run_id"], "reason": "runner failure", "details": recomputed["runner_failures"]})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append({"run_id": row["run_id"], "reason": str(exc)})
    comparisons = {}
    for baseline in ("B0", "B1", "B2", "P-no-opportunity"):
        pairs = []
        for family in FAMILIES:
            for seed in manifest["seeds"]:
                if (family, seed, baseline) in summaries and (family, seed, "P") in summaries:
                    pairs.append(compare_runs(summaries[family, seed, baseline], summaries[family, seed, "P"]))
        aggregate = aggregate_pairs(pairs) if pairs else {"families": [], "macro_area_ratio": None}
        target = {"ratio_met": aggregate["macro_area_ratio"] is not None and aggregate["macro_area_ratio"] <= .85,
            "paired_completion_not_lower": bool(pairs) and all(p["completion_count_not_lower"] for p in pairs),
            "work_severe_harms": sum(w["severe_harm_gt_300_seconds"] for p in pairs for w in p["per_work"]),
            "owner_severe_harms": sum(o["severe_harm_below_80_percent"] for p in pairs for o in p["per_owner"]),
            "pair_count": len(pairs)}
        comparisons[baseline] = {"aggregate": aggregate, "pairs": pairs, "target": target}
    ablation_mismatches = []
    for family in FAMILIES:
        for seed in manifest["seeds"]:
            base = summaries.get((family, seed, "B2"))
            off = summaries.get((family, seed, "P-no-opportunity"))
            if base and off and base["works"] != off["works"]:
                ablation_mismatches.append({"family": family, "seed": seed})
    if ablation_mismatches:
        errors.append({"reason": "Opportunity-off full runs differ from B2", "pairs": ablation_mismatches})
    aggregate_instrumentation["ablation_full_run_identity_violations"] = len(ablation_mismatches)
    evidence_complete = not errors and counts["complete"] == index["expected_runs"]
    # Execution completeness is explicitly separated from blueprint semantic coverage.
    coverage = {"family_count": len(FAMILIES), "seeds_per_family": len(manifest["seeds"]),
        "work_count_per_run": MODEL["works_per_run"], "input_probes": dict(injection_counts),
        "actual_events_by_family": {k: dict(v) for k, v in observed_by_family.items()},
        "heterogeneous_actual_durations": {"status": "NOT_IMPLEMENTED", "reason": MODEL["duration_limit"]},
        "recovery_cost_ablation": {"status": "NOT_APPLICABLE", "reason": MODEL["recovery_cost_component"]},
        "late_callback_conditionally_skipped": injection_counts["late_callback:not_injected_target_not_eligible"],
        "logical_checkpoint_artifact_continuation": "NOT_CLAIMED; separate real CPU gate owns this",
        "semantic_coverage_complete": False}
    target = comparisons["B2"]["target"]
    target_met = (index["partition"] == "validation" and evidence_complete and target["ratio_met"]
                  and target["paired_completion_not_lower"] and not target["work_severe_harms"] and not target["owner_severe_harms"])
    return {"schema": "cp3-analysis-v1", "build": manifest["build"], "manifest_sha256": index["manifest_sha256"],
        "partition": index["partition"], "evidence_complete": evidence_complete, "coverage_complete": False,
        "target_met": target_met, "run_counts": dict(counts), "run_failures": errors,
        "comparisons": comparisons, "instrumentation": dict(aggregate_instrumentation),
        "coverage": coverage, "thresholds": THRESHOLDS, "limitations": manifest["limitations"],
        "interpretation": "R target uses P versus B2, all five families equally weighted. B0/B1/off comparisons retained separately. Green labels are not a full CP3 pass."}


def analyze_suite(root: Path, suite: Path) -> dict:
    analysis = audit_suite(root, suite)
    index = load_suite_index(suite, root=root)
    write_json(suite / "analysis.json", analysis)
    index["analysis"] = {"path": "analysis.json", "sha256": digest((suite / "analysis.json").read_bytes())}
    write_json(suite / "suite-index.json", index)
    return analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run", "analyze"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--partition", choices=("development", "validation"), default="validation")
    parser.add_argument("--workers", type=int, default=1, help="Independent worker processes, 1..4")
    parser.add_argument("--limit", type=int, help="Bound execution count; remaining runs stay NOT_RUN")
    args = parser.parse_args()
    suite = args.output if args.action == "freeze" else args.suite
    if suite is None:
        parser.error("--output is required for freeze; --suite is required for run/analyze")
    if args.action == "freeze":
        value = freeze_suite(args.root, suite, args.partition)
        print(json.dumps({"expected_runs": value["expected_runs"], "manifest_sha256": value["manifest_sha256"]}))
    elif args.action == "run":
        run_suite(args.root, suite, limit=args.limit, workers=args.workers)
    else:
        value = analyze_suite(args.root, suite)
        print(json.dumps({k: value[k] for k in ("evidence_complete", "coverage_complete", "target_met", "run_counts")}))


if __name__ == "__main__":
    main()
