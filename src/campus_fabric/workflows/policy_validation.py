"""Read-only presentation of frozen CP3 experiments, separate from CP2 samples.

All paths originate in a locally sealed suite. HTTP callers select opaque IDs,
never filesystem paths. Validation errors fail closed and do not reuse old data.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

ARTIFACTS = {"input", "summary", "events", "decisions", "receipts", "final-snapshot",
             "run-metadata", "commands", "resource-samples", "external-injections",
             "final-decision-snapshot"}


class SealedPolicyValidation:
    def __init__(self, root: Path, suite: Path | None = None, *,
                 historical_manifest_sha256: str | None = None,
                 historical_index_sha256: str | None = None):
        self.root = root.resolve()
        self.suite = suite or self.root / "evidence/cp3/policy-suite"
        if historical_manifest_sha256 is not None or historical_index_sha256 is not None:
            if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (historical_manifest_sha256, historical_index_sha256)):
                raise ValueError("Historical report requires explicit manifest and index SHA256s")
        self.historical_manifest_sha256 = historical_manifest_sha256
        self.historical_index_sha256 = historical_index_sha256

    def _load(self):
        # The development host already depends on checkout-local configs and
        # evidence. Experiments remain an offline module, not an HTTP executor.
        from experiments.cp3_suite import load_suite_index

        # The pinned index binds the original analysis and every artifact digest.
        # Recheck around parsing so replacing the index cannot re-seal a report.
        index_path = self.suite / "suite-index.json"
        if self.historical_index_sha256 is not None:
            if (index_path.is_symlink() or index_path.stat().st_size > 4 * 1024 * 1024
                    or sha256(index_path.read_bytes()).hexdigest() != self.historical_index_sha256):
                raise ValueError("Historical report index does not match its configured SHA256")
        index = load_suite_index(
            self.suite, root=None if self.historical_manifest_sha256 else self.root)
        if (self.historical_manifest_sha256 is not None
                and index["manifest_sha256"] != self.historical_manifest_sha256):
            raise ValueError("Historical report does not match its configured manifest SHA256")
        if (self.historical_index_sha256 is not None
                and sha256(index_path.read_bytes()).hexdigest() != self.historical_index_sha256):
            raise ValueError("Historical report index changed while reading")
        return index

    def _read(self, entry):
        from experiments.cp3_suite import read_artifact_bytes

        return read_artifact_bytes(self.suite, entry["path"], entry["sha256"])

    def snapshot(self) -> dict:
        if not self.suite.exists():
            if self.historical_manifest_sha256:
                raise ValueError("Configured historical report is absent")
            return {"status": "NOT_RUN", "runs": [], "analysis": None,
                    "detail": "CP3 validation has not been frozen or executed on this checkout."}
        index = self._load()
        analysis_entry = index.get("analysis")
        analysis = json.loads(self._read(analysis_entry)) if analysis_entry else None
        if analysis and (analysis.get("build") != index["build"]
                         or analysis.get("manifest_sha256") != index["manifest_sha256"]):
            raise ValueError("Analysis identity does not match its sealed inputs")
        # Recheck the actual bytes, not just the report written during analysis.
        # A damaged run must not leave a previously green completeness claim.
        for row in index["runs"]:
            refs = row.get("artifacts", {})
            if analysis and analysis.get("evidence_complete") and (
                    row["status"] != "complete" or set(refs) != ARTIFACTS):
                raise ValueError("Completeness claim disagrees with registered runs")
            if self.historical_manifest_sha256 is None:
                for entry in refs.values():
                    self._read(entry)
        # A target observation is not a joint acceptance verdict. Coverage gaps
        # remain visible even if this model happens to beat the numerical target.
        from experiments.cp3_suite import _gate_module

        historical = self.historical_manifest_sha256 is not None
        return {
            "status": "ANALYZED" if analysis else "NOT_RUN",
            "build": index["build"], "manifest_sha256": index["manifest_sha256"],
            "historical_index_sha256": self.historical_index_sha256,
            "historical": historical,
            "serving_build": (_gate_module(self.root).current_build(self.root)
                              if historical else index["build"]),
            "current_build_acceptance_eligible": not historical,
            "integrity_scope": ("manifest_index_analysis; artifacts_verified_on_download"
                                if historical else "all_registered_artifact_bytes"),
            "partition": index["partition"], "expected_runs": index["expected_runs"],
            "analysis": analysis,
            "runs": [{key: row[key] for key in
                      ("run_id", "scenario_family", "seed", "policy", "status")}
                     | {"artifact_ids": [f"{row['run_id']}:{name}" for name in
                                        row.get("artifacts", {}) if name in ARTIFACTS]}
                     for row in index["runs"]],
            "artifact_ids": ["manifest"] + (["analysis"] if analysis_entry else []),
            "joint_acceptance": "NOT_ESTABLISHED_BY_POLICY_REPORT",
        }

    def artifact(self, artifact_id: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", artifact_id):
            raise KeyError(artifact_id)
        index = self._load()
        if artifact_id == "manifest":
            return self._read({"path": "manifest.json", "sha256": index["manifest_sha256"]})
        if artifact_id == "analysis" and index.get("analysis"):
            return self._read(index["analysis"])
        if ":" in artifact_id:
            run_id, name = artifact_id.rsplit(":", 1)
            if name in ARTIFACTS:
                row = next((r for r in index["runs"] if r["run_id"] == run_id), None)
                if row and name in row.get("artifacts", {}):
                    return self._read(row["artifacts"][name])
        raise KeyError(artifact_id)
