"""Bounded B-side CP4 challenges over owned loopback TCP services.

This is a fixed HTTP challenge set, not the complete CP4 reliability, scale,
CPU or human gate. All business writes use the real A command HTTP endpoints.
Only the reset-isolation case uses the test key, against its own temporary host.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, object_id, registration  # noqa: E402
from check_checkpoint import current_build  # noqa: E402

MANUAL_APP = "experiments.cp4_reliability:manual_app"
AUTO_APP = "campus_fabric.workflows.devhost:create_app"
CASES = (
    ("owner-isolation", "Foreign detail, lists, events, metrics, mutations and artifacts remain private."),
    ("malformed-no-mutation", "Invalid JSON, shapes, bounds, query cursors and experiment inputs do not mutate the ledger."),
    ("pinned-query-cursors", "Unchanged pagination is stable; changed revision and invalid event cursors fail closed."),
    ("registration-stage-replay", "Concurrent identical registration and stage replay create one intent, allocation and job; changed payload is rejected."),
    ("stale-stage-partial-success", "A stale job stage preserves its existing lease; a fresh same-stage retry binds exactly one job."),
    ("reset-epoch-isolation", "Old stages, objects and event cursors cannot affect the new reset epoch."),
    ("prejob-recall-resume", "Recall before job binding waits without checkpoint fiction or restart cost, then binds once."),
    ("prejob-expiry-terminal", "An expired pre-job lease records terminal failure and cannot be revived."),
    ("w2-timeout-settlement", "Best-effort W2 times out at T+2, settles once, releases capacity and produces no simulated CPU artifact."),
)
CASE_IDS = tuple(case_id for case_id, _ in CASES)


def manual_app():
    """Real A+B host, with only autonomous orchestration disabled for exact races."""
    from campus_fabric.workflows.devhost import create_app
    return create_app(worker_enabled=False)


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True,
                       allow_nan=False) + "\n").encode()


class ChallengeHarness(Harness):
    def request(self, method, path, *, body=None, actor="a", expected=None, key=None,
                raw=None, extra_headers=None):
        """Retain actual wire observations; headers contain no recorded credentials."""
        with self.lock:
            self.request_counter += 1
            request_id = f"cp4-{self.request_counter}"
        headers = self.headers(actor) if actor is not None else {}
        headers.update(extra_headers or {})
        headers["X-Request-Id"] = request_id
        if key is not None:
            headers["Idempotency-Key"] = key
        if raw is not None:
            headers["Content-Type"] = "application/json"
        started = time.monotonic()
        row = {"kind": "http", "request_id": request_id, "method": method,
               "path": path, "actor_alias": actor or "unauthenticated",
               "idempotency_key": key, "request_body": body,
               "request_raw": raw.decode("utf-8", errors="replace") if raw is not None else None,
               "header_overrides": extra_headers or {}, "status": None}
        try:
            response = self.client.request(method, path, headers=headers,
                **({"content": raw} if raw is not None else {"json": body}))
            try:
                value = response.json() if response.content else None
            except ValueError:
                value = {"unparsed": response.text}
            row.update(status=response.status_code, response_body=value,
                       response_headers={name: response.headers[name] for name in
                           ("cache-control", "x-content-type-options", "x-request-id")
                           if name in response.headers})
        except Exception as exc:
            row["transport_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row["elapsed_seconds"] = time.monotonic() - started
            # Defense in depth: payloads/errors can echo headers or sensitive input.
            safe_row = json.loads(self.redact(json.dumps(row, ensure_ascii=False)))
            with self.lock:
                self.history.append(safe_row)
        if expected is not None:
            allowed = (expected,) if isinstance(expected, int) else tuple(expected)
            assert response.status_code in allowed, (method, path, response.status_code, value)
        return response.status_code, value


def clock(h, seconds):
    return h.request("POST", "/x/v1/admin/clock", actor="admin",
                     body={"advance_seconds": seconds}, expected=200)[1]


def read_work(h, name):
    return h.get(f"/x/v1/work-intents/{name}")


def until(h, name, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while True:
        value = read_work(h, name)
        if predicate(value):
            return value
        assert time.monotonic() < deadline, ("bounded wait expired", name, value)
        time.sleep(.04)


def state(h):
    """Authorized HTTP observations, not a production database counter oracle."""
    snapshots = {"capacity": h.get("/v1/capacity"),
            "metrics": h.get("/v1/metrics", "admin"),
            "events": h.get("/v1/events?limit=500", "admin"),
            "allocations": h.get("/v1/allocations?limit=500", "admin"),
            "jobs": h.get("/v1/jobs?limit=500", "admin"),
            "intents": h.get("/x/v1/work-intents?limit=500", "admin")}
    # Only the transport request identity changes on repeated reads. Retain
    # committed request IDs nested in event and stage receipts unchanged.
    return {name: {key: value for key, value in snapshot.items() if key != "request_id"}
            for name, snapshot in snapshots.items()}


def action(h, name, operation, *, stage=None, actor="a", expected=200):
    payload = {"action": operation, "expected_version": read_work(h, name)["version"]}
    if stage is not None:
        payload["stage_id"] = stage
    return h.request("POST", f"/x/v1/work-intents/{name}/actions", actor=actor,
                     body=payload, expected=expected)[1]


def prepare(h, name, *, timeout=100, settlement="keep_lease", start=True,
            preemption="checkpoint", ttl=300):
    body = registration(name, timeout=timeout, settlement=settlement)
    body["specification"]["allocation"].update(
        ttl_seconds=ttl, preemption_mode=preemption, max_restarts=0,
        checkpoint_timeout_seconds=1)
    receipt = h.request("POST", "/x/v1/work-intents", body=body, expected=202)[1]
    allocated = h.stage(name, "allocate", "allocate", receipt["version"])
    if start:
        h.stage(name, "create_job", "job", allocated["version"])
    return read_work(h, name)


def owner_isolation(h):
    own = prepare(h, "private-alice")
    aid, jid = own["item"]["allocation_ids"][0], own["item"]["job_ids"][0]
    before = state(h)
    for path in ("/v1/allocations", "/v1/jobs", "/x/v1/work-intents"):
        assert h.get(path, "b")["items"] == []
    foreign = [h.get("/v1/events", "b"), h.get("/v1/metrics", "b")]
    assert foreign[0]["items"] == []
    assert foreign[1]["counts"] == {"allocations": 0, "jobs": 0, "work_intents": 0}
    for value in foreign:
        assert value["owner_id"] == "bob" and value["scope"] == "owner"
        text = json.dumps(value)
        assert all(secret not in text for secret in ("private-alice", aid, jid))
    for path in (f"/v1/allocations/{aid}", f"/v1/jobs/{jid}",
                 "/x/v1/work-intents/private-alice",
                 "/x/v1/work-intents/private-alice/artifacts/result"):
        h.request("GET", path, actor="b", expected=404)
    action(h, "private-alice", "release", actor="b", expected=404)
    for actor in ("a", "contributor"):
        h.request("POST", "/x/v1/admin/clock", actor=actor,
                  body={"advance_seconds": 10}, expected=403)
    h.request("POST", "/x/v1/contributions/recall", actor="a", expected=403,
              body={"gpu_ids": ["acceptance-gpu"], "notice_seconds": 1, "duration_seconds": 1})
    for path in ("/x/v1/admin/overview", "/x/v1/experiments", "/x/v1/policy-validation"):
        h.request("GET", path, actor="b", extra_headers={"X-Role": "administrator"}, expected=403)
    h.request("GET", "/v1/jobs", actor="b", extra_headers={"X-Owner": "alice"}, expected=403)
    h.request("GET", "/v1/jobs", actor=None, expected=401)
    h.request("GET", "/x/v1/artifacts?" + urlencode({"uri": "file:///private/not-a-registered-artifact"}),
              actor="a", expected=404)
    assert state(h) == before
    return {"foreign_objects": 3, "ledger_unchanged": True,
            "scope": "Logical artifact denial only; real CPU artifact isolation belongs to A/CP3."}


def malformed_no_mutation(h):
    before = state(h)
    h.request("POST", "/x/v1/experiments", actor=None, raw=b"{", expected=401)
    h.request("POST", "/x/v1/experiments", actor="a", raw=b"{", expected=403)
    for raw in (b"{", b"[]", b"null", b"\xff", b" " * 8193):
        h.request("POST", "/x/v1/experiments", actor="admin", raw=raw, expected=400)
    for body, expected in (({"scenario_id": "unregistered", "policy": "B0"}, 400),
            ({"scenario_id": "cp2-contention-v1", "policy": "unknown"}, 400),
            ({"scenario_id": "cp2-contention-v1", "policy": "P", "seed": True}, 400),
            ({"scenario_id": "cp2-contention-v1", "policy": "P", "seed": -1}, 400),
            ({"scenario_id": "cp2-contention-v1", "policy": "P", "seed": 10000}, 400),
            ({"scenario_id": "cp2-contention-v1", "policy": "P", "path": "../outside"}, 400)):
        h.request("POST", "/x/v1/experiments", actor="admin", body=body, expected=expected)
    for raw in (b"{", b"[]", b"null"):
        h.request("POST", "/x/v1/work-intents", raw=raw, expected=400)
    bad = registration("invalid-work")
    bad["specification"]["allocation"]["request"]["gpu"]["count"] = -1
    h.request("POST", "/x/v1/work-intents", body=bad, expected=400)
    for query in ("limit=0", "limit=501", "limit=-1", "limit=true", "limit=%EF%BC%91",
                  "offset=1", "after=bad", "after=0:0:0&offset=0"):
        h.request("GET", "/v1/jobs?" + query, expected=400)
    for query in ("after=bad", "after=0:-1", "limit=0"):
        h.request("GET", "/v1/events?" + query, expected=400)
    assert h.get("/x/v1/experiments", "admin")["items"] == []
    assert state(h) == before
    return {"ledger_unchanged": True, "experiment_runs_created": 0}


def pinned_query_cursors(h):
    for name in ("page-a", "page-b"):
        h.request("POST", "/x/v1/work-intents", body=registration(name), expected=202)
    first = h.get("/x/v1/work-intents?limit=1")
    assert first["next_cursor"]
    next_path = "/x/v1/work-intents?" + urlencode({"after": first["next_cursor"], "limit": 1})
    second = h.get(next_path)
    assert {first["items"][0]["intent_id"], second["items"][0]["intent_id"]} == {"page-a", "page-b"}
    repeated = h.get(next_path)
    assert {k: v for k, v in repeated.items() if k != "request_id"} == {
        k: v for k, v in second.items() if k != "request_id"}
    clock(h, 1)
    before = state(h)
    _, stale = h.request("GET", next_path, expected=409)
    assert stale["error"] == "stale_version"
    epoch = before["metrics"]["version"]["epoch"]
    _, beyond = h.request("GET", f"/v1/events?after={epoch}:9007199254740991", expected=409)
    assert beyond["error"] == "invalid_cursor"
    assert state(h) == before
    return {"distinct_paged_intents": 2, "stale_revision_rejected": True, "ledger_unchanged": True}


def registration_stage_replay(h):
    name = "same-registration"
    body = registration(name)
    barrier = threading.Barrier(8)

    def register_once(_):
        barrier.wait(timeout=10)
        return h.request("POST", "/x/v1/work-intents", body=body, expected=202)[1]

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(register_once, range(8)))
    assert all(receipt == receipts[0] for receipt in receipts)
    allocated = h.stage(name, "allocate", "allocate", receipts[0]["version"])
    started = h.stage(name, "create_job", "job", allocated["version"])
    clock(h, 1)
    before = state(h)
    assert h.request("POST", "/x/v1/work-intents", body=body, expected=202)[1] == receipts[0]
    assert h.stage(name, "allocate", "allocate", receipts[0]["version"]) == allocated
    assert h.stage(name, "create_job", "job", allocated["version"]) == started
    different = copy.deepcopy(body)
    different["specification"]["job"]["timeout_seconds"] = 101
    _, conflict = h.request("POST", "/x/v1/work-intents", body=different, expected=409)
    assert conflict["error"] == "stage_conflict"
    assert state(h) == before
    final = read_work(h, name)
    assert len(final["item"]["allocation_ids"]) == len(final["item"]["job_ids"]) == 1
    assert len(final["item"]["attempts"]) == 1
    return {"concurrent_registration_requests": 8, "intents": 1, "allocations": 1, "jobs": 1,
            "attempts": 1, "replay_and_conflict_mutations": 0}


def stale_stage_partial_success(h):
    name = "partial-existing-lease"
    prepared = prepare(h, name, start=False)
    aid = prepared["item"]["allocation_ids"][0]
    clock(h, 1)
    before = state(h)
    rejected = h.stage(name, "create_job", "job", prepared["version"], expected=409)
    assert rejected["error"] == "stale_version"
    assert state(h) == before
    current = read_work(h, name)
    assert current["jobs"] == [] and current["allocations"][0]["state"] == "Allocated"
    receipt = h.stage(name, "create_job", "job", current["version"])
    replay = h.stage(name, "create_job", "job", current["version"])
    assert object_id(receipt, "job") == object_id(replay, "job")
    final = read_work(h, name)
    assert final["item"]["allocation_ids"] == [aid] and len(final["jobs"]) == 1
    return {"preserved_allocation": aid, "jobs_after_retry": 1, "stale_rejection": True}


def reset_epoch_isolation(h):
    old = prepare(h, "reused-id")
    aid, jid = old["item"]["allocation_ids"][0], old["item"]["job_ids"][0]
    cursor = h.get("/v1/events")["next_cursor"]
    h.reset()  # Own newly created instance only; test credentials never enter artifacts.
    fresh = h.request("POST", "/x/v1/work-intents", body=registration("reused-id"), expected=202)[1]
    assert fresh["version"]["epoch"] > old["version"]["epoch"]
    before = state(h)
    for operation, stage in (("allocate", "allocate"), ("create_job", "job")):
        h.stage("reused-id", operation, stage, old["version"], expected=409)
    h.request("GET", f"/v1/events?after={cursor['epoch']}:{cursor['after_sequence']}", expected=409)
    h.request("GET", f"/v1/allocations/{aid}", expected=404)
    h.request("GET", f"/v1/jobs/{jid}", expected=404)
    assert state(h) == before
    assert read_work(h, "reused-id")["item"]["allocation_ids"] == []
    return {"old_epoch": old["version"]["epoch"], "new_epoch": fresh["version"]["epoch"],
            "old_effects_rejected": True, "ledger_unchanged": True}


def prejob_recall_resume(h):
    name = "prejob-notice"
    original = prepare(h, name, start=False)
    aid = original["item"]["allocation_ids"][0]
    expiry = original["allocations"][0]["expires_at"]
    h.request("POST", "/x/v1/contributions/recall", actor="contributor", expected=201,
              body={"gpu_ids": ["acceptance-gpu"], "notice_seconds": 1, "duration_seconds": 1})
    action(h, name, "create_job", stage="job", expected=409)
    clock(h, 1)
    stopped = read_work(h, name)
    assert stopped["item"]["state"] == "waiting" and stopped["item"]["result"] is None
    assert stopped["allocations"][0]["state"] == "Preempted"
    assert stopped["item"]["job_ids"] == stopped["item"]["attempts"] == []
    action(h, name, "resume", actor="b", expected=404)
    action(h, name, "resume")
    action(h, name, "resume")
    waiting = read_work(h, name)
    assert waiting["allocations"][0]["state"] == "Pending"
    assert waiting["allocations"][0]["restart_count"] == 0
    clock(h, 2)
    granted = read_work(h, name)
    assert granted["allocations"][0]["state"] == "Allocated"
    assert granted["allocations"][0]["expires_at"] == expiry
    action(h, name, "create_job", stage="job")
    action(h, name, "create_job", stage="job")
    final = read_work(h, name)
    assert final["item"]["allocation_ids"] == [aid]
    assert len(final["item"]["job_ids"]) == len(final["item"]["attempts"]) == 1
    assert final["item"]["attempts"][0]["ref"]["generation"] == 1
    assert final["allocations"][0]["restart_count"] == final["jobs"][0]["retry_count"] == 0
    return {"preserved_allocation": aid, "attempt_generation": 1,
            "restart_cost": 0, "waiting_not_completion": True}


def prejob_expiry_terminal(h):
    name = "prejob-expired"
    original = prepare(h, name, start=False, ttl=30)
    clock(h, 31)
    terminal = read_work(h, name)
    assert terminal["item"]["state"] == "failed"
    assert terminal["item"]["result"]["kind"] == "failure"
    assert terminal["item"]["job_ids"] == terminal["item"]["attempts"] == []
    assert terminal["allocations"][0]["placement"] is None and terminal["legal_actions"] == []
    before = state(h)
    for operation, stage in (("allocate", "new-lease"), ("create_job", "new-job"), ("resume", None)):
        action(h, name, operation, stage=stage, expected=409)
    assert state(h) == before
    assert terminal["item"]["allocation_ids"] == original["item"]["allocation_ids"]
    return {"terminal_failure_without_job": True, "revival_attempts_rejected": 3}


def w2_timeout_settlement(h):
    name = "w2-best-effort-timeout"
    body = registration(name, timeout=2, settlement="release_on_terminal")
    body["specification"]["allocation"].update(preemption_mode="best_effort", max_restarts=0)
    h.request("POST", "/x/v1/work-intents", body=body, expected=202)
    initial = until(h, name, lambda value: value["item"]["state"] == "running")
    clock(h, 10)
    final = until(h, name, lambda value: value["allocations"][0]["state"] == "Released")
    result = final["item"]["result"]
    assert final["item"]["state"] == "failed" and result["kind"] == "failure"
    assert result["failure"]["detail"] == "Job timeout"
    assert result["recorded_at"] == initial["now"] + 2 and final["now"] == initial["now"] + 10
    assert result["artifact_uris"] == [] and final["jobs"][0]["state"] == "Failed"
    assert final["jobs"][0]["retry_count"] == final["allocations"][0]["restart_count"] == 0
    assert final["allocations"][0]["checkpoint"] is None and len(final["item"]["attempts"]) == 1
    for field in ("allocation_ids", "job_ids"):
        assert final["item"][field] == initial["item"][field]
    events = [event["event_type"] for event in final["events"]]
    for event in ("job.failed", "intent.failed", "allocation.released", "intent.lease_settled"):
        assert events.count(event) == 1
    assert not any(event.startswith("allocation.preempt") or event == "job.succeeded" for event in events)
    aid = final["item"]["allocation_ids"][0]
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    after = read_work(h, name)
    assert after["item"]["result"] == result
    assert sum(event["event_type"] == "allocation.released" for event in after["events"]) == 1
    capacity = h.get("/v1/capacity")["totals"]
    assert capacity["available"] == capacity["capacity"]
    return {"failure_recorded_at": result["recorded_at"], "start_time": initial["now"],
            "failure": "Job timeout", "attempts": 1, "lease_release_events": 1,
            "capacity_fully_returned": True, "cpu_artifacts": 0}


HANDLERS = {case_id: globals()[case_id.replace("-", "_")] for case_id in CASE_IDS}


def run_suite(output: Path, *, selected=None):
    """Preserve every case, including omitted/not-started ones and partial failures."""
    if not __debug__:
        raise RuntimeError("CP4 validation requires enabled Python assertions; do not use -O or PYTHONOPTIMIZE")
    output = Path(output).resolve()
    selected = CASE_IDS if selected is None else tuple(selected)
    if len(set(selected)) != len(selected) or set(selected) - set(CASE_IDS):
        raise ValueError("Select each registered case at most once")
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError("Refuse to overwrite an existing CP4 challenge evidence directory") from exc
    before = current_build(ROOT)
    manifest = {"schema": "cp4-b-http-challenge-manifest-v1", "cases": [
        {"id": case_id, "expected": expected} for case_id, expected in CASES],
        "selected": list(selected), "build": before,
        "execution_mode": "logical_simulation", "seed": None,
        "source_checkout": str(ROOT), "python": sys.version, "python_executable": sys.executable,
        "manual_fixture": "Builtin scheduler, real HTTP commands; worker disabled for exact stage boundaries.",
        "auto_fixture": "W2 uses real autonomous B worker with external policy P."}
    manifest_bytes = encoded(manifest)
    (output / "manifest.json").write_bytes(manifest_bytes)
    rows = [{"id": case_id, "expected": expected, "status": "NOT_RUN", "evidence": []}
            for case_id, expected in CASES]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="campus-cp4-b-http-") as temporary:
        for row in rows:
            case_id = row["id"]
            if case_id not in selected:
                row["reason"] = "Not selected; no execution claimed."
                continue
            directory = Path(temporary) / case_id
            directory.mkdir()
            h = ChallengeHarness(directory, app_target=AUTO_APP if case_id == "w2-timeout-settlement" else MANUAL_APP)
            h.env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")))
            if case_id == "w2-timeout-settlement":
                h.env.update(FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY="P")
            row["observed_at"] = datetime.now(timezone.utc).isoformat()
            case_start = time.monotonic()
            try:
                h.start()
                assert h.get("/v1/version")["execution_mode"] == "logical_simulation"
                row["actual"] = HANDLERS[case_id](h)
                row["status"] = "MET"
            except Exception:
                row.update(status="NOT_MET", failure=h.redact(traceback.format_exc()))
            finally:
                try:
                    h.stop()
                except Exception:
                    row.update(status="NOT_MET", cleanup_failure=h.redact(traceback.format_exc()))
                row["elapsed_seconds"] = time.monotonic() - case_start
                server_log = directory / "server.log"
                history = {"case_id": case_id, "app_target": h.app_target,
                           "server_command": getattr(h, "command", None), "source_checkout": str(ROOT),
                           "execution_mode": "logical_simulation", "credentials_recorded": False,
                           "records": h.history,
                           "server_log": server_log.read_text(errors="replace") if server_log.exists() else ""}
                raw = h.redact(encoded(history).decode()).encode()
                assert all(token.encode() not in raw for token in (*h.tokens.values(), h.test_key))
                name = f"{case_id}.json"
                (output / name).write_bytes(raw)
                row["evidence"] = [{"path": name, "sha256": hashlib.sha256(raw).hexdigest()}]
            print(f"{row['status']} {case_id}", flush=True)
    after = current_build(ROOT)
    all_executed = all(row["status"] != "NOT_RUN" for row in rows)
    failures = [row["id"] for row in rows if row["status"] == "NOT_MET"]
    status = "NOT_MET" if failures or before != after else ("MET" if all_executed else "NOT_RUN")
    receipt = {"schema_version": "1.0.0", "check": "CP4-B-HTTP-CHALLENGES",
        "scope": "B_BOUNDED_HTTP_SUBSET_ONLY", "status": status,
        "checkpoint": "CP4", "full_cp4_reliability": "NOT_RUN", "human_acceptance": "NOT_RUN",
        "evidence_kind": "http_blackbox", "build": before, "build_at_finish": after,
        "build_matches": before == after, "observed_at": datetime.now(timezone.utc).isoformat(),
        "manifest": {"path": "manifest.json", "sha256": hashlib.sha256(manifest_bytes).hexdigest()},
        "executor": "Automated B challenge runner; no human attestation",
        "command": "python -m experiments.cp4_reliability --output <fresh-directory>"
                   + (" " + " ".join(f"--case {case_id}" for case_id in selected)
                      if selected != CASE_IDS else ""),
        "elapsed_seconds": time.monotonic() - started, "checks": rows,
        "unresolved_issues": (["Source build changed during execution; rerun the stable candidate."]
                              if before != after else []) + [f"Failed case: {case_id}" for case_id in failures],
        "not_validated": ["Complete CP4 mandatory lifecycle/core reliability set",
            "Independent ledger reference model or universal concurrency correctness",
            "32 concurrent commands, 10000 jobs, 50000 commands, scale or controlled latency",
            "Real CPU/GPU execution, crash windows, clean deployment by a different person",
            "Browser/UI, two human operators, video, or full CP3/CP4 acceptance"]}
    (output / "receipt.json").write_bytes(encoded(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=CASE_IDS, action="append", dest="selected")
    parser.add_argument("--list", action="store_true", help="Print fixed cases without execution")
    args = parser.parse_args()
    if args.list:
        print(encoded({"cases": CASES, "status": "NOT_RUN"}).decode(), end="")
        return 0
    output = args.output or ROOT / "evidence/cp4/b/reliability" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    receipt = run_suite(output, selected=args.selected)
    print(json.dumps({"status": receipt["status"], "receipt": str(output.resolve() / "receipt.json"),
                      "full_cp4_reliability": "NOT_RUN"}))
    return 0 if receipt["status"] == "MET" else 1


if __name__ == "__main__":
    raise SystemExit(main())
