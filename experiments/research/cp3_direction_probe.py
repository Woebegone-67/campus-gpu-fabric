"""Run a small, predeclared action-space diagnostic without changing product P."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campus_fabric.contracts.models import VersionedArtifact
from campus_fabric.policies.baselines import BaselinePolicy, _digest
from campus_fabric.policies.candidate import _can_coexist
from campus_fabric.workflows.acceptance_evidence import _gate_module
from experiments import cp3_runner
from experiments.cp3_inputs import MODEL

ROOT = Path(__file__).resolve().parents[2]
AGE_GUARD = 20


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


class AdmissionProbe:
    """Research proxy, explicitly not registered as product P or a fair scheduler."""

    def __init__(self):
        self.baseline = BaselinePolicy("B2", max_candidates=512)
        self.sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def plan(self, snapshot):
        base = self.baseline.plan(snapshot)
        demands = {d.demand_id: d for d in snapshot.demands}
        legal = [c for c in base.candidates if c.feasibility == "legal"]
        selected = None
        if legal:
            priority = max(demands[c.demand_id].priority for c in legal)
            eligible = [c for c in legal if demands[c.demand_id].priority == priority]
            aged = [c for c in eligible
                    if snapshot.now - demands[c.demand_id].arrived_at >= AGE_GUARD]
            if aged:
                oldest = min((demands[c.demand_id] for c in aged),
                             key=lambda d: (d.arrived_at, d.demand_id)).demand_id
                selected = next(c for c in aged if c.demand_id == oldest)
            else:
                counts = {
                    c.candidate_id: len({other.demand_id for other in eligible
                                        if other.demand_id != c.demand_id
                                        and _can_coexist(c, other, snapshot)})
                    for c in eligible
                }
                # Python min preserves enumeration order for equal keys.
                selected = min(eligible, key=lambda c: -counts[c.candidate_id])
        return base.model_copy(update={
            "decision_id": "research-" + _digest([self.sha, snapshot.model_dump(mode="json")])[:32],
            "policy": VersionedArtifact(name="research-coadmission", version="0.0.1", sha256=self.sha),
            "selected_candidate_id": selected.candidate_id if selected else None,
            "rationale": "Research-only current co-admission proxy; not a proven fair or optimal policy.",
            "tie_break_rule": "Maximum priority; 20-second oldest guard; current co-admission count; B2 enumeration.",
        })


def work(i, width=1, at=0, ttl=300):
    return dict(intent_id=f"probe-work-{i:02d}", owner_id=f"probe-owner-{i}",
                arrived_at=float(at), gpu_model="A10", gpu_count=width, gpu_memory_mib=1024,
                profile=None, cpu_millicores=1000, memory_mib=1024, priority=0,
                ttl_seconds=ttl, timeout_seconds=60, max_retries=0, max_restarts=0,
                checkpoint_behavior="ready", checkpoint_timeout_seconds=3)


def inputs():
    inventory = json.loads((ROOT / "configs/cp1_inventory.json").read_text())["nodes"][:1]
    initial = [work(0, 2), work(1), work(2)]
    cases = {
        "packing-witness": initial,
        "lease-counterexample": [work(0, 2, ttl=30), work(1), work(2)],
        "homogeneous-neutral": [work(i) for i in range(4)],
        "late-wide": initial + [work(3, 2, at=15)],
        "late-narrow": initial + [work(3, 1, at=15)],
    }
    scenarios = {name: dict(schema="research-direction-input-v1", scenario_family=name,
                       seed=None, partition="mechanism_probe", horizon=80, model=deepcopy(MODEL),
                       works=deepcopy(works), events=[], inventory=deepcopy(inventory),
                       cancellation_policy="count_until_horizon", service_unit=MODEL["service_unit"],
                       scope="Designed mechanism witness; no formal CP3 validation claim.",
                       limitations=["Uniform duration within each case, two GPUs, synthetic micro-inputs.",
                                    "Co-admission proxy has no general fairness guarantee."])
            for name, works in cases.items()}
    scenarios["lease-counterexample"]["model"]["duration_seconds"] = 20
    return scenarios


def metrics(summary):
    rows = summary["works"]
    horizon = summary["horizon"]
    return dict(area=sum((min(w["completed_at"], horizon) if w["status"] == "completed"
                          else horizon) - w["arrived_at"] for w in rows),
                completed=sum(w["status"] == "completed" for w in rows),
                failed=sum(w["status"] == "failed" for w in rows),
                works=rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output
    if out.exists():
        raise SystemExit("Refusing to overwrite an existing diagnostic.")
    out.mkdir(parents=True)
    scenarios = inputs()
    start_build = _gate_module(ROOT).current_build(ROOT)
    manifest = dict(schema="cp3-direction-probe-v1", created_at=datetime.now(timezone.utc).isoformat(),
                    source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    protocol_sha256=hashlib.sha256(Path(__file__).with_name("CP3_DIRECTION_PROBE.md").read_bytes()).hexdigest(),
                    source_status=subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True),
                    build=start_build, inputs=scenarios, execution_budget=11, age_guard_seconds=AGE_GUARD,
                    scope="One-pass mechanism diagnosis, not validation or product policy promotion.")
    write(out / "manifest.json", manifest)
    seal = hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
    (out / "manifest.sha256").write_text(seal + "\n")
    original_factory = cp3_runner.policy_for
    outputs = {}
    first_actions = {}
    with tempfile.TemporaryDirectory(prefix="cp3-direction-") as scratch:
        for name, inp in scenarios.items():
            outputs[name] = {}
            for label in ("B2", "coadmission", "P") if name == "packing-witness" else ("B2", "coadmission"):
                def factory(policy_name, max_candidates=512):
                    if label == "coadmission" and policy_name == "P":
                        return AdmissionProbe()
                    return original_factory(policy_name, max_candidates)
                run_name = name + "--" + label
                with patch.object(cp3_runner, "policy_for", factory):
                    result = cp3_runner.execute_scenario(inp, "P" if label == "coadmission" else label,
                                                        db_path=Path(scratch) / (run_name + ".db"))
                raw = {k: v.model_dump(mode="json") if k == "snapshot" else v for k, v in result.items()}
                write(out / "runs" / run_name / "raw.json", raw)
                assert not result["failures"], result["failures"]
                assert result["snapshot"].now == inp["horizon"]
                summary = cp3_runner.summarize_run(inp, result, run_name, label,
                                                  hashlib.sha256(encoded(inp)).hexdigest())
                write(out / "runs" / run_name / "summary.json", summary)
                outputs[name][label] = metrics(summary)
                if label == "coadmission":
                    first_actions[name] = [dict(time=d["snapshot"]["now"],
                                               selected=d["plan"]["selected_candidate_id"],
                                               candidates=d["plan"]["candidates"])
                                           for d in result["decisions"] if d["snapshot"]["now"] < 15]
    comparisons = {}
    for name, rows in outputs.items():
        before, after = rows["B2"], rows["coadmission"]
        comparisons[name] = dict(baseline_area=before["area"], probe_area=after["area"],
                                 ratio=after["area"] / before["area"],
                                 baseline_completed=before["completed"], probe_completed=after["completed"],
                                 baseline_failed=before["failed"], probe_failed=after["failed"])
    finish_build = _gate_module(ROOT).current_build(ROOT)
    assert finish_build == start_build, "Source changed during diagnostic."
    report = dict(manifest_sha256=seal, build_at_start=start_build, build_at_finish=finish_build,
                  run_count=sum(len(x) for x in outputs.values()),
                  comparisons=comparisons, metrics=outputs,
                  changed_future_common_prefix_equal=first_actions["late-wide"] == first_actions["late-narrow"],
                  promotion_allowed=False, formal_validation=False,
                  interpretation="A positive witness only refutes no-headroom in all action classes. Retain counterexamples; no deployment or tuning follows.")
    write(out / "analysis.json", report)
    print(json.dumps({"run_count": report["run_count"], "comparisons": comparisons,
                      "common_prefix_equal": report["changed_future_common_prefix_equal"]}, indent=2))


if __name__ == "__main__":
    main()
