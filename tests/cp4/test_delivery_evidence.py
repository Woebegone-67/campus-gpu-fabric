"""Synthetic negative controls for delivery reviewers, never product evidence."""
from __future__ import annotations

import copy
import json
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance/cp4"))
import _delivery_review as delivery  # noqa: E402
from _figure_index import outcome  # noqa: E402
from cp4_clean_deploy import review as clean_review  # noqa: E402
from cp4_evidence_index import review as index_review  # noqa: E402
from cp4_user_tasks import review as task_review  # noqa: E402
from cp4_video_rehearsal import review as video_review  # noqa: E402

BUILD = {"git_commit": "a" * 40, "source_sha256": "b" * 64, "source_file_count": 10}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "current_build", lambda root: copy.deepcopy(BUILD))
    (tmp_path / "evidence/cp4").mkdir(parents=True)
    (tmp_path / "README.md").write_text("Synthetic test README\n")
    return tmp_path


def put(root, name, value):
    path = root / "evidence/cp4" / name
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    path.write_bytes(data)
    return {"path": str(path.relative_to(root)), "sha256": delivery.digest(data)}


def base(schema):
    return {"schema": schema, "build": copy.deepcopy(BUILD), "observed_at": datetime.now(timezone.utc).isoformat(), "unresolved_issues": []}


def human(name="Test B", role="B"):
    return {"human_id": name, "role": role, "observer_kind": "human", "attested": True}


def png(width=1440, height=900):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00" * (width * 3)) * height)) + chunk(b"IEND", b""))


def clean_source(root):
    source = base("cp4-clean-deploy-v1")
    source.update(operator=human(), candidate_preparer_human_id="Test A",
                  readme_sha256=delivery.digest((root / "README.md").read_bytes()),
                  clean_checkout={"path": "/test-only/new-candidate", "new_directory": True,
                                  "initial_git_status": "", "build": copy.deepcopy(BUILD)})
    log = put(root, "command.txt", b"Synthetic non-runtime fixture output\n")
    source["commands"] = [{"stage": stage, "argv": ["fixture"], "returncode": 0, "log": log}
                          for stage in ("checkout", "backend-dependencies", "frontend-dependencies", "frontend-build", "startup")]
    source["http_history"] = put(root, "http.json", {"build": BUILD, "requests": [
        {"method": "GET", "path": "/healthz", "status_code": 200, "response": {"control_plane_ready": True}},
        {"method": "GET", "path": "/v1/version", "status_code": 200, "response": {"api_version": "2.0.0"}},
        {"method": "POST", "path": "/v1/allocations", "status_code": 401, "response": {}},
    ]})
    source["browser_screenshot"] = put(root, "synthetic.png", png())
    return source


@pytest.mark.parametrize("review", [clean_review, task_review, index_review, video_review])
def test_no_observation_never_passes(root, review):
    result = review(root)
    assert result["status"] == "NOT_RUN"
    assert result["unresolved_issues"]


def test_clean_fixture_checks_all_artifacts_then_rejects_changed_log(root):
    source = clean_source(root)
    put(root, "clean_deploy_observer.json", source)
    assert clean_review(root)["status"] == "PASS"
    (root / "evidence/cp4/command.txt").write_text("Modified after observation")
    assert clean_review(root)["status"] == "NOT_MET"


@pytest.mark.parametrize("attack", ["agent", "same-human", "stale", "known-issue", "forged-png", "failed-command"])
def test_clean_deployment_rejects_false_pass(root, attack):
    source = clean_source(root)
    if attack == "agent":
        source["operator"]["observer_kind"] = "agent"
    elif attack == "same-human":
        source["operator"]["human_id"] = "Test A"
    elif attack == "stale":
        source["build"]["source_sha256"] = "c" * 64
    elif attack == "known-issue":
        source["unresolved_issues"] = ["Intermittent unauthorized request succeeds"]
    elif attack == "forged-png":
        source["browser_screenshot"] = put(root, "bad.png", png()[:40])
    else:
        source["commands"][0]["returncode"] = 1
    source["status"] = "PASS"
    put(root, "clean_deploy_observer.json", source)
    assert clean_review(root)["status"] == "NOT_MET"


def test_artifact_symlink_rejected_even_with_matching_hash(root):
    source = clean_source(root)
    outside = root / "outside.txt"
    outside.write_bytes(b"Synthetic non-runtime fixture output\n")
    log = root / "evidence/cp4/command.txt"
    log.unlink()
    log.symlink_to(outside)
    put(root, "clean_deploy_observer.json", source)
    assert clean_review(root)["status"] == "NOT_MET"


