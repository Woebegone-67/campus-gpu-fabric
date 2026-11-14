"""Finite evidence-binding controls; synthetic fixtures are never human evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance/cp4"))
import _delivery_review as delivery  # noqa: E402

BUILD = {"git_commit": "a" * 40, "source_sha256": "b" * 64, "source_file_count": 10}
VERSION = {"epoch": 0, "revision": 10}
WORK = "work-binding"
LEASE = "lease-binding"
JOB = "job-binding"
WORK_PATH = f"/x/v1/work-intents/{WORK}"


def request(method, path, response, *, body=None, status=200):
    return {"method": method, "path": path, "status_code": status,
            "request_body": body, "response": copy.deepcopy(response)}


def committed(*objects):
    return {"request_id": "request-binding", "outcome": "committed", "error": None,
            "version": VERSION, "event_ids": ["event-binding"],
            "objects": [{"kind": kind, "entity_id": value} for kind, value in objects]}


def attempt(generation):
    return {"intent_id": WORK, "allocation_id": LEASE, "job_id": JOB,
            "attempt_id": f"attempt-{generation}", "generation": generation}


def detail(generation=1, *, applied=False):
    ref = attempt(generation)
    checkpoint = {"checkpoint_id": "save-binding", "report_status": "ready",
                  "verification_status": "verified", "artifact_uri": "fabric-cpu://run/save",
                  "continuation_status": "applied" if applied else "not_attempted",
                  "resumed_attempt_id": ref["attempt_id"] if applied else None}
    work = {"intent_id": WORK, "owner_id": "alice", "execution_mode": "cpu_reference",
            "allocation_ids": [LEASE], "job_ids": [JOB],
            "attempts": [{"ref": ref, "state": "running" if applied else "stopped",
                          "checkpoint": copy.deepcopy(checkpoint)}]}
    return {"version": copy.deepcopy(VERSION), "intent": work, "item": copy.deepcopy(work),
            "allocations": [{"id": LEASE, "allocation_id": LEASE, "owner_id": "alice",
                             "state": "Running" if applied else "Preempted", "checkpoint": checkpoint}],
            "jobs": [{"id": JOB, "job_id": JOB, "allocation_id": LEASE,
                      "owner_id": "alice", "attempt": ref}], "events": []}


@pytest.fixture
def audit(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "current_build", lambda root: copy.deepcopy(BUILD))
    (tmp_path / "evidence/cp4").mkdir(parents=True)
    return delivery.Audit(tmp_path)


def put(audit, name, value):
    path = audit.root / "evidence/cp4" / name
    path.write_bytes(value)
    return {"path": str(path.relative_to(audit.root)), "sha256": hashlib.sha256(value).hexdigest()}


def submit_trace():
    return {"requests": [
        request("POST", "/x/v1/work-intents", committed(("intent", WORK)),
                body={"intent_id": WORK, "owner_id": "alice"}, status=202),
        request("GET", WORK_PATH, detail()),
    ]}


def recovery_trace():
    return {"requests": [request("GET", WORK_PATH, detail()),
        request("POST", WORK_PATH + "/actions", committed(("allocation", LEASE)),
                body={"action": "resume", "expected_version": VERSION}),
        request("GET", WORK_PATH, detail(2, applied=True))]}


def decision_trace():
    return {"requests": [request("GET", "/x/v1/decisions/decision%3Abinding", {
        "version": VERSION, "item": {"decision_id": "decision:binding",
            "selected_candidate_id": "candidate-1", "candidates": [{"candidate_id": "candidate-1",
                "feasibility": "legal", "intent_id": WORK}]},
        "commit_status": "committed", "commit_receipts": [],
        "actual_events": [{"event_id": "event-1", "decision_id": "decision:binding",
                           "event_type": "decision.committed"}],
    })]}


def download_trace(audit):
    raw = '{"seed":18446744073709551615,"name":"真实原始字节"}\n'
    report = {"status": "ANALYZED", "build": BUILD, "serving_build": BUILD,
              "historical": False, "artifact_ids": ["manifest", "analysis"],
              "runs": [{"run_id": "run-1", "artifact_ids": ["run-1:input"]}], "analysis": {}}
    rows = [request("GET", "/v1/events?limit=50", {"version": VERSION, "items": [], "events": []}),
            request("GET", "/x/v1/policy-validation", report),
            request("GET", "/x/v1/policy-validation/artifacts/run-1%3Ainput", json.loads(raw))]
    rows[-1]["raw_response"] = raw
    return {"requests": rows, "downloads": [{"request_index": 2,
        "file": put(audit, "browser.json", raw.encode()),
        "server_artifact": put(audit, "server.json", raw.encode())}]}


def overview():
    resource = {"cpu_millicores": 0, "memory_mib": 0, "gpu_count": 0,
                "gpu_memory_mib": 0, "ephemeral_storage_mib": 0}
    return {"version": VERSION, "now": 0, "logical_time": 0, "scope": "global", "owner_id": None,
            "measurement_kind": "authorized_record_counts", "performance_measurements": None,
            "counts": {"allocations": 0, "jobs": 0, "work_intents": 0},
            "allocations_by_state": {}, "jobs_by_state": {}, "work_by_state": {},
            "allocations": 0, "jobs": 0, "running_jobs": 0, "succeeded_jobs": 0, "failed_jobs": 0,
            "capacity": {"nodes": [], "totals": {"capacity": resource, "available": resource}}}


def administrator_trace():
    return {"requests": [request("GET", "/x/v1/acceptance", {
        "version": VERSION, "items": [], "status": "NOT_RUN", "build_matches": False})]
        + [request("GET", "/x/v1/admin/overview", overview()) for _ in range(10)]}


@pytest.mark.parametrize("task, factory", [
    ("submit-and-follow", submit_trace), ("checkpoint-and-recover", recovery_trace),
    ("explain-decision", decision_trace), ("administrator-acceptance", administrator_trace),
])
def test_matching_real_response_shapes_are_accepted(audit, task, factory):
    delivery.user_task_evidence(audit, task, factory())


@pytest.mark.parametrize("attack", ["other-created-work", "other-detail-path", "other-detail-object",
                                    "rejected-create", "query-before-create"])
def test_submission_cannot_be_proved_by_following_an_unrelated_work(audit, attack):
    trace = submit_trace()
    first, last = trace["requests"]
    if attack == "other-created-work":
        first["request_body"]["intent_id"] = "different-work"
    elif attack == "other-detail-path":
        last["path"] = "/x/v1/work-intents/different-work"
    elif attack == "other-detail-object":
        last["response"]["intent"]["intent_id"] = "different-work"
    elif attack == "rejected-create":
        first["response"]["outcome"] = "rejected"
    else:
        trace["requests"].reverse()
    with pytest.raises(ValueError):
        delivery.user_task_evidence(audit, "submit-and-follow", trace)


@pytest.mark.parametrize("attack", ["health-endpoint", "no-resume", "wrong-action", "other-resume-work",
    "other-receipt-allocation", "other-after-work", "logical-mode", "unchanged-generation",
    "missing-resumed-attempt", "wrong-job-attempt", "query-before-resume"])
def test_recovery_requires_same_cpu_work_lease_and_new_attempt(audit, attack):
    trace = recovery_trace()
    before, action, after = trace["requests"]
    if attack == "health-endpoint":
        before["path"] = after["path"] = "/healthz"
    elif attack == "no-resume":
        trace["requests"].pop(1)
    elif attack == "wrong-action":
        action["request_body"]["action"] = "release"
    elif attack == "other-resume-work":
        action["path"] = "/x/v1/work-intents/other/actions"
    elif attack == "other-receipt-allocation":
        action["response"]["objects"][0]["entity_id"] = "other-lease"
    elif attack == "other-after-work":
        after["response"]["intent"]["intent_id"] = "other-work"
    elif attack == "logical-mode":
        before["response"]["intent"]["execution_mode"] = "logical_simulation"
        after["response"]["intent"]["execution_mode"] = "logical_simulation"
    elif attack == "unchanged-generation":
        after["response"]["intent"]["attempts"][0]["ref"]["generation"] = 1
        after["response"]["jobs"][0]["attempt"]["generation"] = 1
    elif attack == "missing-resumed-attempt":
        after["response"]["intent"]["attempts"] = []
    elif attack == "wrong-job-attempt":
        after["response"]["jobs"][0]["attempt"] = attempt(3)
    else:
        trace["requests"] = [before, after, action]
    with pytest.raises(ValueError):
        delivery.user_task_evidence(audit, "checkpoint-and-recover", trace)


@pytest.mark.parametrize("attack", ["wrong-method", "wrong-item", "other-event", "empty-event-id", "no-events"])
def test_decision_requires_matching_query_identity_and_committed_events(audit, attack):
    trace = decision_trace()
    row = trace["requests"][0]
    if attack == "wrong-method":
        row["method"] = "DELETE"
    elif attack == "wrong-item":
        row["response"]["item"]["decision_id"] = "other"
    elif attack == "other-event":
        row["response"]["actual_events"][0]["decision_id"] = "other"
    elif attack == "empty-event-id":
        row["response"]["actual_events"][0]["event_id"] = ""
    else:
        row["response"]["actual_events"] = []
    with pytest.raises(ValueError):
        delivery.user_task_evidence(audit, "explain-decision", trace)


def test_download_preserves_registered_artifact_bytes_and_large_integer(audit):
    delivery.user_task_evidence(audit, "events-report-and-download", download_trace(audit))


@pytest.mark.parametrize("attack", ["self-reference", "wrong-request-index", "unrelated-endpoint",
    "unregistered-artifact", "wrong-method", "changed-browser-bytes", "changed-wire-bytes",
    "no-wire-bytes", "prefix-collision", "missing-download-request"])
def test_download_cannot_use_equal_unrelated_files_or_requests(audit, attack):
    trace = download_trace(audit)
    download, row = trace["downloads"][0], trace["requests"][2]
    if attack == "self-reference":
        download["server_artifact"] = copy.deepcopy(download["file"])
    elif attack == "wrong-request-index":
        download["request_index"] = 0
    elif attack == "unrelated-endpoint":
        row["path"] = "/healthz"
    elif attack == "unregistered-artifact":
        row["path"] = "/x/v1/policy-validation/artifacts/absent"
    elif attack == "wrong-method":
        row["method"] = "POST"
    elif attack == "changed-browser-bytes":
        download["file"] = put(audit, "different.json", b"{}\n")
    elif attack == "changed-wire-bytes":
        row["raw_response"] = "{}\n"
    elif attack == "no-wire-bytes":
        del row["raw_response"]
    elif attack == "prefix-collision":
        trace["requests"][0]["path"] = "/v1/events-not-the-events-route"
        trace["requests"][1]["path"] = "/x/v1/policy-validation-not-a-report"
    else:
        trace["requests"].pop()
    with pytest.raises(ValueError):
        delivery.user_task_evidence(audit, "events-report-and-download", trace)


@pytest.mark.parametrize("attack", ["post-acceptance", "delete-overview", "empty-overview", "empty-acceptance",
                                    "missing-refresh", "intermittent-failure", "owner-overview"])
def test_admin_evidence_must_be_actual_global_queries(audit, attack):
    trace = administrator_trace()
    if attack == "post-acceptance":
        trace["requests"][0]["method"] = "POST"
    elif attack == "delete-overview":
        trace["requests"][1]["method"] = "DELETE"
    elif attack == "empty-overview":
        trace["requests"][1]["response"] = {}
    elif attack == "empty-acceptance":
        trace["requests"][0]["response"] = {}
    elif attack == "missing-refresh":
        trace["requests"].pop()
    elif attack == "intermittent-failure":
        trace["requests"].append(request("GET", "/x/v1/admin/overview", {}, status=409))
    else:
        trace["requests"][1]["response"]["scope"] = "owner"
    with pytest.raises(ValueError):
        delivery.user_task_evidence(audit, "administrator-acceptance", trace)


@pytest.mark.parametrize("task, condition, row", [
    ("submit-and-follow", "error", request("POST", WORK_PATH + "/actions",
        {"error": "lease_unavailable", "detail": "Lease is unavailable"}, status=409)),
    ("contributor-recall", "error", request("POST", "/x/v1/contributions/recall",
        {"detail": "Contributor authority is required"}, status=403)),
    ("submit-and-follow", "empty", request("GET", "/x/v1/work-intents?limit=50",
        {"items": [], "intents": [], "version": VERSION})),
    ("contributor-recall", "empty", request("GET", "/x/v1/contributions?limit=50",
        {"items": [], "version": VERSION})),
    ("events-report-and-download", "empty", request("GET", "/v1/events?limit=50",
        {"items": [], "events": [], "version": VERSION})),
    ("events-report-and-download", "empty", request("GET", "/x/v1/policy-validation",
        {"status": "NOT_RUN", "runs": [], "analysis": None})),
])
def test_conditions_accept_actual_task_errors_and_empty_list_shapes(task, condition, row):
    delivery.task_condition_evidence(task, {"requests": [copy.deepcopy(row)]}, condition)


@pytest.mark.parametrize("condition, row", [
    ("error", request("GET", "/healthz", {"detail": "unrelated failure"}, status=503)),
    ("error", request("POST", "/x/v1/contributions/recall", {"error": "forbidden"}, status=403)),
    ("error", request("POST", "/x/v1/work-intents", {}, status=400)),
    ("empty", request("GET", "/healthz", {"items": [], "jobs": []})),
    ("empty", request("GET", WORK_PATH, {"intent": {"intent_id": WORK}, "jobs": []})),
    ("empty", request("POST", "/x/v1/work-intents", {"items": []})),
    ("empty", request("GET", "/x/v1/work-intents-unrelated", {"items": []})),
    ("empty", request("GET", "/x/v1/work-intents", {"items": [WORK], "count": 0})),
])
def test_conditions_reject_unrelated_errors_and_nonlist_empty_claims(condition, row):
    with pytest.raises(ValueError):
        delivery.task_condition_evidence("submit-and-follow", {"requests": [copy.deepcopy(row)]}, condition)
