"""Two-run adversarial follow-up; candidate policy is unchanged."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from campus_fabric.workflows.acceptance_evidence import _gate_module
from experiments import cp3_runner
from experiments.research.cp3_direction_probe import (
    ROOT,
    AdmissionProbe,
    encoded,
    inputs,
    metrics,
    write,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    out = parser.parse_args().output
    if out.exists():
        raise SystemExit("Refuse to overwrite previous evidence.")
    out.mkdir(parents=True)
    inp = inputs()["packing-witness"]
    inp["scenario_family"] = "recall-owner-harm"
    inp["events"] = [dict(at=10, kind="recall", device_ids=["sim-a10-gpu0", "sim-a10-gpu1"],
                          notice_seconds=1, duration_seconds=120)]
    build = _gate_module(ROOT).current_build(ROOT)
    manifest = dict(input=inp, build=build, run_budget=2,
                    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    protocol_sha256=hashlib.sha256(Path(__file__).with_name("CP3_DIRECTION_FOLLOWUP.md").read_bytes()).hexdigest(),
                    scope="One predeclared Owner-harm counterexample; not a new formal benchmark.")
    write(out / "manifest.json", manifest)
    seal = hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
    (out / "manifest.sha256").write_text(seal + "\n")
    original = cp3_runner.policy_for
    results = {}
    with tempfile.TemporaryDirectory(prefix="cp3-owner-probe-") as scratch:
        for label in ("B2", "coadmission"):
            def factory(name, max_candidates=512):
                return AdmissionProbe() if label == "coadmission" and name == "P" else original(name, max_candidates)
            with patch.object(cp3_runner, "policy_for", factory):
                raw = cp3_runner.execute_scenario(inp, "P" if label == "coadmission" else "B2",
                                                 db_path=Path(scratch) / (label + ".db"))
            write(out / label / "raw.json", {k: v.model_dump(mode="json") if k == "snapshot" else v for k, v in raw.items()})
            assert not raw["failures"], raw["failures"]
            summary = cp3_runner.summarize_run(inp, raw, "owner-harm--" + label, label,
                                              hashlib.sha256(encoded(inp)).hexdigest())
            write(out / label / "summary.json", summary)
            results[label] = metrics(summary)
    assert _gate_module(ROOT).current_build(ROOT) == build
    before = {w["owner_id"]: w["effective_service"] for w in results["B2"]["works"]}
    after = {w["owner_id"]: w["effective_service"] for w in results["coadmission"]["works"]}
    harms = [dict(owner_id=k, baseline_service=v, candidate_service=after[k])
             for k, v in before.items() if v > 0 and after[k] < 0.8 * v]
    report = dict(manifest_sha256=seal, build_at_start=build, build_at_finish=build, results=results,
                  area_ratio=results["coadmission"]["area"] / results["B2"]["area"],
                  serious_owner_harms=harms, promotion_allowed=False, formal_validation=False)
    write(out / "analysis.json", report)
    print(json.dumps({"areas": {k: v["area"] for k, v in results.items()},
                      "completed": {k: v["completed"] for k, v in results.items()},
                      "ratio": report["area_ratio"], "owner_harms": harms}, indent=2))


if __name__ == "__main__":
    main()
