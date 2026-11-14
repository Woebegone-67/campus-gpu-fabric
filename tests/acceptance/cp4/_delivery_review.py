"""Read-only CP4 delivery evidence checks; these do not authenticate a human.

The review establishes consistency of retained observations, not physical
operator identity. An explicit human attestation is still required and an
agent record can never satisfy a human requirement.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from check_checkpoint import current_build  # noqa: E402

TASKS = {
    "submit-and-follow": {"run", "continuity"},
    "contributor-recall": {"resources", "continuity"},
    "checkpoint-and-recover": {"continuity"},
    "explain-decision": {"decisions"},
    "events-report-and-download": {"events"},
    "administrator-acceptance": {"acceptance"},
}
KINDS = {"CP4-CLEAN-DEPLOY": "clean_deployment", "CP4-USER-TASKS": "ui_runtime",
         "CP4-EVIDENCE-INDEX": "artifact_audit", "CP4-VIDEO-REHEARSAL": "artifact_audit"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def timestamp(value):
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(when.tzinfo is not None and (when - datetime.now(timezone.utc)).total_seconds() <= 300,
            "Observation needs a timezone and cannot be in the future")
    return when


class Audit:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.build = current_build(self.root)
        self.watched = {}

    def artifact(self, ref):
        require(isinstance(ref, dict), "Artifact reference must be an object")
        relative = Path(ref.get("path", ""))
        base = self.root / "evidence/cp4"
        path = self.root / relative
        require(not relative.is_absolute() and ".." not in relative.parts
                and path.resolve().is_relative_to(base.resolve()), "Artifact outside CP4 evidence")
        require(not any(p.is_symlink() for p in (path, *path.parents)
                        if p == base or p.is_relative_to(base)), "Evidence symlinks are not accepted")
        require(path.is_file() and path.stat().st_size > 0, f"Missing or empty artifact: {relative}")
        require(path.stat().st_size <= 1024 * 1024 * 1024, "Evidence file exceeds 1 GiB limit")
        data = path.read_bytes()
        require(digest(data) == ref.get("sha256"), f"Artifact hash mismatch: {relative}")
        self.watched[relative.as_posix()] = digest(data)
        return data

    def json(self, ref):
        return json.loads(self.artifact(ref))

    def source(self, filename, schema):
        path = self.root / "evidence/cp4" / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing evidence/cp4/{filename}; no observation is inferred")
        ref = {"path": str(path.relative_to(self.root)), "sha256": digest(path.read_bytes())}
        record = self.json(ref)
        require(isinstance(record, dict) and record.get("schema") == schema,
                "Wrong CP4 observation schema; earlier checkpoints cannot substitute")
        require(record.get("build") == self.build, "Observation belongs to a different candidate build")
        timestamp(record.get("observed_at", ""))
        require(record.get("unresolved_issues") == [], "Known issues must be resolved, including intermittent failures")
        return record

    def human(self, value):
        require(isinstance(value, dict) and value.get("observer_kind") == "human",
                "An automated observer cannot attest as a human")
        identity = value.get("human_id", "")
        require(isinstance(identity, str) and len(identity.strip()) >= 2, "Human identity required")
        require(value.get("attested") is True, "The named human must explicitly attest their own observations")
        return identity.strip().casefold()

    def png(self, ref, width=1440, height=900):
        data = self.artifact(ref)
        require(len(data) > 32 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR",
                "Retain a real PNG screenshot")
        require(struct.unpack(">II", data[16:24]) == (width, height),
                f"Screenshot must be {width}x{height}; do not resize another viewport")
        offset, compressed, ended = 8, bytearray(), False
        while offset + 12 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            checksum = data[offset + 8 + length:offset + 12 + length]
            require(len(checksum) == 4 and struct.unpack(">I", checksum)[0]
                    == zlib.crc32(kind + payload), "Invalid PNG chunk checksum")
            if kind == b"IDAT":
                compressed.extend(payload)
            if kind == b"IEND":
                ended = True
                break
            offset += length + 12
        require(ended and compressed, "PNG is truncated or has no image data")
        depth, color, compression, filtering, interlace = data[24:29]
        require(depth == 8 and color in (2, 6) and (compression, filtering, interlace) == (0, 0, 0),
                "Use an unmodified 8-bit RGB/RGBA browser PNG")
        stride = width * (3 if color == 2 else 4) + 1
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(compressed, stride * height + 1)
        require(decompressor.eof and len(pixels) == stride * height
                and all(pixels[i] <= 4 for i in range(0, len(pixels), stride)), "Invalid PNG scanlines")

    def viewport_png(self, ref, viewport):
        require(isinstance(viewport, dict) and viewport.get("width") == 1440 and viewport.get("height") == 900,
                "Required CSS viewport is 1440x900")
        ratio = viewport.get("device_pixel_ratio", 1)
        require(isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
                and math.isfinite(ratio) and 0 < ratio <= 4,
                "Record the actual browser device pixel ratio")
        require(int(1440 * ratio) == 1440 * ratio and int(900 * ratio) == 900 * ratio, "Invalid device pixel ratio")
        self.png(ref, int(1440 * ratio), int(900 * ratio))

    def finish(self):
        require(current_build(self.root) == self.build, "Candidate build changed during review")
        refs = [{"path": path, "sha256": sha} for path, sha in sorted(self.watched.items())]
        for ref in refs:
            self.artifact(ref)
        return refs


def review_check(root, check, filename, schema, inspect):
    audit = Audit(root)
    result = {"schema_version": "1.0.0", "check": check, "status": "NOT_RUN",
              "build": audit.build, "evidence_kind": KINDS[check],
              "scope": "Retained CP4 evidence consistency review, not physical operator authentication",
              "observed_at": datetime.now(timezone.utc).isoformat(), "evidence": [],
              "unresolved_issues": []}
    try:
        source = audit.source(filename, schema)
        result["actual"] = inspect(audit, source)
        result["evidence"] = audit.finish()
        result["status"] = "PASS"
    except FileNotFoundError as exc:
        result["unresolved_issues"] = [str(exc)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError, zlib.error, struct.error) as exc:
        result.update(status="NOT_MET", unresolved_issues=[str(exc)])
    return result


def emit(result, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"check": result["check"], "status": result["status"], "output": str(output)}))
    return 0 if result["status"] == "PASS" else 1


def clean_deploy(audit, source):
    operator = audit.human(source.get("operator"))
    preparer = source.get("candidate_preparer_human_id", "").strip().casefold()
    require(preparer and operator != preparer, "Clean deployment must be performed by a different human")
    require(source.get("readme_sha256") == digest((audit.root / "README.md").read_bytes()),
            "Deployment instructions must be this candidate's README")
    checkout = source.get("clean_checkout", {})
    require(checkout.get("new_directory") is True and checkout.get("initial_git_status") == ""
            and checkout.get("build") == audit.build and checkout.get("path"),
            "A new clean checkout with matching build and initial git status is required")
    commands = source.get("commands", [])
    require(isinstance(commands, list) and len(commands) >= 4, "Retain dependency, build and startup commands")
    required = {"checkout", "backend-dependencies", "frontend-dependencies", "frontend-build", "startup"}
    require(required <= {c.get("stage") for c in commands}, "Required clean deployment stages missing")
    for command in commands:
        require(command.get("returncode") == 0 and command.get("argv"), "Clean deployment command failed")
        text = audit.artifact(command.get("log")).decode("utf-8")
        require(len(text.strip()) >= 8, "Retain actual command output rather than an empty success marker")
    health = audit.json(source.get("http_history"))
    require(health.get("build") == audit.build, "HTTP observation belongs to another build")
    rows = health.get("requests", [])
    require(any(r.get("method") == "GET" and r.get("path") == "/healthz"
                and r.get("status_code") == 200 and r.get("response", {}).get("control_plane_ready") is True for r in rows),
            "Actual healthy service response required")
    require(any(r.get("method") == "GET" and r.get("path") == "/v1/version"
                and r.get("status_code") == 200 and r.get("response", {}).get("api_version") == "2.0.0" for r in rows),
            "Actual version response required")
    require(any(r.get("method") == "POST" and r.get("path") == "/v1/allocations"
                and r.get("status_code") == 401 for r in rows), "Unauthenticated write must be rejected")
    audit.viewport_png(source.get("browser_screenshot"), source.get("viewport", {"width": 1440, "height": 900, "device_pixel_ratio": 1}))
    return {"different_human": operator, "commands": len(commands), "http_observations": len(rows),
            "scope": "Human-declared clean deployment, retained logs and actual HTTP/browser artifacts checked"}


def partial_success(trace, requests):
    retained = trace.get("retained_allocation_id")
    require(retained and trace.get("duplicate_allocation_created") is False,
            "Partial success needs the retained allocation identity")
    details = [r["response"] for r in requests if r.get("status_code") == 200
               and r.get("path", "").startswith("/x/v1/work-intents/")
               and isinstance(r.get("response"), dict)
               and retained in r["response"].get("intent", {}).get("allocation_ids", [])]
    require(len(details) >= 2 and len({d["intent"].get("intent_id") for d in details}) == 1
            and all(d["intent"].get("allocation_ids") == [retained] for d in details),
            "Retain before/after work details proving the same single lease")
    staged_rejection = any(
        any(s.get("operation") == "create_allocation_and_bind" and s.get("state") == "committed"
            for s in detail["intent"].get("stages", []))
        and any(s.get("operation") == "create_job_and_bind" and s.get("state") == "failed"
                and isinstance(s.get("receipt"), dict) and s["receipt"].get("outcome") == "rejected"
                and s["receipt"].get("error") for s in detail["intent"].get("stages", []))
        for detail in details)
    direct_rejection = (any(r.get("method") == "POST" and 200 <= r.get("status_code", 0) < 300 for r in requests)
                        and any(r.get("method") == "POST" and r.get("status_code", 0) >= 400 for r in requests))
    require(staged_rejection or direct_rejection,
            "Partial success needs the actual later rejection, including persisted asynchronous stage receipts")


def user_tasks(audit, source):
    operators = source.get("operators", [])
    require(isinstance(operators, list) and len(operators) == 2, "Two cross-operating humans required")
    require({p.get("role") for p in operators} == {"A", "B"}, "Both A and B must participate")
    identities = [audit.human(p) for p in operators]
    require(len(set(identities)) == 2, "Two distinct humans required")
    observed = 0
    for person in operators:
        tasks = person.get("tasks", [])
        require(len(tasks) == len(TASKS) and {t.get("id") for t in tasks} == set(TASKS),
                "Each human must cross-operate all six tasks exactly once")
        conditions = set()
        for task in tasks:
            require(task.get("status") == "PASS" and len(task.get("actual", "").strip()) > 20,
                    "Concrete successful task observation required")
            require(TASKS[task["id"]] <= set(task.get("workspaces", [])), "Task workspace coverage missing")
            audit.viewport_png(task.get("screenshot"), task.get("viewport"))
            trace = audit.json(task.get("history"))
            require(trace.get("schema") == "cp4-ui-history-v1" and trace.get("build") == audit.build
                    and trace.get("task_id") == task["id"]
                    and trace.get("human_id", "").strip().casefold() == person["human_id"].strip().casefold(),
                    "UI history must bind the task, human and candidate build")
            actions = trace.get("actions", [])
            require(isinstance(actions, list) and len(actions) >= 2
                    and [a.get("sequence") for a in actions] == list(range(1, len(actions) + 1)),
                    "Retain ordered actual UI actions")
            for action in actions:
                require(action.get("operation") and action.get("observed"), "Action and observed result required")
            requests = trace.get("requests", [])
            require(isinstance(requests, list) and requests
                    and all(r.get("method") and r.get("path", "").startswith("/")
                            and isinstance(r.get("status_code"), int) and "response" in r for r in requests),
                    "Retain actual service responses for the UI task")
            successful = [r for r in requests if 200 <= r["status_code"] < 300]
            task_id = task["id"]
            if task_id == "submit-and-follow":
                require(any(r["method"] == "POST" and r["path"] == "/x/v1/work-intents" for r in successful)
                        and any(r["method"] == "GET" and r["path"].startswith("/x/v1/work-intents/")
                                and r["response"].get("intent", {}).get("intent_id") for r in successful),
                        "Submit/follow task needs actual creation and retained work detail")
            elif task_id == "contributor-recall":
                require(any(r["method"] == "POST" and r["path"] == "/x/v1/contributions/recall"
                            for r in successful), "Contributor task needs a real authorized recall")
                require(any(r["status_code"] in (403, 404) for r in requests),
                        "Contributor task must retain a rejected other-owner operation")
            elif task_id == "checkpoint-and-recover":
                facts = [a.get("checkpoint") or {} for r in successful if isinstance(r["response"], dict)
                         for a in r["response"].get("allocations", [])]
                require(any(f.get("verification_status") == "verified"
                            and f.get("continuation_status") == "applied" and f.get("resumed_attempt_id") for f in facts),
                        "Recovery task requires verified and applied checkpoint evidence, not logical ready")
            elif task_id == "explain-decision":
                require(any(r["path"].startswith("/x/v1/decisions/")
                            and r["response"].get("commit_status") == "committed"
                            and r["response"].get("actual_events") for r in successful),
                        "Decision task needs actual committed events for the displayed plan")
            elif task_id == "events-report-and-download":
                require(any(r["path"].startswith("/v1/events") for r in successful)
                        and any(r["path"].startswith("/x/v1/policy-validation") for r in successful),
                        "Events and policy report need actual service responses")
                downloads = trace.get("downloads", [])
                require(downloads, "Browser download must be retained, not inferred from a link")
                for download in downloads:
                    require(audit.artifact(download.get("file")) == audit.artifact(download.get("server_artifact")),
                            "Browser download differs from the actual server artifact")
            elif task_id == "administrator-acceptance":
                require(any(r["path"] == "/x/v1/acceptance" for r in successful),
                        "Acceptance task needs actual current evidence status")
                overviews = [r for r in requests if r["path"] == "/x/v1/admin/overview"]
                require(len(overviews) >= 10 and all(r["status_code"] == 200 for r in overviews),
                        "Ten consecutive administrator overviews must succeed; no hidden 409 allowed")
            states = set(task.get("conditions", []))
            if "error" in states:
                require(any(r["status_code"] >= 400 for r in requests), "Error-state claim needs an actual rejection")
            if "empty" in states:
                require(any(r["status_code"] == 200 and (r["response"] == []
                            or isinstance(r["response"], dict) and any(r["response"].get(k) == []
                                for k in ("items", "intents", "events", "allocations", "jobs"))) for r in requests),
                        "Empty-state claim needs an actual empty service response")
            if "partial-success" in states:
                partial_success(trace, requests)
            conditions |= states
            observed += len(actions)
        require({"error", "empty", "partial-success"} <= conditions,
                "Each operator must observe error, empty and partial-success states")
    return {"human_operators": identities, "tasks": 12, "recorded_actions": observed,
            "viewport": "1440x900", "scope": "Cross-operation attestations with raw observations; no agent substitution"}


def video_rehearsal(audit, source):
    audit.human(source.get("operator"))
    video = source.get("video")
    audit.artifact(video)
    path = audit.root / video["path"]
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], text=True))
    streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    require(streams and float(metadata.get("format", {}).get("duration", 0)) >= 30,
            "Retain an actual video rehearsal of at least 30 seconds, not a still image")
    scene = audit.json(source.get("scene"))
    require(scene.get("schema") == "cp4-rehearsal-scene-v1" and scene.get("build") == audit.build,
            "Scene and candidate must have identical build identity")
    deployment = audit.json(source.get("deployment_observation"))
    require(deployment.get("build") == audit.build, "Video and deployment builds differ")
    require(scene.get("model") == deployment.get("model") == source.get("model"),
            "Video, scene and deployment models differ")
    require(scene.get("model") in {"logical_simulation", "cpu_reference"}, "Explicit execution mode required")
    require(scene.get("scene_id") and scene.get("scene_id") == source.get("scene_id"), "Scene identity mismatch")
    require(scene.get("recording_method") == "continuous_screen_recording"
            and scene.get("edited_for_outcome") is False, "Rehearsal needs continuous actual recording")
    steps = scene.get("steps", [])
    require(len(steps) >= 3 and all(s.get("observed") and s.get("at_seconds") is not None for s in steps),
            "Retain time-coded actual rehearsal observations")
    duration = float(metadata["format"]["duration"])
    times = [float(s["at_seconds"]) for s in steps]
    require(times == sorted(times) and min(times) >= 0 and max(times) <= duration,
            "Rehearsal timecodes do not lie within actual media")
    require(source.get("credentials_visible") is False, "Recording must not expose credentials")
    return {"duration_seconds": duration, "scene_id": scene["scene_id"], "model": scene["model"],
            "scope": "Same-build candidate rehearsal media; not CP5 final video or external submission"}


def finite(value):
    require(isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value),
            "Finite numeric value required")
    return value


def evidence_index(audit, source):
    if source.get("catalog_kind") == "cp3-frozen-and-research-v1":
        from _figure_index import inspect_integrity
        return inspect_integrity(audit, source)
    inventory = audit.json(source.get("figure_inventory"))
    require(inventory.get("schema") == "cp4-figure-inventory-v1" and inventory.get("build") == audit.build,
            "Current-candidate figure inventory required")
    require(inventory.get("scope") == "complete-declared-candidate-figure-inventory",
            "Declare complete figure scope, not a sample of convenient figures")
    figures = source.get("figures", [])
    ids = [f.get("figure_id") for f in figures]
    require(figures and len(ids) == len(set(ids)) and set(ids) == set(inventory.get("figure_ids", [])),
            "Every declared figure must resolve exactly once")
    publications = inventory.get("publications", [])
    require(publications, "Retain actual report/chart publications")
    for ref in publications:
        audit.artifact(ref)
    counts = 0
    for figure in figures:
        require(figure.get("publication") in [p["path"] for p in publications]
                and figure.get("locator"), "Each figure needs its actual publication location")
        calculation = audit.json(figure.get("calculation"))
        require(calculation.get("schema") == "cp4-area-calculation-v1", "Unsupported calculation recipe")
        inputs = audit.json(figure.get("input"))
        model = audit.json(figure.get("model"))
        metadata = audit.json(figure.get("run"))
        snapshot = audit.json(figure.get("final_snapshot"))
        policy = audit.json(figure.get("policy"))
        manifest = audit.json(figure.get("manifest"))
        suite_index = audit.json(figure.get("suite_index"))
        source_build = figure.get("source_build")
        require(isinstance(source_build, dict) and source_build.get("git_commit")
                and source_build.get("source_sha256") and source_build.get("source_file_count"),
                "Original experiment build must be retained")
        require(metadata.get("build") == metadata.get("build_at_finish") == source_build
                and metadata.get("input_sha256") == figure["input"]["sha256"]
                and metadata.get("run_id") == figure.get("run_id"),
                "Run metadata does not bind input, policy and original experiment build")
        require(manifest.get("build") == source_build and suite_index.get("build") == source_build
                and metadata.get("manifest_sha256") == figure["manifest"]["sha256"]
                and suite_index.get("manifest_sha256") == figure["manifest"]["sha256"],
                "Original manifest and suite index must bind the experiment build")
        require(inputs.get("model") == model == metadata.get("model") == manifest.get("model")
                and policy == metadata.get("policy_artifact")
                and policy == manifest.get("policy_artifacts", {}).get(figure.get("policy_id")),
                "Model/policy differs from the original run and manifest")
        rows = [r for r in suite_index.get("runs", []) if r.get("run_id") == figure.get("run_id")]
        require(len(rows) == 1 and rows[0].get("policy") == figure.get("policy_id")
                and rows[0].get("status") == "complete", "Figure run omitted or duplicated in original suite")
        for key, label in (("input", "input"), ("run", "run-metadata"), ("final_snapshot", "final-snapshot")):
            require(figure[key]["sha256"] == rows[0].get("artifacts", {}).get(label, {}).get("sha256"),
                    "Figure source differs from original suite artifact hashes")
        sources = figure.get("policy_sources", [])
        require(sources, "Retain original policy implementation source, not just its name")
        for ref in sources:
            content = audit.artifact(ref)
            repo_path = ref.get("repo_path", "")
            require(repo_path.startswith("src/campus_fabric/policies/") and ".." not in Path(repo_path).parts,
                    "Policy source path must be inside the policy module")
            original = subprocess.check_output(["git", "-C", str(audit.root), "show",
                                                source_build["git_commit"] + ":" + repo_path])
            require(content == original, "Retained policy code differs from original experiment commit")
        require(calculation.get("run_id") == metadata["run_id"]
                and calculation.get("input_sha256") == figure["input"]["sha256"]
                and calculation.get("final_snapshot_sha256") == figure["final_snapshot"]["sha256"],
                "Calculation must bind the exact input and final state")
        if source_build != audit.build:
            require(figure.get("historical") is True and len(figure.get("historical_disclosure", "")) > 20,
                    "Old results must be explicitly historical, never relabelled as current-build execution")
        horizon = finite(inputs["horizon"])
        works = inputs["works"]
        require(works and len({w["intent_id"] for w in works}) == len(works), "Input work cohort incomplete or duplicated")
        intents = {w["intent_id"]: w for w in snapshot["intents"]}
        area, completed = 0., 0
        cohort = []
        for work in works:
            arrival = finite(work["arrived_at"])
            if arrival > horizon:
                continue
            intent = intents.get(work["intent_id"], {})
            terminal = intent.get("result") or {}
            finish = finite(terminal.get("recorded_at", horizon))
            done = intent.get("state") == "completed" and finish <= horizon
            area += max(0., min(finish if done else horizon, horizon) - arrival)
            completed += int(done)
            cohort.append(work["intent_id"])
        require(calculation.get("cohort") == cohort, "Calculation drops, duplicates or reorders input works")
        require(math.isclose(finite(calculation["F"]), area, abs_tol=1e-9, rel_tol=1e-12)
                and calculation.get("completed") == completed, "Figure calculation disagrees with raw final outcomes")
        require(figure.get("display_values") == {"F": calculation["F"], "completed": completed},
                "Displayed figure values differ from recomputation")
        counts += len(cohort)
    return {"figures": len(figures), "recomputed_work_rows": counts,
            "scope": "All declared candidate figures trace to exact artifacts; historical builds remain historical"}
