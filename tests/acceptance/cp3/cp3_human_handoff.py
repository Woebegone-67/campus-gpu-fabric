"""Review this CP3 build's A/B human observations and artifact integrity.

The reviewer does not operate the UI or identify the physical operator. Named
human attestations remain distinct from automated DOM/HTTP acceptance. CP2
records cannot satisfy this round's CP3 journeys, even if copied or renamed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from check_checkpoint import _artifact_errors, current_build, sha256_file  # noqa: E402

WORKSPACES = {"run", "resources", "continuity", "decisions", "events", "acceptance"}
JOURNEYS = {
    "cp3-terminal-settlement",
    "cp3-checkpoint-cpu-artifact",
    "cp3-decision-owner-isolation",
    "cp3-sealed-experiment-isolation",
}


def require(value, message):
    if not value:
        raise ValueError(message)


def review(root):
    root = Path(root).resolve()
    build = current_build(root)
    path = root / "evidence/cp3/b/human_handoff.json"
    if not path.is_file():
        raise FileNotFoundError("Missing current-round evidence/cp3/b/human_handoff.json; CP2 or automated QA cannot sign CP3")
    content = path.read_bytes()
    record = json.loads(content)
    require(isinstance(record, dict), "CP3 handoff must be an object")
    require(record.get("schema_version") == "1.0.0" and record.get("checkpoint") == "CP3",
            "Explicit CP3 schema and checkpoint required; previous-round evidence is insufficient")
    require(record.get("build") == build, "CP3 human handoff belongs to a different build")
    when = datetime.fromisoformat(record.get("observed_at", "").replace("Z", "+00:00"))
    require(when.tzinfo is not None and (when - datetime.now(timezone.utc)).total_seconds() <= 300,
            "CP3 observation needs a timezone-qualified timestamp, not a future date")
    people = record.get("operators")
    require(isinstance(people, list) and len(people) == 2 and all(isinstance(p, dict) for p in people),
            "Two distinct A/B human attestations required")
    require({p.get("role") for p in people} == {"A", "B"}, "Both A and B must be identified")
    identities = [p.get("human_id") for p in people]
    require(all(isinstance(value, str) and value.strip() for value in identities),
            "Each operator needs a human identity")
    require(len({value.strip().casefold() for value in identities}) == 2,
            "Two distinct human identities required")
    watched = []
    for person in people:
        require(person.get("observer_kind") == "human", "An automated observer cannot sign human acceptance")
        require(isinstance(person.get("workspaces"), list) and set(person["workspaces"]) == WORKSPACES,
                "Each operator must observe all six workspaces")
        require(isinstance(person.get("roles"), list)
                and set(person["roles"]) == {"researcher", "contributor", "administrator"},
                "Each operator must exercise the three authorized roles")
        checks = person.get("checks")
        require(isinstance(checks, list) and all(isinstance(c, dict) for c in checks), "Invalid CP3 journey checks")
        require(len(checks) == len(JOURNEYS) and {c.get("id") for c in checks} == JOURNEYS,
                "Each human must attest all four CP3 journeys exactly once")
        for check in checks:
            require(check.get("status") == "PASS", "A required CP3 human journey has not passed")
            require(isinstance(check.get("actual"), str) and len(check["actual"].strip()) > 20,
                    "Concrete CP3 observed outcome required")
            refs = check.get("evidence")
            require(isinstance(refs, list) and refs, "Actual CP3 human observation artifacts required")
            for entry in refs:
                problems = _artifact_errors(root, "CP3", entry, path)
                require(not problems, "; ".join(problems))
                watched.append(entry)
    require(current_build(root) == build, "Build changed during CP3 human evidence review")
    require(path.read_bytes() == content, "Human handoff changed during review")
    for entry in watched:
        problems = _artifact_errors(root, "CP3", entry, path)
        require(not problems, "; ".join(problems))
    return build


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = {"schema_version": "1.0.0", "check": "CP3-HUMAN-HANDOFF",
              "scope": "JOINT_CP3_HUMAN_EVIDENCE_REVIEW", "status": "NOT_RUN",
              "build": current_build(args.root), "observed_at": datetime.now(timezone.utc).isoformat()}
    try:
        reviewed = review(args.root)
        result.update(status="PASS", build=reviewed,
                      actual="Current-build CP3 A/B journey attestations and their retained artifacts verified")
        source = args.root / "evidence/cp3/b/human_handoff.json"
        result["evidence"] = [{"path": "evidence/cp3/b/human_handoff.json", "sha256": sha256_file(source)}]
    except FileNotFoundError as exc:
        result["reason"] = str(exc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result.update(status="NOT_MET", reason=str(exc))
    output = args.output or args.root / "evidence/cp3/human_review.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