def test_native_retina_screenshot_uses_css_viewport_and_actual_dpr(root):
    screenshot = put(root, "retina.png", png(2880, 1800))
    audit = delivery.Audit(root)
    audit.viewport_png(screenshot, {"width": 1440, "height": 900, "device_pixel_ratio": 2})
    with pytest.raises(ValueError, match="Screenshot"):
        audit.viewport_png(screenshot, {"width": 1440, "height": 900, "device_pixel_ratio": 1})


def test_two_accounts_of_one_person_cannot_cross_sign(root):
    source = base("cp4-user-tasks-v1")
    source["operators"] = [human("Same person", "A"), human("same PERSON", "B")]
    put(root, "user_tasks_observer.json", source)
    result = task_review(root)
    assert result["status"] == "NOT_MET"
    assert "distinct humans" in result["unresolved_issues"][0]


def test_partial_task_list_cannot_satisfy_six_tasks(root):
    source = base("cp4-user-tasks-v1")
    source["operators"] = [human("Test A", "A"), human()]
    for person in source["operators"]:
        person["tasks"] = [{"id": "submit-and-follow", "status": "PASS"}]
    put(root, "user_tasks_observer.json", source)
    assert task_review(root)["status"] == "NOT_MET"


def contributor_requests():
    supplier = {"actor_id": "supplier", "owner_id": "supplier", "role": "contributor"}
    borrower = {"actor_id": "bob", "owner_id": "bob", "role": "researcher"}
    body = {"gpu_ids": ["gpu-1"], "notice_seconds": 30, "duration_seconds": 60}
    return [
        {"method": "GET", "path": "/x/v1/contributions?limit=50", "status_code": 200,
         "actor": supplier, "response": {"items": [{"contributor_owner_id": "supplier", "devices": [
             {"device_id": "gpu-1", "physical_gpu_id": "gpu-1"}]}]}},
        {"method": "POST", "path": "/x/v1/contributions/recall", "status_code": 201,
         "actor": supplier, "origin": "browser", "request_body": copy.deepcopy(body),
         "response": {"outcome": "committed", "error": None, "objects": [{"kind": "gpu", "entity_id": "gpu-1"}]}},
        {"method": "GET", "path": "/x/v1/work-intents", "status_code": 200, "actor": borrower,
         "response": {"scope": "owner", "owner_id": "bob", "items": []}},
        {"method": "POST", "path": "/x/v1/contributions/recall", "status_code": 403,
         "actor": borrower, "origin": "explicit_http", "request_body": copy.deepcopy(body),
         "response": {"detail": "Contributor authority is required"}},
    ]


def test_borrower_refusal_on_known_foreign_gpu_is_valid_original_boundary():
    delivery.contributor_recall(contributor_requests())


@pytest.mark.parametrize("attack", ["unrelated403", "different-gpu", "unknown-owner", "own-resource",
                                    "anonymous", "wrong-success-card", "no-browser-success", "unrelated-error", "no-body"])
def test_recall_rejects_unbound_or_wrong_authorization_evidence(attack):
    rows = contributor_requests()
    if attack == "unrelated403":
        rows[3]["path"] = "/x/v1/policy-validation"
    elif attack == "different-gpu":
        rows[3]["request_body"]["gpu_ids"] = ["other-gpu"]
    elif attack == "unknown-owner":
        rows[0]["response"]["items"][0]["contributor_owner_id"] = "unrelated-owner"
    elif attack == "own-resource":
        rows[3]["actor"]["owner_id"] = "supplier"
    elif attack == "anonymous":
        rows.pop(2)
    elif attack == "wrong-success-card":
        rows[1]["response"]["objects"][0]["entity_id"] = "other-gpu"
    elif attack == "no-browser-success":
        rows[1]["origin"] = "agent_http"
    elif attack == "unrelated-error":
        rows[3]["response"] = {"detail": "Artifact unavailable"}
    else:
        rows[3].pop("request_body")
    with pytest.raises(ValueError, match="same known GPU"):
        delivery.contributor_recall(rows)


