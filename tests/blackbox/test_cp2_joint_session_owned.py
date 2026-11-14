"""Isolated CLI lifecycle, process ownership and sanitized human-session exports."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import cp2_joint_session as session  # noqa: E402
from accept_cp1_a import registration  # noqa: E402

from campus_fabric.executors.cpu_files import advance  # noqa: E402


def free_ports():
    sockets = [socket.socket() for _ in range(4)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return dict(
            zip(session.DEFAULT_PORTS, [sock.getsockname()[1] for sock in sockets], strict=True)
        )
    finally:
        for sock in sockets:
            sock.close()


def cli(runtime, action, *, ports=None, output=None):
    command = [
        sys.executable,
        str(ROOT / "scripts/cp2_joint_session.py"),
        action,
        "--runtime",
        str(runtime),
    ]
    for name, port in (ports or {}).items():
        command.extend(["--" + name + "-port", str(port)])
    if output:
        command.extend(["--output", str(output)])
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def wait_detail(client, path, headers, predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        response = client.get(path, headers=headers)
        if response.status_code == 200 and predicate(response.json()):
            return response.json()
        time.sleep(0.05)
    raise AssertionError(response.text)


def test_two_mode_session_lifecycle_export_and_repeated_start_preserve_data(tmp_path):
    runtime = tmp_path / "runtime"
    ports = free_ports()
    try:
        started = cli(runtime, "start", ports=ports)
        assert all(
            item["owned_running"] and item["health"]["status"] == 200
            for item in started["services"].values()
        )
        assert started["human_acceptance"] == "NOT_ATTESTED"
        pids = {name: item["pid"] for name, item in started["services"].items()}
        credentials = {
            mode: json.loads((runtime / mode / "client-identities.json").read_text())
            for mode in ("logical", "cpu")
        }
        auth_bytes = {mode: (runtime / mode / "auth.json").read_bytes() for mode in credentials}
        assert auth_bytes["logical"] != auth_bytes["cpu"]
        for mode in credentials:
            for name in ("auth.json", "client-identities.json"):
                assert (runtime / mode / name).stat().st_mode & 0o777 == 0o600
            base = f"http://127.0.0.1:{ports[mode + '-web']}"
            with httpx.Client(base_url=base, trust_env=False) as client:
                assert client.get("/").status_code == 200
                version = client.get("/v1/version").json()
                assert version["execution_mode"] == (
                    "cpu_reference" if mode == "cpu" else "logical_simulation"
                )
                assert version["scheduling_mode"] == "external"
                wrong = credentials["logical" if mode == "cpu" else "cpu"]["alice"]
                assert client.get("/x/v1/work-intents", headers=wrong).status_code == 401

        base = f"http://127.0.0.1:{ports['logical-api']}"
        with httpx.Client(base_url=base, trust_env=False) as client:
            body = registration("session-smoke", settlement="release_on_success")
            response = client.post(
                "/x/v1/work-intents", json=body, headers=credentials["logical"]["alice"]
            )
            assert response.status_code == 202, response.text
            wait_detail(
                client,
                "/x/v1/work-intents/session-smoke",
                credentials["logical"]["alice"],
                lambda value: value["item"]["state"] == "running",
            )
            assert (
                client.post(
                    "/x/v1/admin/clock",
                    json={"advance_seconds": 10},
                    headers=credentials["logical"]["admin"],
                ).status_code
                == 200
            )
            completed = wait_detail(
                client,
                "/x/v1/work-intents/session-smoke",
                credentials["logical"]["alice"],
                lambda value: value["item"]["state"] == "completed",
            )
            assert completed["allocations"][0]["state"] == "Released"

        repeated = cli(runtime, "start", ports=ports)
        assert pids == {name: item["pid"] for name, item in repeated["services"].items()}
        assert all(
            (runtime / mode / "auth.json").read_bytes() == content
            for mode, content in auth_bytes.items()
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{ports['cpu-api']}", trust_env=False
        ) as client:
            body = registration("session-cpu-save", settlement="release_on_terminal")
            body["execution_mode"] = "cpu_reference"
            body["specification"]["allocation"].update(preemption_mode="checkpoint", max_restarts=2)
            response = client.post(
                "/x/v1/work-intents", json=body, headers=credentials["cpu"]["alice"]
            )
            assert response.status_code == 202, response.text
            wait_detail(
                client,
                "/x/v1/work-intents/session-cpu-save",
                credentials["cpu"]["alice"],
                lambda value: (
                    value["item"]["state"] == "running"
                    and value["item"]["attempts"][0]["started_at"] is not None
                ),
            )
            response = client.post(
                "/x/v1/contributions/recall",
                headers=credentials["cpu"]["supplier"],
                json={
                    "gpu_ids": ["sim-a10-gpu0", "sim-a10-gpu1"],
                    "notice_seconds": 2,
                    "duration_seconds": 1,
                },
            )
            assert response.status_code == 201, response.text
            saved = wait_detail(
                client,
                "/x/v1/work-intents/session-cpu-save",
                credentials["cpu"]["alice"],
                lambda value: value["allocations"][0]["state"] == "Preempted",
            )
            assert saved["item"]["attempts"][0]["checkpoint"]["verification_status"] == "verified"
        exported = cli(runtime, "export", output=tmp_path / "export")
        assert exported["complete"] and not exported["auth_exported"]
        contents = "".join(path.read_text() for path in (tmp_path / "export").glob("*.json"))
        for identity in credentials.values():
            for headers in identity.values():
                assert headers["Authorization"].removeprefix("Bearer ") not in contents
        logical = json.loads((tmp_path / "export/logical.json").read_text())
        assert logical["works"]["items"][0]["intent_id"] == "session-smoke"
        assert logical["work_details"][0]["body"]["item"]["state"] == "completed"
        assert logical["events"]["items"]
        cpu = json.loads((tmp_path / "export/cpu.json").read_text())
        assert len(cpu["artifacts"]) == 1
        assert cpu["artifacts"][0]["response"]["status"] == 200
        saved_state = cpu["artifacts"][0]["response"]["body"]["state"]
        assert 0 < saved_state["step"] < 1_000_000
        assert saved_state["value"] == advance(17, 0, saved_state["step"])
        receipt = json.loads((tmp_path / "export/receipt.json").read_text())
        assert receipt["human_acceptance"] == "NOT_ATTESTED"

        stopped = cli(runtime, "stop")
        assert len(stopped["stopped"]) == 4 and not stopped["unresolved"]
        assert not any(
            item["owned_running"] for item in cli(runtime, "status")["services"].values()
        )
        resumed = cli(runtime, "start", ports=ports)
        assert all(item["owned_running"] for item in resumed["services"].values())
        with httpx.Client(base_url=base, trust_env=False) as client:
            detail = client.get(
                "/x/v1/work-intents/session-smoke", headers=credentials["logical"]["alice"]
            )
            assert detail.status_code == 200 and detail.json()["item"] == completed["item"]
    finally:
        if (runtime / "session.json").exists():
            clients = runtime / "cpu/client-identities.json"
            if clients.exists():
                headers = json.loads(clients.read_text())["alice"]
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{ports['cpu-api']}", trust_env=False
                ) as client:
                    try:
                        allocations = (
                            client.get("/v1/allocations", headers=headers).json().get("items", [])
                        )
                        for allocation in allocations:
                            client.delete(f"/v1/allocations/{allocation['id']}", headers=headers)
                    except httpx.ConnectError:
                        pass
            cli(runtime, "stop")
    assert all(session.port_available(port) for port in ports.values())


def test_refuses_protected_ports_and_foreign_process_identity(tmp_path, monkeypatch):
    ports = dict(session.DEFAULT_PORTS, **{"logical-api": 18081})
    with pytest.raises(RuntimeError, match="protected"):
        session.initialize(tmp_path, ports)
    assert not (tmp_path / "session.json").exists()
    manifest = {
        "root": str(ROOT),
        "runtime": str(tmp_path),
        "session_id": "owned-test-token",
        "ports": session.DEFAULT_PORTS,
        "processes": {
            "cpu-api": {"pid": os.getpid(), "identity": session.process_identity(os.getpid())}
        },
    }
    session.write_private(tmp_path / "session.json", manifest)
    monkeypatch.setattr(
        os, "kill", lambda *_args: pytest.fail("Foreign PID must never be signalled")
    )
    stopped = session.stop(tmp_path)
    assert not stopped["stopped"] and "another process" in stopped["unresolved"][0]


def test_historical_report_configuration_survives_environment_cleanup(tmp_path, monkeypatch):
    import uvicorn

    archive = {"suite": "/original/read-only-suite", "manifest_sha256": "a" * 64,
               "index_sha256": "b" * 64}
    record = session.initialize(tmp_path, session.DEFAULT_PORTS, archive)
    with pytest.raises(RuntimeError, match="historical-report settings"):
        session.initialize(tmp_path, session.DEFAULT_PORTS)
    for key in ("FABRIC_CP3_ARCHIVE_SUITE", "FABRIC_CP3_ARCHIVE_MANIFEST_SHA256",
                "FABRIC_CP3_ARCHIVE_INDEX_SHA256", "FABRIC_DATA_DIR", "FABRIC_AUTH_FILE",
                "FABRIC_INVENTORY_FILE", "FABRIC_MODE", "FABRIC_SCHEDULING_MODE", "FABRIC_B_POLICY"):
        monkeypatch.setenv(key, "untrusted-inherited-value")
    def run(*args, **kwargs):
        assert os.environ["FABRIC_CP3_ARCHIVE_SUITE"] == archive["suite"]
        assert os.environ["FABRIC_CP3_ARCHIVE_MANIFEST_SHA256"] == archive["manifest_sha256"]
        assert os.environ["FABRIC_CP3_ARCHIVE_INDEX_SHA256"] == archive["index_sha256"]
        assert kwargs["port"] == session.DEFAULT_PORTS["logical-api"]
    monkeypatch.setattr(uvicorn, "run", run)
    session.serve(tmp_path, "logical-api", record["session_id"], "unused-node")
    assert session.policy_archive_configuration(None, None, None) is None
    with pytest.raises(ValueError, match="both pinned"):
        session.policy_archive_configuration(tmp_path, "a" * 64, None)


def test_redaction_removes_headers_and_nested_known_tokens():
    value = {
        "Authorization": "Bearer test-only-key",
        "nested": [{"token": "test-only-key", "message": "input test-only-key"}],
        "result": 42,
    }
    assert session.redact(value, ["test-only-key"]) == {
        "nested": [{"message": "input <redacted>"}],
        "result": 42,
    }
