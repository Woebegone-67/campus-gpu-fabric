"""A+B integration through real TCP and actual owned CPU worker processes."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, registration  # noqa: E402
from accept_cp2_b import act, cleanup_cpu, recall, until  # noqa: E402

from campus_fabric.executors.cpu_files import advance  # noqa: E402


@contextmanager
def owned_host(directory, *, preview=False):
    target = "cp2_cpu_preview:create_app" if preview else "accept_cp2_b:cpu_app"
    h = Harness(directory, app_target=target)
    h.env.update(
        FABRIC_MODE="cpu_reference", FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY="P"
    )
    h.env["PYTHONPATH"] = str(ROOT / "scripts") + os.pathsep + h.env.get("PYTHONPATH", "")
    h.start()
    try:
        yield h
    finally:
        try:
            if h.client is None:
                h.start()
            cleanup_cpu(h)
        finally:
            h.stop()


def register_work(h, name, *, exercise_stale=False):
    body = registration(name, timeout=80, settlement="release_on_success")
    body["execution_mode"] = "cpu_reference"
    body["specification"]["allocation"].update(preemption_mode="checkpoint", max_restarts=2)
    if exercise_stale:
        old = h.get("/v1/metrics")["version"]
        h.advance(0.001)
        headers = {**h.headers("a"), "If-Match": f"{old['epoch']}:{old['revision']}"}
        rejected = h.client.post("/x/v1/work-intents", json=body, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["error"] == "stale_version"
        assert not any(item["intent_id"] == name for item in h.get("/x/v1/work-intents")["items"])
    # Match the client recovery rule: retry only a proven, rejected stale registration.
    for _ in range(5):
        status, receipt = h.request("POST", "/x/v1/work-intents", body=body, expected=(202, 409))
        if status == 202:
            break
        assert receipt["error"] == "stale_version"
    else:
        raise AssertionError("CPU registration cannot make progress")
    return until(
        h,
        f"/x/v1/work-intents/{name}",
        lambda detail: (
            detail["item"]["state"] == "running"
            and detail["item"]["attempts"][0]["started_at"] is not None
        ),
    )


def artifact(h, uri):
    path = "/x/v1/artifacts?" + urlencode({"uri": uri})
    response = h.client.get(path, headers=h.headers("a"))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "token" not in response.json()
    h.request("GET", path, actor="b", expected=404)
    return response.json()


def test_external_p_cpu_recall_restart_resume_auto_settlement_and_persistence(tmp_path):
    with owned_host(tmp_path) as h:
        name = "joint-cpu-p"
        path = f"/x/v1/work-intents/{name}"
        first = register_work(h, name, exercise_stale=True)
        aid, jid = first["allocations"][0]["id"], first["jobs"][0]["id"]
        expiry = first["allocations"][0]["expires_at"]
        decision = next(
            event["decision_id"]
            for event in first["events"]
            if event["event_type"] == "allocation.granted"
        )
        assert h.get(f"/x/v1/decisions/{decision}")["item"]["policy"]["name"] == "P"
        recall(h, notice=2, duration=1)
        stopped = until(h, path, lambda detail: detail["allocations"][0]["state"] == "Preempted")
        cp = stopped["item"]["attempts"][0]["checkpoint"]
        assert (
            cp["verification_status"] == "verified" and cp["continuation_status"] == "not_attempted"
        )
        saved = artifact(h, cp["artifact_uri"])
        assert 0 < saved["state"]["step"] < 100_000

        # A paused work survives a hard service restart without another lease or job.
        h.stop(kill=True)
        h.start()
        recovered = h.get(path)
        assert recovered["allocations"][0]["state"] == "Preempted"
        assert len(recovered["item"]["attempts"]) == 1
        assert recovered["item"]["job_ids"] == [jid]
        act(h, name, "resume")
        resumed = until(
            h,
            path,
            lambda detail: (
                len(detail["item"]["attempts"]) == 2
                and (detail["item"]["attempts"][-1]["checkpoint"] or {}).get("continuation_status")
                == "applied"
            ),
        )
        ref = resumed["item"]["attempts"][-1]["ref"]
        assert ref["generation"] == 2 and ref["job_id"] == jid and ref["allocation_id"] == aid
        assert resumed["allocations"][0]["expires_at"] == expiry

        # The actual resumed worker continues while the second server is killed.
        h.stop(kill=True)
        h.start()
        completed = until(
            h,
            path,
            lambda detail: (
                detail["item"]["state"] == "completed"
                and detail["allocations"][0]["state"] == "Released"
            ),
        )
        result = artifact(h, completed["jobs"][0]["result_uri"])
        assert result["state"] == {"step": 100_000, "value": advance(17, 0, 100_000)}
        assert result["pid"] != saved["pid"]
        assert len(completed["item"]["attempts"]) == 2
        assert completed["item"]["job_ids"] == [jid]
        assert completed["item"]["allocation_ids"] == [aid]
        assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
        h.request("GET", path, actor="b", expected=404)

        h.stop()
        h.start()
        final = h.get(path)
        assert final["item"] == completed["item"]
        assert final["jobs"][0]["result_uri"] == completed["jobs"][0]["result_uri"]
        assert final["allocations"][0]["state"] == "Released"
        journal = json.loads((tmp_path / "database/cpu-executor/journal.json").read_text())
        assert len(journal["attempts"]) == 2
        assert all(attempt["stop_confirmed"] for attempt in journal["attempts"].values())


def test_cpu_preview_factory_starts_real_paced_worker_and_cancels_safely(tmp_path):
    with owned_host(tmp_path, preview=True) as h:
        version = h.get("/v1/version")
        assert version["execution_mode"] == "cpu_reference"
        assert version["capabilities"]["real_cpu_execution"] is True
        assert version["capabilities"]["real_gpu_execution"] is False
        name = "joint-preview"
        path = f"/x/v1/work-intents/{name}"
        initial = register_work(h, name)
        assert initial["item"]["execution_mode"] == "cpu_reference"
        journal_path = tmp_path / "database/cpu-executor/journal.json"
        records = list(json.loads(journal_path.read_text())["attempts"].values())
        assert len(records) == 1 and records[0]["pid"] > 0
        assert records[0]["config"]["total_steps"] == 1_000_000
        assert records[0]["config"]["batch_pause_seconds"] == 0.05
        act(h, name, "cancel")
        until(h, path, lambda detail: detail["item"]["state"] == "cancelled")
        act(h, name, "release")
        until(h, path, lambda detail: detail["allocations"][0]["state"] == "Released")
        assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
        records = list(json.loads(journal_path.read_text())["attempts"].values())
        assert all(record["stop_confirmed"] for record in records)
