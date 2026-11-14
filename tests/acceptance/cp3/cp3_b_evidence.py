"""Small evidence writer shared by B's registered CP3 validators."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from check_checkpoint import current_build  # noqa: E402


def retain(directory, name, value):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".json")
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    label = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    return {"path": label, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def receipt(check, kind, scope):
    return {"schema_version": "1.0.0", "check": check, "evidence_kind": kind,
            "status": "NOT_RUN", "build": current_build(ROOT),
            "observed_at": datetime.now(timezone.utc).isoformat(), "scope": scope,
            "checks": [], "unresolved_issues": []}


def finish(output, result):
    result["build_at_finish"] = current_build(ROOT)
    if result["build_at_finish"] != result["build"]:
        result["unresolved_issues"].append("Source changed during execution; rerun the stable build.")
        if result["status"] == "PASS":
            result["status"] = "NOT_MET"
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"check": result["check"], "status": result["status"], "receipt": str(output)}))
    return 0 if result["status"] == "PASS" else 1
