"""B settlement over owned loopback HTTP instances; never human acceptance."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from accept_cp1_a import Harness, registration  # noqa: E402
from accept_cp2_b import act, cleanup_cpu, clock, recall, until  # noqa: E402


@contextmanager
def host(directory, name, *, cpu=False):
    h = Harness(directory, app_target=("accept_cp2_b:cpu_app" if cpu else
                                      "campus_fabric.workflows.devhost:create_app"))
    h.env.update(FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY="P",
                 FABRIC_MODE="cpu_reference" if cpu else "logical_simulation")
    h.env["PYTHONPATH"] = str(ROOT / "scripts") + os.pathsep + h.env.get("PYTHONPATH", "")
    h.start()
    try:
        yield h
    finally:
        try:
            if cpu:
                cleanup_cpu(h)
        finally:
            h.stop()
            output = os.environ.get("CP3_SETTLEMENT_HISTORY_DIR")
            if output:
                path = Path(output)
                path.mkdir(parents=True, exist_ok=True)
                raw = json.dumps({"case": name, "history": h.history}, ensure_ascii=False, indent=2)
                assert all(token not in raw for token in [*h.tokens.values(), h.test_key])
                (path / f"{name}.json").write_text(raw + "\n")


def register(h, name, settlement, *, timeout=100, cpu=False):
    body = registration(name, settlement=settlement, timeout=timeout)
    body["execution_mode"] = "cpu_reference" if cpu else "logical_simulation"
    body["specification"]["allocation"].update(preemption_mode="checkpoint", max_restarts=2)
    for _ in range(5):
        status, value = h.request("POST", "/x/v1/work-intents", body=body, expected=(202, 409))
        if status == 202:
            break
        assert value["error"] == "stale_version"
    assert status == 202
    return until(h, f"/x/v1/work-intents/{name}", lambda d: (
        d["item"]["state"] == "running" and
        (not cpu or d["item"]["attempts"][0]["started_at"] is not None)))


@pytest.mark.parametrize(("ending", "settlement", "auto_release"), [
    ("success", "keep_lease", False),
    ("success", "release_on_success", True),
    ("failure", "keep_lease", False),
    ("failure", "release_on_success", False),
    ("failure", "release_on_terminal", True),
    ("cancelled", "keep_lease", False),
    ("cancelled", "release_on_terminal", True),
])
def test_terminal_result_and_lease_settlement_are_independent(tmp_path, ending, settlement,
                                                            auto_release):
    name = f"{ending}-{settlement}"
    path = f"/x/v1/work-intents/{name}"
    with host(tmp_path, name) as h:
        initial = register(h, name, settlement, timeout=2 if ending == "failure" else 100)
        aid, jid = initial["item"]["allocation_ids"][0], initial["item"]["job_ids"][0]
        assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 0
        if ending == "cancelled":
            act(h, name, "cancel")
        else:
            clock(h, 10)
        terminal = until(h, path, lambda d: d["item"]["result"] is not None)
        assert terminal["item"]["result"]["kind"] == ending
        assert terminal["item"]["result"]["artifact_uris"] == []
        assert terminal["item"]["allocation_ids"] == [aid]
        assert terminal["item"]["job_ids"] == [jid]
        assert len(terminal["item"]["attempts"]) == 1
        if ending == "failure":
            assert terminal["item"]["result"]["failure"]["detail"]
        if auto_release:
            terminal = until(h, path, lambda d: d["allocations"][0]["state"] == "Released")
        else:
            assert terminal["allocations"][0]["state"] in {"Running", "Allocated"}
            assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 0
            assert any(a["operation"] == "allocation.release" for a in terminal["legal_actions"])
            act(h, name, "release")
        released = until(h, path, lambda d: d["allocations"][0]["state"] == "Released")
        assert released["item"]["result"] == terminal["item"]["result"]
        assert not any(a["operation"] in {"job.cancel", "allocation.resume"}
                       for a in released["legal_actions"])
        capacity = h.get("/v1/capacity")["totals"]
        assert capacity["available"] == capacity["capacity"]
        h.request("GET", path, actor="b", expected=404)
        denied = h.request("POST", path + "/actions", actor="b", expected=404,
                           body={"action": "release", "expected_version": released["version"]})
        assert denied[0] == 404
        assert h.get(path)["item"] == released["item"]


def test_interruption_ready_is_not_verification_or_applied_and_waiting_is_not_completion(tmp_path):
    name = "logical-interruption"
    path = f"/x/v1/work-intents/{name}"
    with host(tmp_path, name) as h:
        initial = register(h, name, "release_on_success")
        aid = initial["item"]["allocation_ids"][0]
        recall(h, notice=2, duration=5)
        h.request("POST", f"/v1/allocations/{aid}/checkpoint", body={"status": "ready"}, expected=200)
        clock(h, 2)
        interrupted = until(h, path, lambda d: d["allocations"][0]["state"] == "Preempted")
        checkpoint = interrupted["allocations"][0]["checkpoint"]
        assert checkpoint["report_status"] == "ready"
        assert checkpoint["verification_status"] == "unverified"
        assert checkpoint["continuation_status"] == "not_attempted"
        assert interrupted["item"]["result"] is None
        act(h, name, "resume")
        waiting = h.get(path)
        assert waiting["allocations"][0]["state"] == "Pending"
        assert waiting["allocations"][0]["restart_count"] == 0
        assert len(waiting["item"]["attempts"]) == 1
        assert waiting["item"]["result"] is None
        clock(h, 5)
        resumed = until(h, path, lambda d: len(d["item"]["attempts"]) == 2)
        assert resumed["allocations"][0]["restart_count"] == 1
        assert resumed["allocations"][0]["checkpoint"]["continuation_status"] == "not_attempted"
        assert resumed["item"]["allocation_ids"] == initial["item"]["allocation_ids"]
        assert resumed["item"]["job_ids"] == initial["item"]["job_ids"]
        clock(h, 10)
        completed = until(h, path, lambda d: d["item"]["state"] == "completed")
        assert completed["allocations"][0]["state"] == "Released"


def test_actual_cpu_result_is_readable_as_exact_json_and_owner_isolated(tmp_path):
    name = "cpu-result-access"
    path = f"/x/v1/work-intents/{name}"
    with host(tmp_path, name, cpu=True) as h:
        register(h, name, "release_on_success", cpu=True)
        final = until(h, path, lambda d: d["item"]["state"] == "completed" and
                      d["allocations"][0]["state"] == "Released")
        uri = final["item"]["result"]["artifact_uris"][0]
        artifact_path = "/x/v1/artifacts?" + urlencode({"uri": uri})
        response = h.client.get(artifact_path, headers=h.headers("a"))
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        payload = json.loads(response.content)
        assert payload["kind"] == "result" and payload["state"]["step"] == 100_000
        assert type(payload["state"]["value"]) is int
        assert payload["attempt"] == final["item"]["attempts"][-1]["ref"]
        repeated = h.client.get(artifact_path, headers=h.headers("a"))
        assert repeated.content == response.content
        h.history.append({"kind": "cpu_artifact_read", "uri": uri,
                          "bytes": len(response.content),
                          "sha256": hashlib.sha256(response.content).hexdigest(),
                          "raw_json": response.text})
        h.request("GET", artifact_path, actor="b", expected=404)
        h.request("GET", "/x/v1/artifacts?uri=file%3A%2F%2F%2Fetc%2Fpasswd",
                  expected=(400, 404))
        assert h.client.get(artifact_path).status_code == 401
        assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
