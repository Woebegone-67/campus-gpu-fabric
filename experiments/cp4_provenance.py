"""Audit registered CP4 figure/result sources without executing or resealing a suite."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from campus_fabric.workflows.acceptance_evidence import _gate_module
from experiments.cp3_suite import (
    ARTIFACT_IDS,
    ROOT,
    audit_suite,
    digest,
    encoded,
    load_suite_index,
    read_artifact,
    write_json,
)
from experiments.cp3_validation import suite_evidence


def _status(checks):
    values = [row["status"] for row in checks]
    if "FAIL" in values:
        return "FAIL"
    return "PASS" if values and all(value == "PASS" for value in values) else "NOT_RUN"


def _bounded_bytes(root: Path, relative: str, sha256: str | None = None) -> bytes:
    """Read only explicit CP4 evidence, never runtime/auth files or symlink targets."""
    if not isinstance(relative, str):
        raise ValueError("Evidence path must be a relative string")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.is_relative_to("evidence/cp4"):
        raise ValueError("Figure index and outputs must be inside evidence/cp4")
    target = root / path
    if any(p.is_symlink() for p in (target, *target.parents) if p.is_relative_to(root)):
        raise ValueError("Evidence symlinks are not accepted")
    if not target.resolve().is_relative_to((root / "evidence/cp4").resolve()):
        raise ValueError("Evidence path escaped evidence/cp4")
    if target.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Figure evidence exceeds 64 MiB")
    payload = target.read_bytes()
    if not payload:
        raise ValueError("Figure evidence is empty")
    if sha256 is not None:
        if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Invalid output SHA-256")
        if digest(payload) != sha256:
            raise ValueError("Figure/result file hash mismatch")
    return payload


def _pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("A nonempty JSON pointer is required for every source value")
    current = document
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("Invalid JSON pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("Invalid JSON array index")
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise ValueError("JSON pointer crosses a scalar")
    return current


def _run_chain(suite, row, manifest, manifest_sha, frozen):
    """CP3 recalculation plus explicit metadata fields omitted by its older audit."""
    if set(row["artifacts"]) != set(ARTIFACT_IDS):
        raise ValueError("Run does not have all eleven raw artifacts")
    ref = frozen[row["scenario_family"], row["seed"]]
    metadata_ref = row["artifacts"]["run-metadata"]
    metadata = read_artifact(suite, metadata_ref["path"], metadata_ref["sha256"])
    input_ref = row["artifacts"]["input"]
    inputs = read_artifact(suite, input_ref["path"], input_ref["sha256"])
    if (metadata["run_id"] != row["run_id"] or metadata["input_sha256"] != ref["sha256"]
            or metadata["model"] != manifest["model"]
            or metadata["build"] != manifest["build"]
            or metadata["build_at_finish"] != manifest["build"]
            or metadata["manifest_sha256"] != manifest_sha
            or metadata["policy_artifact"] != manifest["policy_artifacts"][row["policy"]]):
        raise ValueError("Run identity/input/model/build/policy chain differs from sealed manifest")
    if (inputs["scenario_family"] != row["scenario_family"] or inputs["seed"] != row["seed"]
            or inputs["partition"] != manifest["partition"] or inputs["model"] != manifest["model"]
            or input_ref["sha256"] != ref["sha256"]):
        raise ValueError("Run input identity/model differs from frozen input")
    return {"run_id": row["run_id"], "scenario_family": row["scenario_family"],
            "seed": row["seed"], "policy": row["policy"], "input": ref,
            "model": metadata["model"], "build": metadata["build"],
            "policy_artifact": metadata["policy_artifact"], "artifacts": row["artifacts"]}


def _figures(root, figure_index, build, suite, verified_runs):
    section = {"status": "NOT_RUN", "registered": 0, "verified": 0, "items": [], "checks": []}
    if figure_index is None or not figure_index.exists():
        section["checks"].append({"id": "registered-figure-index", "status": "NOT_RUN",
                                  "observed": "No figure/result index was supplied or found"})
        return section
    relative = figure_index.absolute().relative_to(root).as_posix()
    raw = _bounded_bytes(root, relative)
    index = json.loads(raw)
    if index["schema"] != "cp4-figure-index-v1" or index["build"] != build:
        raise ValueError("Figure index schema/build differs from current candidate")
    rows = index["figures"]
    if not isinstance(rows, list):
        raise ValueError("Figure index entries must be a list")
    ids = [row["id"] for row in rows]
    if any(not isinstance(identity, str) or not identity.strip() for identity in ids) or len(set(ids)) != len(ids):
        raise ValueError("Figure/result identifiers must be nonempty and unique")
    section.update(registered=len(rows), index={"path": relative, "sha256": digest(raw)})
    section["checks"].append({"id": "nonempty-figure-index", "status": "PASS" if rows else "NOT_RUN",
                              "observed": len(rows)})
    for row in rows:
        result = {"id": row["id"], "status": "FAIL"}
        try:
            if row["kind"] not in {"figure", "result"}:
                raise ValueError("Entry kind must be figure or result")
            if Path(row["path"]) == Path(relative):
                raise ValueError("Figure index cannot cite itself as an output")
            original = _bounded_bytes(root, row["path"], row["sha256"])
            if not isinstance(row["sources"], list) or not row["sources"]:
                raise ValueError("Every figure/result needs at least one explicit source")
            sources = []
            for source in row["sources"]:
                run = verified_runs.get(source["run_id"])
                if run is None:
                    raise ValueError("Figure cites a run without a verified execution chain")
                if source["artifact"] not in {"summary", "input", "run-metadata"}:
                    raise ValueError("Source artifact must be summary, input or run-metadata")
                ref = run["artifacts"][source["artifact"]]
                if source["sha256"] != ref["sha256"]:
                    raise ValueError("Figure source hash differs from registered run artifact")
                document = read_artifact(suite, ref["path"], ref["sha256"])
                actual = _pointer(document, source["json_pointer"])
                if encoded(actual) != encoded(source["value"]):
                    raise ValueError("Registered figure source value differs from raw artifact")
                sources.append({**source, "path": ref["path"], "input": run["input"],
                                "model": run["model"], "build": run["build"]})
            if _bounded_bytes(root, row["path"], row["sha256"]) != original:
                raise ValueError("Figure output changed during audit")
            result.update(status="PASS", kind=row["kind"], path=row["path"],
                          sha256=row["sha256"], sources=sources)
            section["verified"] += 1
        except (ValueError, KeyError, TypeError, AttributeError, OSError, IndexError) as exc:
            result["reason"] = str(exc)
        section["items"].append(result)
        section["checks"].append({"id": row["id"], "status": result["status"],
                                  "observed": result.get("reason", "Output bytes and registered source values verified")})
    if _bounded_bytes(root, relative) != raw:
        raise ValueError("Figure index changed during audit")
    section["status"] = _status(section["checks"])
    return section


def audit_provenance(root: Path, suite: Path, figure_index: Path | None = None) -> dict:
    """Return bounded source evidence; never infer CP3/CP4 or algorithm acceptance."""
    root, suite = Path(root).resolve(), Path(suite).absolute()
    figure_index = Path(figure_index).absolute() if figure_index is not None else None
    gate = _gate_module(root)
    build = gate.current_build(root)
    report = {"schema": "cp4-provenance-v1", "check": "CP4-PROVENANCE", "build": build,
              "observed_at": datetime.now(timezone.utc).isoformat(), "status": "NOT_RUN", "checks": [],
              "scope": "Current-build CP3 raw-run provenance and explicitly registered CP4 figure/result sources",
              "suite_sources": {"status": "NOT_RUN", "path": str(suite), "runs": []},
              "coverage": {"expected_runs": 0, "verified_runs": 0, "registered_figures": 0, "verified_figures": 0},
              "algorithm_target": {"status": "NOT_RUN", "reason": "No validated formal suite"},
              "limitations": ["Source integrity is independent of algorithm benefit and checkpoint acceptance.",
                              "Unregistered figures/results are outside this audit; an empty index is NOT_RUN.",
                              "File hashes and declared source values are verified, not the visual encoding or prose of a figure.",
                              "No new experiments, complete authoritative state replay, CPU/GPU performance or human sign-off are claimed."]}
    verified_runs = {}
    try:
        if not suite.exists():
            report["checks"].append({"id": "sealed-suite", "status": "NOT_RUN", "observed": "Suite directory is absent"})
        else:
            before = suite_evidence(root, suite)
            index = load_suite_index(suite, root=root)
            manifest = read_artifact(suite, "manifest.json", index["manifest_sha256"])
            frozen = {(entry["scenario_family"], entry["seed"]): entry for entry in manifest["inputs"]}
            expected_inputs = {(family, seed) for family in manifest["families"] for seed in manifest["seeds"]}
            if set(frozen) != expected_inputs or len(frozen) != len(manifest["inputs"]):
                raise ValueError("Frozen input identities are missing or duplicated")
            for (family, seed), ref in frozen.items():
                inp = read_artifact(suite, ref["path"], ref["sha256"])
                if (inp["scenario_family"], inp["seed"], inp["partition"], inp["model"]) != (family, seed, manifest["partition"], manifest["model"]):
                    raise ValueError("Frozen input identity/model differs from manifest")
            analysis = audit_suite(root, suite)
            if index.get("analysis"):
                ref = index["analysis"]
                cached_analysis = read_artifact(suite, ref["path"], ref["sha256"])
                if encoded(cached_analysis) != encoded(analysis):
                    raise ValueError("Registered analysis differs from raw-run recalculation")
            general_failures = [failure for failure in analysis["run_failures"] if "run_id" not in failure]
            if general_failures:
                raise ValueError("Strict suite audit found cross-run inconsistencies: " + str(general_failures))
            failures = {failure["run_id"]: failure["reason"] for failure in analysis["run_failures"] if "run_id" in failure}
            checks = []
            run_rows = []
            for row in index["runs"]:
                item = {"run_id": row["run_id"], "execution_status": row["status"], "status": "NOT_RUN"}
                if row["status"] == "not_run" and not row["artifacts"]:
                    item["reason"] = "Frozen run has not been executed"
                elif row["status"] == "running":
                    item["reason"] = "Run has not completed"
                else:
                    try:
                        if row["status"] != "complete" or row["run_id"] in failures:
                            raise ValueError(failures.get(row["run_id"], "Run is not complete"))
                        chain = _run_chain(suite, row, manifest, index["manifest_sha256"], frozen)
                        verified_runs[row["run_id"]] = chain
                        item.update(chain, status="PASS")
                    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                        item.update(status="FAIL", reason=str(exc))
                run_rows.append(item)
                checks.append({"id": row["run_id"], "status": item["status"], "observed": item.get("reason", "Verified raw-run source chain")})
            if suite_evidence(root, suite) != before:
                raise ValueError("Sealed suite changed during source audit")
            report["suite_sources"] = {"status": _status(checks), "path": str(suite),
                                        "partition": manifest["partition"], "manifest_sha256": index["manifest_sha256"],
                                        "model": manifest["model"], "runs": run_rows, "evidence": before}
            report["coverage"].update(expected_runs=index["expected_runs"], verified_runs=len(verified_runs),
                                      run_status_counts=dict(Counter(row["status"] for row in index["runs"])),
                                      families_with_verified_runs=sorted({row["scenario_family"] for row in verified_runs.values()}))
            report["checks"].append({"id": "sealed-suite", "status": report["suite_sources"]["status"],
                                      "observed": {"verified": len(verified_runs), "expected": index["expected_runs"]}})
            if manifest["partition"] == "development":
                report["algorithm_target"] = {"status": "NOT_APPLICABLE", "reason": "Development sources cannot establish the formal target"}
            elif report["suite_sources"]["status"] == "PASS":
                report["algorithm_target"] = {"status": "MET" if analysis["target_met"] else "NOT_MET",
                                              "comparison": analysis["comparisons"]["B2"]["target"],
                                              "macro_area_ratio": analysis["comparisons"]["B2"]["aggregate"]["macro_area_ratio"]}
    except (ValueError, KeyError, TypeError, AttributeError, OSError, IndexError) as exc:
        report["suite_sources"].update(status="FAIL", reason=str(exc))
        report["checks"].append({"id": "sealed-suite", "status": "FAIL", "observed": str(exc)})
        verified_runs.clear()
    try:
        report["figures"] = _figures(root, figure_index, build, suite, verified_runs)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, IndexError) as exc:
        report["figures"] = {"status": "FAIL", "registered": 0, "verified": 0, "items": [], "reason": str(exc)}
    figures = report["figures"]
    report["coverage"].update(registered_figures=figures["registered"], verified_figures=figures["verified"])
    report["checks"].append({"id": "registered-figure-sources", "status": figures["status"],
                              "observed": figures.get("reason", {"verified": figures["verified"], "registered": figures["registered"]})})
    if report["suite_sources"].get("evidence"):
        try:
            if suite_evidence(root, suite) != report["suite_sources"]["evidence"]:
                raise ValueError("Suite sources changed while auditing figure/result references")
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            report["suite_sources"].update(status="FAIL", reason=str(exc))
            report["algorithm_target"] = {"status": "NOT_RUN", "reason": "Sources changed during audit"}
            report["checks"].append({"id": "source-snapshot-stable", "status": "FAIL", "observed": str(exc)})
    report["build_at_finish"] = gate.current_build(root)
    report["checks"].append({"id": "candidate-build-stable", "status": "PASS" if report["build_at_finish"] == build else "FAIL",
                              "observed": report["build_at_finish"]})
    report["status"] = _status(report["checks"])
    if report["build_at_finish"] != build:
        report["status"] = "STALE"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--suite", type=Path, help="Defaults to ROOT/evidence/cp3/policy-suite")
    parser.add_argument("--figure-index", type=Path, help="Explicit cp4-figure-index-v1 JSON under ROOT/evidence/cp4")
    parser.add_argument("--output", type=Path, help="New output path; existing evidence is never overwritten")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    suite = args.suite or root / "evidence/cp3/policy-suite"
    output = args.output or root / "evidence/cp4/provenance.json"
    if output.exists():
        parser.error("Output already exists; choose a new evidence path")
    report = audit_provenance(root, suite, args.figure_index)
    write_json(output, report)
    print(json.dumps({"check": report["check"], "status": report["status"], "coverage": report["coverage"], "output": str(output)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
