#!/usr/bin/env python3
"""Own two local CP2 sessions without touching other developers' processes or data."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

from check_checkpoint import current_build

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
PROTECTED_PORTS = {18081, 18085, 18086, 15174, 15178, 15179}
DEFAULT_PORTS = {"logical-api": 18087, "logical-web": 15180, "cpu-api": 18088, "cpu-web": 15181}
OPENER = build_opener(ProxyHandler({}))


def now():
    return datetime.now(timezone.utc).isoformat()


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def process_identity(pid):
    if not isinstance(pid, int) or pid <= 1:
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = result.stdout.strip().split(None, 6)
    if result.returncode or len(parts) != 7 or "Z" in parts[0]:
        return None
    command = parts[6]
    # macOS's Python launcher replaces its executable during startup. The birth
    # time and unique owned script arguments remain stable across that exec.
    if str(SCRIPT) + " " in command:
        command = command[command.index(str(SCRIPT) + " ") :]
    return " ".join(parts[1:6]) + " " + command


def owns_process(record, token):
    identity = process_identity(record.get("pid"))
    return bool(
        identity
        and identity == record.get("identity")
        and token in identity
        and "cp2_joint_session.py" in identity
        and "_serve" in identity
    )


def http(url, headers=None):
    try:
        with OPENER.open(Request(url, headers=headers or {}), timeout=4) as response:
            body = response.read()
            try:
                body = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                body = {"html_received": bool(body)}
            return {"status": response.status, "body": body}
    except Exception as exc:
        # Do not include request headers, auth-file content or full exception objects.
        return {"status": getattr(exc, "code", None), "error": type(exc).__name__}


def load_session(runtime):
    path = runtime / "session.json"
    if not path.is_file():
        raise RuntimeError(f"No session at {runtime}; run start first")
    session = json.loads(path.read_text())
    if session.get("root") != str(ROOT) or session.get("runtime") != str(runtime):
        raise RuntimeError("Session belongs to another checkout or directory; it was preserved")
    return session


def port_available(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def node_executable():
    local = ROOT / "runtime/b-tools/node/node_modules/.bin/node"
    node = str(local) if local.is_file() else shutil.which("node")
    if not node:
        raise RuntimeError("Node 22.12+ is required; run make setup with the declared toolchain")
    version = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
    major, minor, *_ = map(int, version.split("."))
    if major != 22 or minor < 12:
        raise RuntimeError(f"Found Node {version}; this project requires Node >=22.12 and <23")
    if not (ROOT / "web/node_modules/vite/bin/vite.js").is_file():
        raise RuntimeError("Web dependencies are absent; run make setup first")
    return node


def policy_archive_configuration(suite, manifest_sha256, index_sha256):
    if not any((suite, manifest_sha256, index_sha256)):
        return None
    if not all((suite, manifest_sha256, index_sha256)):
        raise ValueError("Historical report needs a directory and both pinned SHA256 values")
    from campus_fabric.workflows.policy_validation import SealedPolicyValidation

    suite = Path(suite).expanduser().resolve()
    SealedPolicyValidation(ROOT, suite, historical_manifest_sha256=manifest_sha256,
                           historical_index_sha256=index_sha256)._load()
    return {"suite": str(suite), "manifest_sha256": manifest_sha256,
            "index_sha256": index_sha256}


def initialize(runtime, ports, policy_archive=None):
    if len(set(ports.values())) != 4 or any(
        p in PROTECTED_PORTS or not 1024 <= p <= 65535 for p in ports.values()
    ):
        raise RuntimeError("Use four distinct ports >=1024; existing CP1/QA ports are protected")
    manifest = runtime / "session.json"
    if manifest.exists():
        session = load_session(runtime)
        if session["ports"] != ports:
            raise RuntimeError("This session already uses different ports; pass its original ports")
        if session.get("policy_archive") != policy_archive:
            raise RuntimeError("This session already uses different historical-report settings")
        return session
    if any(path.name != ".session.lock" for path in runtime.iterdir()):
        raise RuntimeError(
            "Runtime is nonempty and has no owned session manifest; preserved unchanged"
        )
    session = {
        "schema_version": "1.0.0",
        "session_id": uuid.uuid4().hex,
        "created_at": now(),
        "root": str(ROOT),
        "runtime": str(runtime),
        "ports": ports,
        "processes": {},
        "policy_archive": policy_archive,
    }
    write_private(manifest, session)
    return session


def start(runtime, ports, policy_archive=None):
    if importlib.util.find_spec("uvicorn") is None:
        raise RuntimeError("Python dependencies are absent; run make setup first")
    node = node_executable()
    session = initialize(runtime, ports, policy_archive)
    for mode in ("logical", "cpu"):
        directory = runtime / mode
        directory.mkdir(exist_ok=True)
        auth = directory / "auth.json"
        clients = directory / "client-identities.json"
        if auth.exists() != clients.exists():
            raise RuntimeError(f"Incomplete identity pair at {directory}; preserved for inspection")
        if not auth.exists():
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/init_cp1_demo.py"), str(directory)],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        auth.chmod(0o600)
        clients.chmod(0o600)
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/init_cp1_demo.py"), str(directory),
             "--ensure-accounts"], check=True, stdout=subprocess.DEVNULL,
        )
        inventory = directory / "inventory.json"
        if not inventory.exists():
            shutil.copyfile(ROOT / "configs/cp1_inventory.json", inventory)
    missing = [
        name
        for name in ports
        if not owns_process(session["processes"].get(name, {}), session["session_id"])
    ]
    for name in missing:
        if not port_available(ports[name]):
            raise RuntimeError(f"Port {ports[name]} is occupied; no process was stopped")
    build = current_build(ROOT)
    for name in missing:
        log = runtime / f"{name}.log"
        command = [
            sys.executable,
            str(SCRIPT),
            "_serve",
            "--runtime",
            str(runtime),
            "--service",
            name,
            "--token",
            session["session_id"],
            "--node",
            node,
        ]
        with log.open("ab") as stream:
            log.chmod(0o600)
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
            )
        identity = None
        for _ in range(100):
            identity = process_identity(process.pid)
            if identity and session["session_id"] in identity:
                break
            if process.poll() is not None:
                raise RuntimeError(f"{name} failed to start; inspect {log}")
            time.sleep(0.01)
        session["processes"][name] = {
            "pid": process.pid,
            "identity": identity,
            "started_at": now(),
            "build": build,
        }
        write_private(runtime / "session.json", session)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if not owns_process(session["processes"][name], session["session_id"]):
                raise RuntimeError(
                    f"{name} exited; inspect {log}; other owned services are preserved"
                )
            probe = http(f"http://127.0.0.1:{ports[name]}/healthz")
            if probe.get("status") == 200 and probe.get("body", {}).get("status") == "ok":
                break
            time.sleep(0.15)
        else:
            raise RuntimeError(
                f"{name} is not healthy; inspect {log}; use stop to close owned services"
            )
    return status(runtime, session=session)


def serve(runtime, name, token, node):
    """Unique token stays in the supervisor command line for stop ownership checks."""
    session = load_session(runtime)
    if token != session["session_id"] or name not in session["ports"]:
        raise RuntimeError("Service identity does not belong to this session")
    mode, kind = name.split("-")
    for key in list(os.environ):
        if key.startswith(("FABRIC_", "VITE_")):
            del os.environ[key]
    os.environ.update(
        FABRIC_DATA_DIR=str(runtime / mode / "data"),
        FABRIC_AUTH_FILE=str(runtime / mode / "auth.json"),
        FABRIC_ACCOUNTS_FILE=str(runtime / mode / "accounts.json"),
        FABRIC_INVENTORY_FILE=str(runtime / mode / "inventory.json"),
        FABRIC_MODE="cpu_reference" if mode == "cpu" else "logical_simulation",
        FABRIC_SCHEDULING_MODE="external",
        FABRIC_B_POLICY="P",
    )
    if session.get("policy_archive"):
        archive = session["policy_archive"]
        os.environ.update(
            FABRIC_CP3_ARCHIVE_SUITE=archive["suite"],
            FABRIC_CP3_ARCHIVE_MANIFEST_SHA256=archive["manifest_sha256"],
            FABRIC_CP3_ARCHIVE_INDEX_SHA256=archive["index_sha256"],
        )
    if kind == "api":
        sys.path.insert(0, str(ROOT / "scripts"))
        import uvicorn

        target = (
            "cp2_cpu_preview:create_app"
            if mode == "cpu"
            else "campus_fabric.workflows.devhost:create_app"
        )
        uvicorn.run(
            target, factory=True, host="127.0.0.1", port=session["ports"][name], log_level="warning"
        )
    else:
        os.environ["FABRIC_API_TARGET"] = f"http://127.0.0.1:{session['ports'][mode + '-api']}"
        child = subprocess.Popen(
            [
                node,
                str(ROOT / "web/node_modules/vite/bin/vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                str(session["ports"][name]),
                "--strictPort",
            ],
            cwd=ROOT / "web",
        )

        def forward(_number, _frame):
            if child.poll() is None:
                child.terminate()

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            child.wait()
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


def status(runtime, session=None):
    session = session or load_session(runtime)
    build = current_build(ROOT)
    services = {}
    for name, port in session["ports"].items():
        record = session["processes"].get(name, {})
        owned = owns_process(record, session["session_id"])
        services[name] = {
            "url": f"http://127.0.0.1:{port}",
            "owned_running": owned,
            "pid": record.get("pid"),
            "started_at": record.get("started_at"),
            "build_at_start": record.get("build"),
            "source_matches_start": record.get("build") == build,
            "health": http(f"http://127.0.0.1:{port}/healthz") if owned else None,
        }
        if owned and name.endswith("-api"):
            services[name]["version"] = http(f"http://127.0.0.1:{port}/v1/version")
    return {
        "session_id": session["session_id"],
        "observed_at": now(),
        "current_build": build,
        "runtime": str(runtime),
        "services": services,
        "identities": {
            mode: str(runtime / mode / "client-identities.json") for mode in ("logical", "cpu")
        },
        "login_accounts": {
            mode: str(runtime / mode / "login-accounts.txt") for mode in ("logical", "cpu")
        },
        "human_acceptance": "NOT_ATTESTED",
        "checkpoint_passed": False,
    }


def stop(runtime):
    session = load_session(runtime)
    stopped, refused = [], []
    for name in ("logical-web", "cpu-web", "logical-api", "cpu-api"):
        record = session["processes"].get(name, {})
        if not owns_process(record, session["session_id"]):
            if process_identity(record.get("pid")):
                refused.append(name + ": PID belongs to another process; untouched")
            continue
        os.kill(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while owns_process(record, session["session_id"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        if owns_process(record, session["session_id"]):
            refused.append(name + ": graceful stop timed out; no force-kill issued")
        else:
            stopped.append(name)
    return {
        "stopped": stopped,
        "unresolved": refused,
        "data_preserved": True,
        "note": "Service stop does not cancel work. Real CPU workers may finish while the service is down; restart reconciles them. Complete/cancel and release work in the UI before a final shutdown.",
    }


def redact(value, secrets):
    if isinstance(value, dict):
        return {
            key: redact(item, secrets)
            for key, item in value.items()
            if key.lower()
            not in {"authorization", "auth", "token", "secret", "test_key", "password"}
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "<redacted>")
    return value


def read_pages(base, path, headers, *, events=False):
    # Bound reads during active CPU clocks. Never label a truncated export complete.
    for _ in range(3):
        items, cursor = [], None
        for _ in range(100):
            query = {"limit": 500}
            if cursor:
                query["after"] = cursor
            response = http(base + path + "?" + urlencode(query), headers)
            if response["status"] != 200:
                break
            body = response["body"]
            items.extend(body.get("items", []))
            more = body.get("has_more") if events else body.get("next_cursor") is not None
            if not more:
                return {"complete": True, "version": body.get("version"), "items": items}
            cursor = body.get("next_after") if events else body.get("next_cursor")
        else:
            return {"complete": False, "error": "Export exceeded 100 pages", "items": items}
        if response["status"] != 409:
            return {"complete": False, "response": response, "items": items}
    return {
        "complete": False,
        "error": "Snapshot changed during pagination; retry export",
        "items": [],
    }


def export(runtime, output=None):
    session = load_session(runtime)
    output = output or runtime / "exports" / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    if output.exists():
        raise RuntimeError("Export output already exists; choose a new directory")
    output.mkdir(parents=True)
    report = status(runtime, session=session)
    report["scope"] = "READ_ONLY_SESSION_EXPORT_NOT_HUMAN_ACCEPTANCE"
    report["exports"] = {}
    identities = {
        mode: json.loads((runtime / mode / "client-identities.json").read_text())
        for mode in ("logical", "cpu")
    }
    all_secrets = [
        headers["Authorization"].removeprefix("Bearer ")
        for clients in identities.values()
        for headers in clients.values()
    ]
    for mode in ("logical", "cpu"):
        clients = identities[mode]
        if not report["services"][mode + "-api"]["owned_running"]:
            report["exports"][mode] = {"complete": False, "error": "Owned API is not running"}
            continue
        base = f"http://127.0.0.1:{session['ports'][mode + '-api']}"
        admin = clients["admin"]
        records = {
            "events": read_pages(base, "/v1/events", admin, events=True),
            "works": read_pages(base, "/x/v1/work-intents", admin),
            "allocations": read_pages(base, "/v1/allocations", admin),
            "jobs": read_pages(base, "/v1/jobs", admin),
            "capacity": http(base + "/v1/capacity?limit=500", admin),
            "metrics": http(base + "/v1/metrics", admin),
            "work_details": [],
            "artifacts": [],
        }
        for work in records["works"]["items"]:
            detail = http(base + "/x/v1/work-intents/" + quote(work["intent_id"], safe=""), admin)
            records["work_details"].append(detail)
            owner_headers = clients.get(work["owner_id"])
            if detail["status"] != 200 or not owner_headers:
                continue
            body = detail["body"]
            uris = {job.get("result_uri") for job in body.get("jobs", [])}
            uris.update(
                (attempt.get("checkpoint") or {}).get("artifact_uri")
                for attempt in body["item"].get("attempts", [])
            )
            for uri in sorted(filter(None, uris)):
                records["artifacts"].append(
                    {
                        "uri": uri,
                        "owner_id": work["owner_id"],
                        "response": http(
                            base + "/x/v1/artifacts?" + urlencode({"uri": uri}), owner_headers
                        ),
                    }
                )
        complete = all(
            records[key]["complete"] for key in ("events", "works", "allocations", "jobs")
        )
        complete &= all(records[key]["status"] == 200 for key in ("capacity", "metrics"))
        complete &= all(item["status"] == 200 for item in records["work_details"])
        complete &= all(item["response"]["status"] == 200 for item in records["artifacts"])
        write_private(output / f"{mode}.json", redact(records, all_secrets))
        report["exports"][mode] = {"complete": complete, "file": f"{mode}.json"}
    write_private(output / "receipt.json", redact(report, all_secrets))
    return {
        "output": str(output),
        "complete": all(item["complete"] for item in report["exports"].values()),
        "human_acceptance": "NOT_ATTESTED",
        "auth_exported": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "export", "_serve"))
    parser.add_argument("--runtime", type=Path, default=ROOT / "runtime/cp2-joint")
    for key, port in DEFAULT_PORTS.items():
        parser.add_argument("--" + key + "-port", type=int, default=port)
    parser.add_argument("--output", type=Path, help="New export directory (export only)")
    parser.add_argument("--policy-archive", type=Path, help="Read-only historical CP3 suite (start only)")
    parser.add_argument("--policy-archive-manifest-sha256", help="Pinned original manifest digest")
    parser.add_argument("--policy-archive-index-sha256", help="Pinned original index digest")
    parser.add_argument("--service", choices=tuple(DEFAULT_PORTS), help=argparse.SUPPRESS)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--node", help=argparse.SUPPRESS)
    args = parser.parse_args()
    runtime = args.runtime.expanduser().resolve()
    try:
        if runtime.is_relative_to(ROOT) and not runtime.is_relative_to(ROOT / "runtime"):
            raise RuntimeError(
                "Inside this checkout, private sessions must stay under ignored runtime/"
            )
        if args.action == "_serve":
            serve(runtime, args.service, args.token, args.node)
            return
        runtime.mkdir(parents=True, exist_ok=True)
        with (runtime / ".session.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action == "start":
                ports = {
                    key: getattr(args, key.replace("-", "_") + "_port") for key in DEFAULT_PORTS
                }
                archive = policy_archive_configuration(
                    args.policy_archive, args.policy_archive_manifest_sha256,
                    args.policy_archive_index_sha256)
                result = start(runtime, ports, archive)
            elif args.action == "stop":
                result = stop(runtime)
            elif args.action == "export":
                result = export(
                    runtime, args.output.expanduser().resolve() if args.output else None
                )
            else:
                result = status(runtime)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("unresolved") or result.get("complete") is False:
            raise SystemExit(1)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Session command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
