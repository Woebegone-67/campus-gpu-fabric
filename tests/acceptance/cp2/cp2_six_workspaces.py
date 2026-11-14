"""Review actual current-build UI evidence and the distinct A/B human handoff.

This validator checks attestations and artifact integrity, not who physically
clicked. Automated role switching is accepted as agent QA, never as two humans.
It cannot launch a browser or synthesize UI observations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from check_checkpoint import current_build  # noqa: E402

WORKSPACES = {"run", "resources", "continuity", "decisions", "events", "acceptance"}
JOURNEYS = {
    "three-role-recall-resume", "checkpoint-truth-and-waiting", "contributor-isolation",
    "real-cpu-artifact", "mode-owner-and-experiment-isolation",
}


def require(value, message):
    if not value:
        raise ValueError(message)


def read_record(root, name, build, watched=None):
    path = root / "evidence/cp2/b" / name
    if not path.is_file():
        raise FileNotFoundError(f"NOT_RUN: missing evidence/cp2/b/{name}")
    content = path.read_bytes()
    record = json.loads(content)
    if watched is not None:
        watched.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(content).hexdigest()})
    require(isinstance(record, dict), "Evidence record must be an object")
    require(record.get("build") == build, "Evidence is not for the current build")
    when = datetime.fromisoformat(record.get("observed_at", "").replace("Z", "+00:00"))
    require(when.tzinfo is not None and (when - datetime.now(timezone.utc)).total_seconds() <= 300,
            "Evidence timestamp needs a timezone and cannot be in the future")
    return record


def artifact(root, entry):
    require(isinstance(entry, dict), "Artifact reference must be an object")
    relative = Path(entry.get("path", ""))
    path = (root / relative).resolve()
    require(not relative.is_absolute() and ".." not in relative.parts
            and path.is_relative_to((root / "evidence/cp2").resolve()), "Artifact outside CP2 evidence")
    require(path.is_file() and path.stat().st_size > 0, "Missing or empty evidence artifact")
    require(hashlib.sha256(path.read_bytes()).hexdigest() == entry.get("sha256"), "Evidence artifact changed")


def observation(root, check, watched=None):
    require(check.get("status") == "PASS", "Required UI observation has not passed")
    require(isinstance(check.get("actual"), str) and len(check["actual"].strip()) > 20,
            "Concrete observed outcome required")
    require(isinstance(check.get("evidence"), list) and check["evidence"], "Actual artifacts required")
    for entry in check["evidence"]:
        artifact(root, entry)
        if watched is not None:
            watched.append(dict(entry))


def review(root, *, require_humans=True):
    root = Path(root).resolve()
    build = current_build(root)
    watched = []
    ui = read_record(root, "ui_receipt.json", build, watched)
    require(ui.get("observer_kind") in {"agent", "human"} and ui.get("observer_ids"), "Identify actual UI observer")
    require(set(ui.get("workspaces", [])) == WORKSPACES, "All six workspaces must be observed")
    require(set(ui.get("roles", [])) == {"researcher", "contributor", "administrator"}, "All three roles required")
    checks = ui.get("checks", [])
    require(isinstance(checks, list) and all(isinstance(c, dict) for c in checks), "Invalid UI checks")
    require(len(checks) == len(JOURNEYS) and {c.get("id") for c in checks} == JOURNEYS,
            "Dynamic journeys incomplete or duplicated")
    for check in checks:
        observation(root, check, watched)
    if require_humans:
        humans = read_record(root, "human_handoff.json", build, watched)
        people = humans.get("operators", [])
        require(isinstance(people, list) and len(people) == 2 and all(isinstance(p, dict) for p in people),
                "Two distinct A/B human attestations required")
        require({p.get("role") for p in people} == {"A", "B"}, "A and B must both be identified")
        identities = [p.get("human_id", "").strip() for p in people]
        require(all(identities) and len(set(identities)) == 2, "Two distinct human identities required")
        for person in people:
            require(person.get("observer_kind") == "human", "An agent cannot count as a human operator")
            require(set(person.get("workspaces", [])) == WORKSPACES, "Each operator must review all six workspaces")
            observation(root, person, watched)
    require(current_build(root) == build, "Build changed during UI evidence review")
    # Recheck the exact receipt bytes and all artifacts after the review. This
    # detects concurrent edits; it is not an atomic filesystem snapshot.
    for entry in watched:
        artifact(root, entry)
    return build


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp2/six_workspace_receipt.json")
    parser.add_argument("--agent-qa-only", action="store_true", help="Review agent UI artifacts only; cannot grant joint CP2")
    args = parser.parse_args(argv)
    scope = "AGENT_UI_EVIDENCE_ONLY" if args.agent_qa_only else "JOINT_CP2_UI_EVIDENCE_REVIEW"
    result = {"check": "CP2-SIX-WORKSPACES", "scope": scope,
              "status": "NOT_RUN", "build": current_build(args.root),
              "observed_at": datetime.now(timezone.utc).isoformat()}
    try:
        review(args.root, require_humans=not args.agent_qa_only)
        result.update(status="PASS", actual="Current-build UI observations and their actual artifacts verified; human handoff checked when required")
    except FileNotFoundError as exc:
        result["reason"] = str(exc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result.update(status="NOT_MET", reason=str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
