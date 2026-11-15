"""Small, read-only report for the separately frozen reclaim experiment.

Only registered artifacts are served. The report does not run experiments or
write the live ledger, and historical placement results remain separate.
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / "evidence/reclaim-aware-20261115"
FILES = {"report": "report.json", "protocol": "protocol.json"}


def _read(path: Path) -> bytes:
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Invalid report artifact")
    return path.read_bytes()


def artifact(artifact_id: str) -> bytes:
    if artifact_id not in FILES:
        raise KeyError(artifact_id)
    manifest_bytes = _read(SUITE / "manifest.json")
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported report manifest")
    entry = manifest["artifacts"][artifact_id]
    if entry["path"] != FILES[artifact_id]:
        raise ValueError("Artifact path is not registered")
    content = _read(SUITE / FILES[artifact_id])
    if sha256(content).hexdigest() != entry["sha256"]:
        raise ValueError("Artifact digest mismatch")
    if _read(SUITE / "manifest.json") != manifest_bytes:
        raise ValueError("Manifest changed during read")
    return content


def snapshot() -> dict:
    if not SUITE.exists():
        return {"status": "NOT_RUN", "summary": [], "runs": []}
    content = artifact("report")
    report = json.loads(content)
    if report.get("schema_version") != 1:
        raise ValueError("Unsupported report schema")
    if report["protocol_sha256"] != sha256(artifact("protocol")).hexdigest():
        raise ValueError("Report and protocol do not match")
    planner = ROOT / "src/campus_fabric/policies/reclaim.py"
    report["planner_matches_current"] = (
        planner.is_file() and report["planner_sha256"] == sha256(planner.read_bytes()).hexdigest()
    )
    report["report_sha256"] = sha256(content).hexdigest()
    return {**report, "status": "ANALYZED", "artifact_ids": list(FILES)}
