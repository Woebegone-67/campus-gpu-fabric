"""Trace historical CP3 publications without relabelling them as CP4 runs."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def raw(audit, ref):
    data = audit.artifact(ref)
    if ref.get("encoding") == "gzip":
        data = gzip.decompress(data)
    require(sha(data) == ref.get("original_sha256", ref.get("sha256")), "Original decompressed bytes changed")
    return data


def read(audit, ref):
    return json.loads(raw(audit, ref))


def outcome(inputs, snapshot, summary):
    horizon = inputs["horizon"]
    intents = {x["intent_id"]: x for x in snapshot["intents"]}
    works = summary["works"]
    require([w["intent_id"] for w in inputs["works"]] == [w["intent_id"] for w in works], "Work cohort changed")
    area, completed, owners = 0., 0, Counter()
    for source, item in zip(inputs["works"], works):
        intent = intents.get(item["intent_id"], {})
        state = intent.get("state")
        state = state if state in {"completed", "failed", "cancelled"} else "unfinished"
        result = intent.get("result") or {}
        time = result.get("recorded_at") if state == "completed" else None
        require(source["arrived_at"] == item["arrived_at"] and source["owner_id"] == item["owner_id"]
                and state == item["status"] and time == item["completed_at"], "Summary disagrees with raw work state")
        area += max(0., min(time if time is not None else horizon, horizon) - item["arrived_at"])
        service = int(state == "completed" and time <= horizon)
        require(item["effective_service"] == service, "Effective service disagrees with raw outcome")
        completed += service
        owners[item["owner_id"]] += service
    return {"F": area, "completed": completed, "owner_service": dict(owners)}


def inspect_catalog(audit, source, *, calculations_only=False):
    from experiments.research.cp3_r1_diagnostics import (
        baseline_key,
        chosen,
        current_effect_class,
        pack_witness,
        scores,
    )

    inventory = read(audit, source["figure_inventory"])
    require(inventory.get("build") == audit.build and inventory.get("scope") == "complete-declared-candidate-figure-inventory",
            "Current candidate's complete publication inventory required")
    require({ref.get("repo_path") for ref in inventory["publications"]}
            == {"docs/research/CP3_BOTTLENECK_AND_DIRECTION.md", "web/src/components/PolicyValidation.tsx"},
            "Declared publication set differs")
    for ref in inventory["publications"]:
        content = raw(audit, ref)
        repo_path = ref.get("repo_path")
        if repo_path:
            require(content == (audit.root / repo_path).read_bytes(), "Publication differs from candidate file")
    required_figures = {"policy-report-overview", "policy-report-family-table", "policy-report-run-table",
                        "research-decision-counts", "research-area-decomposition", "research-micro-comparisons",
                        "research-owner-harm", "research-mcm-historical-citations"}
    figures = source.get("figures", [])
    require({f.get("id") for f in figures} == required_figures == set(inventory["figure_ids"])
            and len(figures) == len(required_figures), "Publication figure coverage omitted or duplicated")
    suite = source["historical_suite"]
    manifest, index = read(audit, suite["manifest"]), read(audit, suite["index"])
    require(index["manifest_sha256"] == suite["manifest"]["original_sha256"]
            == raw(audit, suite["seal"]).decode().strip(), "Original suite seal mismatch")
    require(manifest["build"] == index["build"] == suite["source_build"]
            and source.get("historical_results_reexecuted") is False, "Historical run identity changed")
    require(index["expected_runs"] == 125 and len(index["runs"]) == 125
            and manifest["partition"] == "validation", "Historical validation cohort incomplete")
    source_build = suite["source_build"]
    for ref in suite["policy_sources"]:
        require(raw(audit, ref) == subprocess.check_output(["git", "-C", str(audit.root), "show",
                    source_build["git_commit"] + ":" + ref["repo_path"]]), "Historical policy source changed")
    original_refs = {a["path"]: a["sha256"] for row in index["runs"] for a in row["artifacts"].values()}
    refs = suite["artifacts"]
    require(set(refs) == {a["path"] for r in index["runs"] for name, a in r["artifacts"].items()
                         if name in {"input", "summary", "run-metadata", "final-snapshot"}
                         or name == "decisions" and r["policy"] == "P"}, "Required source artifacts omitted")
    results, summaries = {}, {}
    counts, decomposition = Counter(), defaultdict(Counter)
    for row in index["runs"]:
        artifacts = row["artifacts"]

        def get(name):
            original = artifacts[name]["path"]
            require(refs[original]["original_sha256"] == original_refs[original], "Original registered artifact hash changed")
            return read(audit, refs[original])

        inputs, summary, metadata = get("input"), get("summary"), get("run-metadata")
        require(metadata["build"] == metadata["build_at_finish"] == source_build
                and metadata["manifest_sha256"] == index["manifest_sha256"]
                and metadata["input_sha256"] == artifacts["input"]["sha256"]
                and metadata["model"] == inputs["model"] == manifest["model"]
                and metadata["policy_artifact"] == manifest["policy_artifacts"][row["policy"]]
                and metadata["run_id"] == row["run_id"], "Run/input/model/policy binding differs")
        results[row["run_id"]] = outcome(inputs, get("final-snapshot"), summary)
        summaries[row["run_id"]] = summary
        if row["policy"] == "B2":
            target = decomposition[row["scenario_family"]]
            for w in summary["works"]:
                f = max(0., min(w["completed_at"] if w["status"] == "completed" else inputs["horizon"], inputs["horizon"]) - w["arrived_at"])
                target["total_F"] += f
                if w["status"] == "completed":
                    target["completed_fixed_10s_service_base"] += 10
                    target["completed_queueing_interruption_other_elapsed"] += f - 10
                else:
                    target[w["status"] + "_H_minus_arrival"] += f
        if row["policy"] == "P":
            for item in get("decisions"):
                plan, snapshot = item["plan"], item["snapshot"]
                action = chosen(plan)
                counts["plans"] += 1
                counts["candidate_budget_truncations"] += int("Candidate budget" in (plan["coverage"]["truncation_reason"] or ""))
                if action is None:
                    continue
                counts["selected"] += 1
                choices = [c for c in plan["candidates"] if c["feasibility"] == "legal" and c["demand_id"] == action["demand_id"]]
                values = [scores(c)["observed_opportunity_loss"] for c in choices]
                counts["admitted_multi_candidate"] += len(choices) > 1
                counts["admitted_multiple_current_effect_classes"] += len({current_effect_class(c, snapshot) for c in choices}) > 1
                counts["admitted_score_varies"] += max(values) > min(values)
                counts["changed_action"] += action["candidate_id"] != min(choices, key=baseline_key)["candidate_id"]
                counts["two_one_gpu_instead_of_selected_two_gpu_witnesses"] += pack_witness(action, item) is not None
    analysis = read(audit, suite["analysis"])
    require(suite["analysis"]["original_sha256"] == index["analysis"]["sha256"], "Published analysis changed")
    families = defaultdict(list)
    pairs = []
    for row in index["runs"]:
        if row["policy"] != "P":
            continue
        baseline = row["run_id"].rsplit("--", 1)[0] + "--B2"
        ratio = results[row["run_id"]]["F"] / results[baseline]["F"]
        families[row["scenario_family"]].append(ratio)
        pairs.append({"P": row["run_id"], "B2": baseline, "R": ratio})
    family_values = [{"scenario_family": name, "pair_count": len(values),
                      "mean_area_ratio": sum(values) / len(values), "worst_area_ratio": max(values)}
                     for name, values in sorted(families.items())]
    aggregate = analysis["comparisons"]["B2"]["aggregate"]
    require(math.isclose(aggregate["macro_area_ratio"], sum(f["mean_area_ratio"] for f in family_values) / len(family_values)),
            "Published macro ratio differs from all raw outcomes")
    published_families = [{key: row[key] for key in ("scenario_family", "pair_count", "mean_area_ratio", "worst_area_ratio")}
                          for row in aggregate["families"]]
    require(sorted(published_families, key=lambda f: f["scenario_family"]) == family_values,
            "Published family table differs from raw outcomes")
    require(analysis["target_met"] is False and analysis["coverage_complete"] is False,
            "Historical negative result or coverage gap was relabelled")
    diagnostics = read(audit, suite["diagnostics"])
    require(all(diagnostics["global"][key] == value for key, value in counts.items()), "Published diagnostic counts differ")
    require(all(diagnostics["B2_F_by_family"][family].get(key, 0) == value
                for family, values in decomposition.items() for key, value in values.items()), "F decomposition differs")
    micro_values = {}
    independent = read(audit, source["micro_independent_review"])
    for group in source["micro_groups"]:
        model = read(audit, group["manifest"])
        published = read(audit, group["analysis"])
        original = independent["groups"][group["id"]]
        require(group["manifest"]["original_sha256"] == original["manifest_sha256"] == published["manifest_sha256"],
                "Micro manifest seal differs")
        require(model["build"] == original["build"] == published["build_at_start"] == published["build_at_finish"],
                "Micro execution build differs")
        require(published["formal_validation"] is False and published["promotion_allowed"] is False,
                "Research witness cannot become formal performance evidence")
        for ref in group["sources"]:
            require(raw(audit, ref) == subprocess.check_output(["git", "-C", str(audit.root), "show",
                        model["build"]["git_commit"] + ":" + ref["repo_path"]]), "Micro implementation/protocol source changed")
        values = {}
        for record in original["runs"]:
            binding = group["runs"][record["run"]]
            require(binding["raw"]["original_sha256"] == record["raw_sha256"]
                    and binding["summary"]["original_sha256"] == record["summary_sha256"], "Micro raw evidence changed")
            trace, summary = read(audit, binding["raw"]), read(audit, binding["summary"])
            inputs = model["inputs"][record["scenario"]] if "inputs" in model else model["input"]
            actual = outcome(inputs, trace["snapshot"], summary)
            require(actual["F"] == record["area"] and actual["completed"] == record["completed"]
                    and actual["owner_service"] == record["owner_service"], "Micro raw outcomes disagree with independent calculation")
            values[record["run"]] = actual
        micro_values[group["id"]] = values
    owner_results = micro_values["cp3-owner-harm-20261114-metadata-corrected"]
    owner_pairs = list(owner_results.values())
    require(sorted(x["F"] for x in owner_pairs) == [100., 170.]
            and sorted(x["owner_service"]["probe-owner-0"] for x in owner_pairs) == [0, 1],
            "Owner-harm conclusion differs from raw outcomes")
    for ref in source["mcm_historical_citations"]:
        raw(audit, ref)
    calculations = {"policy-report-overview": {"R": aggregate["macro_area_ratio"], "runs": len(index["runs"]),
                    "target_met": False, "coverage_complete": False},
                    "policy-report-family-table": family_values,
                    "policy-report-run-table": [{k: r[k] for k in ("run_id", "scenario_family", "seed", "policy", "status")} for r in index["runs"]],
                    "research-decision-counts": dict(counts),
                    "research-area-decomposition": {k: dict(v) for k, v in decomposition.items()},
                    "research-micro-comparisons": micro_values,
                    "research-owner-harm": independent["owner_counterexample"],
                    "research-mcm-historical-citations": {"reexecuted": False, "source_files": len(source["mcm_historical_citations"])}}
    if calculations_only:
        return calculations
    for figure in figures:
        require(figure.get("historical") is True and len(figure.get("historical_disclosure", "")) > 20
                and figure.get("publication") and figure.get("locator"), "Explicit historical publication location required")
        expected = read(audit, figure["calculation"])
        require(expected.get("values") == calculations[figure["id"]], "Figure calculation artifact differs from raw recomputation")
        require(expected.get("recipe_source_sha256") == sha(Path(__file__).read_bytes()), "Calculation recipe is stale")
    return {"figures": len(figures), "historical_runs": 125, "micro_runs": 13,
            "R": aggregate["macro_area_ratio"], "status_boundary": "Traceability audit, not a new CP3 validation or CP4 performance result"}


def _graph_refs(source, inventory):
    refs = {}

    def walk(node):
        if isinstance(node, dict):
            if "path" in node and "sha256" in node:
                require(isinstance(node["path"], str), "Invalid graph reference")
                refs[node["path"]] = {"path": node["path"], "sha256": node["sha256"]}
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(source)
    walk(inventory)
    return refs


def inspect_integrity(audit, source):
    """Fast read path: validate the deep audit and every retained source digest.

    Expensive JSON decompression and raw-outcome recalculation is explicit CLI
    work. The read path never treats an old-source audit as current or skips
    source-byte hashes; it does not claim to have rerun those calculations.
    """
    deep = audit.source("figure_deep_audit.json", "cp4-figure-deep-audit-v1")
    source_path = audit.root / "evidence/cp4/figure_index_source.json"
    require(deep.get("status") == "PASS" and deep.get("source_sha256") == sha(source_path.read_bytes())
            and deep.get("recipe_sha256") == sha(Path(__file__).read_bytes()), "Deep audit is missing, stale or for another graph/recipe")
    inventory = read(audit, source["figure_inventory"])
    require(inventory.get("build") == audit.build and inventory.get("scope") == "complete-declared-candidate-figure-inventory",
            "Current candidate inventory required")
    refs = _graph_refs(source, inventory)
    refs["evidence/cp4/figure_index_source.json"] = {"path": "evidence/cp4/figure_index_source.json", "sha256": sha(source_path.read_bytes())}
    recorded = deep.get("evidence", [])
    require(len(recorded) == len(refs) and {r["path"]: r for r in recorded} == refs,
            "Deep audit omits graph artifacts or binds different bytes")
    for ref in refs.values():
        audit.artifact(ref)
    suite = source["historical_suite"]
    manifest, index = read(audit, suite["manifest"]), read(audit, suite["index"])
    require(index.get("manifest_sha256") == suite["manifest"]["original_sha256"]
            == raw(audit, suite["seal"]).decode().strip()
            and index.get("build") == manifest.get("build") == suite.get("source_build"),
            "Historical graph lost its original build or manifest seal")
    require(index.get("expected_runs") == 125 and len(index.get("runs", [])) == 125
            and len({r["run_id"] for r in index["runs"]}) == 125, "Historical run registry incomplete")
    expected_sources = {ref["path"]: ref["sha256"] for row in index["runs"] for name, ref in row["artifacts"].items()
                        if name in {"input", "summary", "run-metadata", "final-snapshot"}
                        or name == "decisions" and row["policy"] == "P"}
    require(set(suite["artifacts"]) == set(expected_sources)
            and all(ref["original_sha256"] == expected_sources[path] for path, ref in suite["artifacts"].items()),
            "Required raw source omitted or original hash changed")
    allowed = {"docs/research/CP3_BOTTLENECK_AND_DIRECTION.md", "web/src/components/PolicyValidation.tsx"}
    require({r.get("repo_path") for r in inventory["publications"]} == allowed, "Publication set differs")
    for ref in inventory["publications"]:
        require(raw(audit, ref) == (audit.root / ref["repo_path"]).read_bytes(), "Published candidate changed since deep audit")
    require(deep.get("actual", {}).get("figures") == len(source.get("figures", [])) == 8
            and deep["actual"].get("historical_runs") == 125 and deep["actual"].get("micro_runs") == 13,
            "Deep audit coverage is incomplete")
    return {**deep["actual"], "read_path": "Current deep-audit binding plus all source hashes; no on-read recomputation",
            "hashed_artifacts": len(refs)}


def deep_audit(root):
    from _delivery_review import Audit

    audit = Audit(root)
    source = audit.source("figure_index_source.json", "cp4-figure-index-v1")
    result = inspect_catalog(audit, source)
    refs = audit.finish()
    record = {"schema": "cp4-figure-deep-audit-v1", "build": audit.build,
              "observed_at": datetime.now(timezone.utc).isoformat(), "unresolved_issues": [], "status": "PASS",
              "source_sha256": sha((audit.root / "evidence/cp4/figure_index_source.json").read_bytes()),
              "recipe_sha256": sha(Path(__file__).read_bytes()), "actual": result, "evidence": refs,
              "scope": "Explicit read-only recomputation from original raw states; no experiment rerun or human attestation"}
    (audit.root / "evidence/cp4/figure_deep_audit.json").write_bytes(encoded(record))
    return record


def prepare(root, suite_path, probe_root, mcm_root):
    """Copy original evidence bytes losslessly; never execute a scenario."""
    from _delivery_review import Audit

    root, suite_path, probe_root, mcm_root = map(Path, (root, suite_path, probe_root, mcm_root))
    audit = Audit(root)
    destination = root / "evidence/cp4/figure-sources"
    destination.mkdir(parents=True, exist_ok=True)

    def save(label, content, *, repo_path=None):
        original_sha = sha(content)
        compressed = len(content) > 200_000
        path = destination / (label + (".gz" if compressed else ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        data = gzip.compress(content, compresslevel=1, mtime=0) if compressed else content
        if not path.exists() or sha(path.read_bytes()) != sha(data):
            path.write_bytes(data)
        ref = {"path": path.relative_to(root).as_posix(), "sha256": sha(data), "original_sha256": original_sha}
        if compressed:
            ref["encoding"] = "gzip"
        if repo_path:
            ref["repo_path"] = repo_path
        return ref

    def copy(label, path):
        return save(label, Path(path).read_bytes())

    def original_source(label, build, path):
        return save(label, subprocess.check_output(["git", "-C", str(root), "show", build["git_commit"] + ":" + path]), repo_path=path)

    index = json.loads((suite_path / "suite-index.json").read_bytes())
    manifest = json.loads((suite_path / "manifest.json").read_bytes())
    suite = {"source_build": manifest["build"], "manifest": copy("historical/manifest.json", suite_path / "manifest.json"),
             "index": copy("historical/suite-index.json", suite_path / "suite-index.json"),
             "seal": copy("historical/manifest.sha256", suite_path / "manifest.sha256"),
             "analysis": copy("historical/analysis.json", suite_path / "analysis.json"),
             "diagnostics": copy("historical/diagnostics.json", root / "docs/research/cp3-bottleneck-evidence/historical-diagnostics.json"),
             "policy_sources": [], "artifacts": {}}
    for path in ("src/campus_fabric/policies/baselines.py", "src/campus_fabric/policies/candidate.py"):
        suite["policy_sources"].append(original_source("historical/source/" + Path(path).name, manifest["build"], path))
    for row in index["runs"]:
        for name, ref in row["artifacts"].items():
            if name not in {"input", "summary", "run-metadata", "final-snapshot"} and not (name == "decisions" and row["policy"] == "P"):
                continue
            retained = copy("historical/" + ref["path"], suite_path / ref["path"])
            require(retained["original_sha256"] == ref["sha256"], "Source suite artifact changed before copying")
            suite["artifacts"][ref["path"]] = retained
    independent_path = root / "docs/research/cp3-bottleneck-evidence/independent-review-corrected.json"
    independent = json.loads(independent_path.read_bytes())
    micro_groups = []
    for name, old in independent["groups"].items():
        base = probe_root / name
        group = {"id": name, "manifest": copy("micro/" + name + "/manifest.json", base / "manifest.json"),
                 "analysis": copy("micro/" + name + "/analysis.json", base / "analysis.json"), "sources": [], "runs": {}}
        paths = ["experiments/research/cp3_direction_probe.py", "experiments/research/CP3_DIRECTION_PROBE.md",
                 "src/campus_fabric/policies/baselines.py", "src/campus_fabric/policies/candidate.py"]
        if "owner-harm" in name:
            paths += ["experiments/research/cp3_owner_harm_probe.py", "experiments/research/CP3_DIRECTION_FOLLOWUP.md"]
        for path in paths:
            group["sources"].append(original_source("micro/" + name + "/source/" + Path(path).name, old["build"], path))
        for run in old["runs"]:
            directory = base / run["policy"] if "owner-harm" in name else base / "runs" / run["run"]
            group["runs"][run["run"]] = {key: copy("micro/" + name + "/" + run["run"] + "/" + key + ".json", directory / (key + ".json")) for key in ("raw", "summary")}
        micro_groups.append(group)
    report = "docs/research/CP3_BOTTLENECK_AND_DIRECTION.md"
    component = "web/src/components/PolicyValidation.tsx"
    locations = {"policy-report-overview": (component, "facts: macro ratio, target and coverage"),
                 "policy-report-family-table": (component, "aggregate.families table"),
                 "policy-report-run-table": (component, "runs table"),
                 "research-decision-counts": (report, "section 1 decision-count table"),
                 "research-area-decomposition": (report, "section 2 F decomposition table"),
                 "research-micro-comparisons": (report, "section 4 six micro-input rows"),
                 "research-owner-harm": (report, "section 4 owner-harm and GPU-second bound"),
                 "research-mcm-historical-citations": (report, "section 3 archived MCM numbers, not rerun")}
    publications = [save("publications/" + Path(path).name, (root / path).read_bytes(), repo_path=path)
                    for path in (report, component)]
    inventory = {"schema": "cp4-figure-inventory-v1", "build": audit.build,
                 "scope": "complete-declared-candidate-figure-inventory", "figure_ids": list(locations), "publications": publications,
                 "boundary": "CP3 policy report and current CP3 research report; operational resource counters are live state, not experimental figures"}
    mcm_paths = ["FINAL_MCM_B_20261018/01_PAPER_HANDOFF/00_FINAL_DECISIONS.md",
                 "FINAL_MCM_B_20261018/04_REPRODUCIBLE_CORE/outputs/ablation_A10/results.json",
                 "ARCHIVE/legacy_workspace_20261018/research_delivery/六轮研究总结与理论深化报告.md"]
    source = {"schema": "cp4-figure-index-v1", "catalog_kind": "cp3-frozen-and-research-v1", "build": audit.build,
              "observed_at": datetime.now(timezone.utc).isoformat(), "unresolved_issues": [],
              "historical_results_reexecuted": False, "historical_suite": suite,
              "micro_independent_review": copy("micro/independent-review.json", independent_path),
              "micro_groups": micro_groups,
              "mcm_historical_citations": [copy("mcm/" + str(i) + "-" + Path(path).name, mcm_root / path) for i, path in enumerate(mcm_paths)],
              "figure_inventory": save("figure-inventory.json", encoded(inventory)),
              "figures": [{"id": key, "publication": path, "locator": locator, "historical": True,
                           "historical_disclosure": "Preserved original experiment build; CP4 candidate only audits and publishes these historical results, not a rerun."}
                          for key, (path, locator) in locations.items()]}
    calculations = inspect_catalog(audit, source, calculations_only=True)
    for figure in source["figures"]:
        figure["calculation"] = save("calculations/" + figure["id"] + ".json", encoded({"recipe_source_sha256": sha(Path(__file__).read_bytes()),
                    "recipe": "tests/acceptance/cp4/_figure_index.py:inspect_catalog", "values": calculations[figure["id"]]}))
    (root / "evidence/cp4/figure_index_source.json").write_bytes(encoded(source))
    # A second independent read checks the sealed graph and generated figures.
    # This remains explicit preparation, never the interactive page read path.
    deep_audit(root)
    return source
