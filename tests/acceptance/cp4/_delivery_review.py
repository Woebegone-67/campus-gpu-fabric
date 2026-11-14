"""Read-only CP4 delivery evidence checks; these do not authenticate a human.

The review establishes consistency of retained observations, not physical
operator identity. An explicit human attestation is still required and an
agent record can never satisfy a human requirement.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

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
    details = [(index, r["response"]) for index, r in enumerate(requests)
               if r.get("method") == "GET" and r.get("status_code") == 200
               and r.get("path", "").startswith("/x/v1/work-intents/")
               and isinstance(r.get("response"), dict)
               and retained in r["response"].get("intent", {}).get("allocation_ids", [])]
    require(len(details) >= 2 and len({d["intent"].get("intent_id") for _, d in details}) == 1
            and all(d["intent"].get("allocation_ids") == [retained] for _, d in details),
            "Retain before/after work details proving the same single lease")
    work = details[0][1]["intent"].get("intent_id")
    path = f"/x/v1/work-intents/{work}"
    require(work and all(requests[index].get("path") == path for index, _ in details),
            "Work detail path and retained work identity differ")

    def committed_allocation(receipt):
        return (isinstance(receipt, dict) and receipt.get("outcome") == "committed"
                and not receipt.get("error") and receipt.get("request_id")
                and {("allocation", retained), ("intent", work)} <= {
                    (obj.get("kind"), obj.get("entity_id")) for obj in receipt.get("objects", [])})

    def rejected_job(receipt):
        return (isinstance(receipt, dict) and receipt.get("outcome") == "rejected"
                and isinstance(receipt.get("error"), dict) and receipt["error"].get("code")
                and receipt.get("event_ids") == [])

    def allocated_stages(detail):
        return [(index, stage) for index, stage in enumerate(detail["intent"].get("stages", []))
                if stage.get("operation") == "allocate" and stage.get("state") == "committed"
                and committed_allocation(stage.get("receipt"))]

    # A worker rejection may be returned later inside a successful detail GET.
    # Its persisted ordered stage receipts must still prove this same work/lease.
    staged_rejection = any(
        before_index < after_index
        and before["intent"].get("job_ids") == after["intent"].get("job_ids") == []
        and any(first["receipt"] == allocation["receipt"] for _, first in allocated_stages(before)
                for allocation_index, allocation in allocated_stages(after)
                for job_index, job in enumerate(after["intent"].get("stages", []))
                if allocation_index < job_index and job.get("operation") == "create_job_and_bind"
                and job.get("state") == "failed" and rejected_job(job.get("receipt")))
        for before_index, before in details for after_index, after in details)
    direct_rejection = False
    for allocation_index, allocation in enumerate(requests):
        if not (allocation.get("method") == "POST" and allocation.get("path") == path + "/actions"
                and (allocation.get("request_body") or {}).get("action") == "allocate"
                and 200 <= allocation.get("status_code", 0) < 300
                and committed_allocation(allocation.get("response"))):
            continue
        for job_index, job in enumerate(requests):
            response = job.get("response") or {}
            receipt = response.get("receipt", response) if isinstance(response, dict) else None
            if not (job_index > allocation_index and job.get("method") == "POST"
                    and job.get("path") == path + "/actions"
                    and (job.get("request_body") or {}).get("action") == "create_job"
                    and job.get("status_code", 0) >= 200 and rejected_job(receipt)):
                continue
            direct_rejection |= any(
                allocation_index < before_index < job_index < after_index
                and before["intent"].get("job_ids") == after["intent"].get("job_ids") == []
                and any(stage["receipt"] == allocation["response"] for _, stage in allocated_stages(before))
                and any(stage["receipt"] == allocation["response"] for _, stage in allocated_stages(after))
                for before_index, before in details for after_index, after in details)
    require(staged_rejection or direct_rejection,
            "Partial success needs the actual later rejection, including persisted asynchronous stage receipts")


def contributor_recall(requests):
    """Bind the authorized UI action and borrower HTTP refusal to one known GPU."""
    endpoint = "/x/v1/contributions/recall"

    def actor(row):
        value = row.get("actor", {})
        return value if isinstance(value, dict) else {}

    def gpu_ids(row):
        body = row.get("request_body", {})
        if not isinstance(body, dict):
            return set()
        ids = body.get("gpu_ids")
        if (not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x for x in ids)
                or type(body.get("notice_seconds")) is not int or not 1 <= body["notice_seconds"] <= 300
                or type(body.get("duration_seconds")) is not int or not 1 <= body["duration_seconds"] <= 3600):
            return set()
        return set(ids)

    for own in requests:
        owner = actor(own)
        receipt = own.get("response", {})
        if not (own.get("method") == "POST" and own.get("path") == endpoint
                and own.get("status_code") == 201 and own.get("origin") == "browser"
                and owner.get("role") == "contributor" and owner.get("owner_id")
                and owner.get("actor_id") and isinstance(receipt, dict)
                and receipt.get("outcome") == "committed" and not receipt.get("error")):
            continue
        selected = gpu_ids(own)
        owned = set()
        for row in requests:
            if (row.get("method") == "GET" and urlsplit(row.get("path", "")).path == "/x/v1/contributions"
                    and row.get("status_code") == 200 and actor(row) == owner):
                for node in row.get("response", {}).get("items", []):
                    if node.get("contributor_owner_id") == owner["owner_id"]:
                        owned.update(d.get("physical_gpu_id", d.get("device_id")) for d in node.get("devices", []))
        committed = {x.get("entity_id") for x in receipt.get("objects", []) if x.get("kind") == "gpu"}
        if not (selected and selected <= owned and committed == selected):
            continue
        for denied in requests:
            borrower = actor(denied)
            body = denied.get("response", {})
            if not (denied.get("method") == "POST" and denied.get("path") == endpoint
                    and denied.get("status_code") == 403 and gpu_ids(denied) == selected
                    and borrower.get("actor_id") and borrower.get("owner_id")
                    and borrower.get("owner_id") != owner["owner_id"]
                    and borrower.get("role") in {"researcher", "contributor"}
                    and isinstance(body, dict)):
                continue
            authenticated = any(
                row.get("method") == "GET" and urlsplit(row.get("path", "")).path == "/x/v1/work-intents"
                and row.get("status_code") == 200 and actor(row) == borrower
                and row.get("response", {}).get("scope") == "owner"
                and row["response"].get("owner_id") == borrower["owner_id"]
                and isinstance(row["response"].get("items"), list) for row in requests)
            role_refusal = (borrower["role"] == "researcher"
                            and body.get("detail") == "Contributor authority is required")
            rejection = body.get("receipt", {})
            owner_refusal = (borrower["role"] == "contributor" and body.get("error") == "forbidden"
                             and rejection.get("outcome") == "rejected"
                             and rejection.get("error", {}).get("code") == "forbidden"
                             and rejection.get("error", {}).get("detail") == "Contributor may recall only owned GPUs"
                             and rejection.get("event_ids") == [])
            if authenticated and (role_refusal or owner_refusal):
                return
    require(False, "Recall needs a contributor browser success and an authenticated other-owner refusal for the same known GPU and recall endpoint")


def user_task_evidence(audit, task_id, trace):
    requests = trace.get("requests", [])
    successful = [(i, r) for i, r in enumerate(requests) if 200 <= r.get("status_code", 0) < 300]

    def path(row):
        return urlsplit(row.get("path", "")).path

    def work_detail(row):
        match = re.fullmatch(r"/x/v1/work-intents/([^/]+)", path(row))
        data = row.get("response", {})
        work = data.get("intent", {}) if isinstance(data, dict) else {}
        return (unquote(match[1]), data, work) if (row.get("method") == "GET" and match
                and work.get("intent_id") == unquote(match[1])) else None

    def committed(row, kind, identity):
        data = row.get("response", {})
        return (isinstance(data, dict) and data.get("outcome") == "committed" and not data.get("error")
                and any(obj.get("kind") == kind and obj.get("entity_id") == identity
                        for obj in data.get("objects", [])))

    if task_id == "submit-and-follow":
        valid = any(
            row.get("method") == "POST" and path(row) == "/x/v1/work-intents"
            and (work := (row.get("request_body") or {}).get("intent_id"))
            and committed(row, "intent", work)
            and any(j > i and (detail := work_detail(other)) and detail[0] == work for j, other in successful)
            for i, row in successful)
        require(valid, "Submission and later work detail must bind the same created intent")
    elif task_id == "contributor-recall":
        contributor_recall(requests)
    elif task_id == "checkpoint-and-recover":
        valid = False
        for i, resume in successful:
            match = re.fullmatch(r"/x/v1/work-intents/([^/]+)/actions", path(resume))
            if not (resume.get("method") == "POST" and match
                    and (resume.get("request_body") or {}).get("action") == "resume"):
                continue
            work_id = unquote(match[1])
            for j, before_row in successful:
                before = work_detail(before_row)
                if not (j < i and before and before[0] == work_id and before[2].get("execution_mode") == "cpu_reference"):
                    continue
                for k, after_row in successful:
                    after = work_detail(after_row)
                    if not (k > i and after and after[0] == work_id and after[2].get("execution_mode") == "cpu_reference"):
                        continue
                    for allocation in after[1].get("allocations", []):
                        aid, checkpoint = allocation.get("allocation_id", allocation.get("id")), allocation.get("checkpoint") or {}
                        if not (aid in before[2].get("allocation_ids", []) and aid in after[2].get("allocation_ids", [])
                                and committed(resume, "allocation", aid) and checkpoint.get("verification_status") == "verified"
                                and checkpoint.get("continuation_status") == "applied" and checkpoint.get("resumed_attempt_id")):
                            continue
                        for job in after[1].get("jobs", []):
                            attempt = job.get("attempt") or {}
                            jid = job.get("job_id", job.get("id"))
                            old = [x.get("attempt", {}) for x in before[1].get("jobs", []) if x.get("job_id", x.get("id")) == jid]
                            if (jid in after[2].get("job_ids", []) and attempt.get("intent_id") == work_id
                                    and attempt.get("allocation_id") == aid and attempt.get("job_id") == jid
                                    and attempt.get("attempt_id") == checkpoint["resumed_attempt_id"]
                                    and any(x.get("ref") == attempt for x in after[2].get("attempts", []))
                                    and type(attempt.get("generation")) is int
                                    and any(type(x.get("generation")) is int and x["generation"] < attempt["generation"] for x in old)):
                                valid = True
        require(valid, "Recovery needs a real resume between same-CPU-work details and the verified applied new attempt")
    elif task_id == "explain-decision":
        valid = False
        for _, row in successful:
            match = re.fullmatch(r"/x/v1/decisions/([^/]+)", path(row))
            body = row.get("response", {})
            if row.get("method") != "GET" or not match or not isinstance(body, dict):
                continue
            identity = unquote(match[1])
            events = body.get("actual_events", [])
            valid |= (body.get("item", {}).get("decision_id") == identity and body.get("commit_status") == "committed"
                      and bool(events) and all(e.get("event_id") and e.get("decision_id") == identity for e in events))
        require(valid, "Decision GET, plan identity and actual committed events must refer to the same decision")
    elif task_id == "events-report-and-download":
        require(any(row.get("method") == "GET" and path(row) == "/v1/events"
                    and isinstance(row.get("response", {}).get("events"), list) for _, row in successful),
                "Events task needs the actual GET events response")
        reports = [(i, row["response"]) for i, row in successful if row.get("method") == "GET"
                   and path(row) == "/x/v1/policy-validation" and isinstance(row.get("response"), dict)]
        require(reports and trace.get("downloads"), "Policy report and actual browser download are required")
        for download in trace["downloads"]:
            index = download.get("request_index")
            require(type(index) is int and 0 <= index < len(requests), "Download needs its exact HTTP request index")
            request = requests[index]
            match = re.fullmatch(r"/x/v1/policy-validation/artifacts/([^/]+)", path(request))
            require(request.get("method") == "GET" and request.get("status_code") == 200 and match,
                    "Download must come from the policy artifact GET endpoint")
            artifact_id = unquote(match[1])
            require(any(i < index and artifact_id in [*report.get("artifact_ids", []),
                        *(ident for run in report.get("runs", []) for ident in run.get("artifact_ids", []))]
                        for i, report in reports), "Downloaded artifact is not registered by the observed report")
            file, server = download.get("file", {}), download.get("server_artifact", {})
            require(file.get("path") != server.get("path"), "Browser and server evidence cannot reference the same file")
            raw = request.get("raw_response")
            require(isinstance(raw, str) and raw, "Retain the downloaded HTTP response bytes as raw_response")
            require(audit.artifact(file) == audit.artifact(server) == raw.encode("utf-8"),
                    "Browser file must match this actual artifact HTTP response byte-for-byte")
    elif task_id == "administrator-acceptance":
        require(any(row.get("method") == "GET" and path(row) == "/x/v1/acceptance"
                    and isinstance(row.get("response", {}).get("items"), list)
                    and row["response"].get("status") in {"NOT_RUN", "NOT_MET", "MET"}
                    and "build_matches" in row["response"] and row["response"]["build_matches"] in (True, False, None)
                    for _, row in successful), "Acceptance needs its actual GET response schema")
        overviews = [row for row in requests if path(row) == "/x/v1/admin/overview"]
        require(len(overviews) >= 10, "Ten consecutive administrator overview responses are required")
        for row in overviews:
            body = row.get("response", {})
            require(row.get("method") == "GET" and row.get("status_code") == 200 and isinstance(body, dict)
                    and body.get("scope") == "global" and body.get("measurement_kind") == "authorized_record_counts",
                    "Every administrator overview must be a successful GET of the actual overview schema")
            counts = body.get("counts", {})
            for label, field in (("allocations", "allocations_by_state"), ("jobs", "jobs_by_state"), ("work_intents", "work_by_state")):
                states = body.get(field)
                require(type(counts.get(label)) is int and counts[label] >= 0 and isinstance(states, dict)
                        and all(type(n) is int and n >= 0 for n in states.values()) and sum(states.values()) == counts[label],
                        "Overview state counts are missing or inconsistent")
            capacity = body.get("capacity", {})
            require(isinstance(capacity.get("nodes"), list) and isinstance(capacity.get("totals"), dict)
                    and all(isinstance(capacity["totals"].get(key), dict) for key in ("capacity", "available")),
                    "Overview capacity response missing")


def task_condition_evidence(task_id, trace, condition):
    routes = {
        "submit-and-follow": r"/x/v1/work-intents(?:/[^/]+(?:/actions)?)?",
        "contributor-recall": r"/x/v1/contributions(?:/recall)?",
        "checkpoint-and-recover": r"/x/v1/work-intents/[^/]+(?:/actions)?",
        "explain-decision": r"/x/v1/decisions/[^/]+",
        "events-report-and-download": r"(?:/v1/events|/x/v1/policy-validation(?:/artifacts/[^/]+)?)",
        "administrator-acceptance": r"/x/v1/(?:acceptance|admin/overview)",
    }
    empty_routes = {
        "submit-and-follow": {"/x/v1/work-intents": ("items", "intents")},
        "contributor-recall": {"/x/v1/contributions": ("items",)},
        "events-report-and-download": {"/v1/events": ("events",), "/x/v1/policy-validation": ("runs",)},
    }
    for row in trace.get("requests", []):
        path = urlsplit(row.get("path", "")).path
        body = row.get("response")
        if not isinstance(body, dict) or not re.fullmatch(routes[task_id], path):
            continue
        if condition == "error" and 400 <= row.get("status_code", 0) < 600:
            receipt = body.get("receipt") or {}
            if body.get("error") or body.get("detail") or (receipt.get("outcome") == "rejected" and receipt.get("error")):
                return
        if condition == "empty" and row.get("method") == "GET" and row.get("status_code") == 200:
            if any(body.get(key) == [] for key in empty_routes.get(task_id, {}).get(path, ())):
                return
    require(False, f"{condition} condition needs an actual response from this task's relevant product route")


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
            user_task_evidence(audit, task["id"], trace)
            states = set(task.get("conditions", []))
            for condition in states & {"error", "empty"}:
                task_condition_evidence(task["id"], trace, condition)
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
    require(deployment.get("schema") == "cp4-clean-deploy-v1" and deployment.get("unresolved_issues") == [],
            "Video must reference an actual clean-deployment observation, not a build/model stub")
    timestamp(deployment.get("observed_at", ""))
    clean_deploy(audit, deployment)
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