def test_asynchronous_partial_success_uses_persisted_receipts_and_same_lease():
    trace = {"retained_allocation_id": "lease-1", "duplicate_allocation_created": False}
    detail = {"intent": {"intent_id": "work-1", "allocation_ids": ["lease-1"], "job_ids": [], "stages": [
        {"operation": "allocate", "state": "committed", "receipt": {
            "request_id": "allocate-1", "outcome": "committed", "error": None,
            "objects": [{"kind": "allocation", "entity_id": "lease-1"},
                        {"kind": "intent", "entity_id": "work-1"}]}},
        {"operation": "create_job_and_bind", "state": "failed",
         "receipt": {"outcome": "rejected", "error": {"code": "invalid_state"}, "event_ids": []}},
    ]}}
    rows = [{"method": "GET", "path": "/x/v1/work-intents/work-1", "status_code": 200,
             "response": copy.deepcopy(detail)} for _ in range(2)]
    delivery.partial_success(trace, rows)
    rows[1]["response"]["intent"]["allocation_ids"].append("duplicate-lease")
    with pytest.raises(ValueError, match="same single lease"):
        delivery.partial_success(trace, rows)
    rows[1]["response"] = copy.deepcopy(detail)
    for row in rows:
        row["response"]["intent"]["stages"][1]["receipt"] = {"outcome": "committed"}
    with pytest.raises(ValueError, match="actual later rejection"):
        delivery.partial_success(trace, rows)


def partial_requests():
    """Real API shapes: committed allocate, detail, rejected create, detail."""
    receipt = {"request_id": "allocate-1", "outcome": "committed", "error": None,
               "objects": [{"kind": "allocation", "entity_id": "lease-1"},
                           {"kind": "intent", "entity_id": "work-1"}]}
    detail = {"intent": {"intent_id": "work-1", "allocation_ids": ["lease-1"], "job_ids": [],
                        "stages": [{"operation": "allocate", "state": "committed", "receipt": receipt}]}}
    path = "/x/v1/work-intents/work-1"
    return [
        {"method": "POST", "path": path + "/actions", "status_code": 200,
         "request_body": {"action": "allocate"}, "response": copy.deepcopy(receipt)},
        {"method": "GET", "path": path, "status_code": 200, "response": copy.deepcopy(detail)},
        {"method": "POST", "path": path + "/actions", "status_code": 409,
         "request_body": {"action": "create_job"}, "response": {"error": "stale_version", "receipt": {
             "outcome": "rejected", "error": {"code": "stale_version"}, "event_ids": []}}},
        {"method": "GET", "path": path, "status_code": 200, "response": copy.deepcopy(detail)},
    ]


def test_direct_partial_success_requires_the_same_work_action_receipts_in_order():
    delivery.partial_success({"retained_allocation_id": "lease-1", "duplicate_allocation_created": False},
                             partial_requests())


@pytest.mark.parametrize("attack", ["unrelated-post", "other-work", "wrong-action", "reversed-order",
                                    "late-allocation", "no-rejected-receipt", "wrong-lease", "job-created"])
def test_direct_partial_success_rejects_unrelated_or_reordered_evidence(attack):
    rows = partial_requests()
    if attack == "unrelated-post":
        rows[2]["path"] = "/x/v1/contributions/recall"
    elif attack == "other-work":
        rows[2]["path"] = "/x/v1/work-intents/work-2/actions"
    elif attack == "wrong-action":
        rows[2]["request_body"]["action"] = "release"
    elif attack == "reversed-order":
        rows[1], rows[2] = rows[2], rows[1]
    elif attack == "late-allocation":
        rows[0], rows[2] = rows[2], rows[0]
    elif attack == "no-rejected-receipt":
        rows[2]["response"] = {"error": "forbidden"}
    elif attack == "wrong-lease":
        rows[0]["response"]["objects"][0]["entity_id"] = "lease-2"
    else:
        rows[3]["response"]["intent"]["job_ids"] = ["unexpected-job"]
    with pytest.raises(ValueError, match="actual later rejection"):
        delivery.partial_success({"retained_allocation_id": "lease-1", "duplicate_allocation_created": False}, rows)


def test_asynchronous_partial_success_rejects_reversed_stages():
    rows = partial_requests()
    rows[3]["response"]["intent"]["stages"].insert(0, {
        "operation": "create_job_and_bind", "state": "failed",
        "receipt": {"outcome": "rejected", "error": {"code": "invalid_state"}, "event_ids": []}})
    # Remove direct rejection so the retained GET cannot conceal reversed stages.
    rows.pop(2)
    with pytest.raises(ValueError, match="actual later rejection"):
        delivery.partial_success({"retained_allocation_id": "lease-1", "duplicate_allocation_created": False}, rows)


def test_empty_figure_inventory_cannot_vacuously_pass(root):
    source = base("cp4-figure-index-v1")
    source["figure_inventory"] = put(root, "inventory.json", {"schema": "cp4-figure-inventory-v1", "build": BUILD,
        "scope": "complete-declared-candidate-figure-inventory", "figure_ids": [], "publications": []})
    source["figures"] = []
    put(root, "figure_index_source.json", source)
    assert index_review(root)["status"] == "NOT_MET"


@pytest.mark.parametrize("attack", ["value", "run", "recipe", "build", "omitted-source"])
def test_fast_figure_review_rejects_stale_deep_bindings(root, attack):
    import _figure_index
    from _figure_index import sha

    inventory = {"build": BUILD, "scope": "complete-declared-candidate-figure-inventory", "publications": []}
    source = base("cp4-figure-index-v1")
    source.update(catalog_kind="cp3-frozen-and-research-v1", figure_inventory=put(root, "inventory.json", inventory),
                  figures=[{"id": "old-run", "value": 1.0}])
    source_ref = put(root, "figure_index_source.json", source)
    deep = base("cp4-figure-deep-audit-v1")
    deep.update(status="PASS", source_sha256=source_ref["sha256"],
                recipe_sha256=sha(Path(_figure_index.__file__).read_bytes()),
                evidence=[source_ref, source["figure_inventory"]], actual={"figures": 8, "historical_runs": 125, "micro_runs": 13})
    if attack == "value":
        source["figures"][0]["value"] = .8
    elif attack == "run":
        source["figures"][0]["id"] = "new-run"
    elif attack == "recipe":
        deep["recipe_sha256"] = "e" * 64
    elif attack == "build":
        deep["build"]["git_commit"] = "f" * 40
    else:
        deep["evidence"].pop()
    put(root, "figure_index_source.json", source)
    put(root, "figure_deep_audit.json", deep)
    result = index_review(root)
    assert result["status"] == "NOT_MET"
    assert any(word in result["unresolved_issues"][0] for word in ("stale", "another", "different", "omits"))


def test_video_build_mismatch_rejected_after_media_probe(root, monkeypatch):
    monkeypatch.setattr(delivery.subprocess, "check_output", lambda *a, **k: json.dumps({
        "streams": [{"codec_type": "video"}], "format": {"duration": "60"}}))
    source = base("cp4-video-rehearsal-v1")
    source.update(operator=human(), video=put(root, "fixture.mp4", b"Synthetic fixture; not a real media acceptance"),
                  model="logical_simulation", scene_id="fixture-scene", credentials_visible=False)
    source["scene"] = put(root, "scene.json", {"schema": "cp4-rehearsal-scene-v1", "build": BUILD})
    source["deployment_observation"] = put(root, "deployment.json", {"build": {**BUILD, "git_commit": "d" * 40}})
    put(root, "video_rehearsal_observer.json", source)
    assert video_review(root)["status"] == "NOT_MET"


def test_video_same_build_model_stub_cannot_replace_clean_deployment(root, monkeypatch):
    monkeypatch.setattr(delivery.subprocess, "check_output", lambda *a, **k: json.dumps({
        "streams": [{"codec_type": "video"}], "format": {"duration": "60"}}))
    source = base("cp4-video-rehearsal-v1")
    source.update(operator=human(), video=put(root, "fixture.mp4", b"Synthetic media fixture"),
                  model="logical_simulation", scene_id="fixture-scene", credentials_visible=False)
    source["scene"] = put(root, "scene.json", {"schema": "cp4-rehearsal-scene-v1", "build": BUILD,
                                            "model": "logical_simulation"})
    source["deployment_observation"] = put(root, "deployment.json", {"build": BUILD,
                                                                   "model": "logical_simulation"})
    put(root, "video_rehearsal_observer.json", source)
    result = video_review(root)
    assert result["status"] == "NOT_MET"
    assert "build/model stub" in result["unresolved_issues"][0]


def test_area_recalculation_preserves_failed_and_unfinished_cohort():
    inputs = {"horizon": 80, "works": [{"intent_id": str(i), "arrived_at": 0, "owner_id": "a"} for i in range(3)]}
    snapshot = {"intents": [{"intent_id": "0", "state": "completed", "result": {"recorded_at": 10}},
                            {"intent_id": "1", "state": "failed", "result": {"recorded_at": 20}},
                            {"intent_id": "2", "state": "waiting", "result": None}]}
    summary = {"works": [{**work, "status": state, "completed_at": finish, "effective_service": service}
                         for work, state, finish, service in zip(inputs["works"], ["completed", "failed", "unfinished"], [10, None, None], [1, 0, 0])]}
    assert outcome(inputs, snapshot, summary) == {"F": 170., "completed": 1, "owner_service": {"a": 1}}
    dropped = copy.deepcopy(summary)
    dropped["works"].pop()
    with pytest.raises(ValueError, match="cohort"):
        outcome(inputs, snapshot, dropped)
    changed = copy.deepcopy(summary)
    changed["works"][0]["completed_at"] = 5
    with pytest.raises(ValueError, match="raw work state"):
        outcome(inputs, snapshot, changed)
