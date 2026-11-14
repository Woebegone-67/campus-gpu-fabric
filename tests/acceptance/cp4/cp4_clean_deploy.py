"""Review actual CP4 clean deploy evidence without manufacturing observations."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import httpx
from _delivery_review import ROOT, clean_deploy, current_build, digest, emit, review_check


def review(root: Path = ROOT) -> dict:
    return review_check(root, 'CP4-CLEAN-DEPLOY', 'clean_deploy_observer.json', 'cp4-clean-deploy-v1', clean_deploy)


def run_agent_qa(root):
    """Actually clone/build/start in a new directory; never sign for a human."""
    root = Path(root).resolve()
    build = current_build(root)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / "evidence/cp4/agent-clean-deploy" / name
    output.mkdir(parents=True, exist_ok=False)
    record = {"schema": "cp4-clean-deploy-agent-qa-v1", "check": "CP4-CLEAN-DEPLOY",
              "status": "NOT_RUN", "build": build, "observed_at": datetime.now(timezone.utc).isoformat(),
              "observer_kind": "agent", "executor": "codex-clean-clone-runner", "formal_human_gate_satisfied": False,
              "scope": "Actual clean checkout/dependency/build/HTTP checks only; different-human deployment remains required",
              "commands": [], "evidence": [], "unresolved_issues": []}

    def retain(label, content):
        path = output / label
        path.write_bytes(content)
        ref = {"path": path.relative_to(root).as_posix(), "sha256": digest(content)}
        record["evidence"].append(ref)
        return ref

    def command(stage, argv, cwd, timeout=180):
        result = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
        log = retain(stage + ".txt", json.dumps({"argv": argv, "returncode": result.returncode}).encode()
                     + b"\nSTDOUT\n" + result.stdout + b"\nSTDERR\n" + result.stderr)
        record["commands"].append({"stage": stage, "argv": argv, "returncode": result.returncode, "log": log})
        if result.returncode:
            raise ValueError(f"Clean checkout stage {stage} failed ({result.returncode}); actual output retained")
        return result.stdout

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    processes = []
    handles = []

    def stop_group(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    try:
        if not build["git_commit"]:
            raise ValueError("Commit the candidate before clean-clone validation")
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="fabric-cp4-clean-"))
            checkout = Path(directory) / "candidate"
            command("checkout", ["git", "clone", "--no-hardlinks", "--no-checkout", str(root), str(checkout)], directory)
            command("checkout-commit", ["git", "checkout", "--detach", build["git_commit"]], checkout)
            initial = subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout, text=True)
            candidate = current_build(checkout)
            if candidate != build or initial:
                raise ValueError("The clean committed checkout differs from this working tree; freeze all source first")
            record["clean_checkout"] = {"path": str(checkout), "new_directory": True, "initial_git_status": initial, "build": candidate}
            record["readme_sha256"] = digest((checkout / "README.md").read_bytes())
            command("backend-dependencies", ["uv", "sync", "--locked"], checkout)
            node = command("node-version", ["node", "--version"], checkout).decode().strip()
            if not node.startswith("v22."):
                raise ValueError(f"README requires Node 22.x; observed {node}")
            command("frontend-dependencies", ["npm", "--prefix", "web", "ci"], checkout)
            command("contract-consistency", [str(checkout / ".venv/bin/python"), "scripts/check_generated.py"], checkout)
            command("frontend-build", ["npm", "--prefix", "web", "run", "build"], checkout)
            api_port, web_port = free_port(), free_port()
            env = {k: v for k, v in os.environ.items() if not k.startswith("FABRIC_")}
            env.update(FABRIC_INSTANCE="cp4-clean-agent", FABRIC_MODE="logical_simulation",
                       FABRIC_DATA_DIR=str(checkout / "runtime/cp4-clean-agent"),
                       FABRIC_AUTH_FILE=str(checkout / "runtime/cp4-clean-agent/auth.json"),
                       FABRIC_SCHEDULING_MODE="external", FABRIC_B_POLICY="P", PORT=str(api_port),
                       WEB_PORT=str(web_port), FABRIC_API_TARGET=f"http://127.0.0.1:{api_port}")
            requests = []
            for stage, argv in (("api-startup", ["bash", "web/dev-local.sh", "api"]),
                                ("web-startup", ["bash", "web/dev-local.sh", "web"])):
                handle = (output / (stage + ".txt")).open("wb")
                handles.append(handle)
                process = subprocess.Popen(argv, cwd=checkout, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                processes.append(process)
                stack.callback(stop_group, process)
            with httpx.Client(timeout=3, trust_env=False) as client:
                for port, route in ((api_port, "/healthz"), (web_port, "/")):
                    deadline = time.monotonic() + 30
                    while True:
                        if any(p.poll() is not None for p in processes):
                            raise ValueError("A clean startup process exited unexpectedly")
                        try:
                            response = client.get(f"http://127.0.0.1:{port}{route}")
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline:
                            raise ValueError("Clean candidate startup did not become ready in 30 seconds")
                        time.sleep(.1)
                    requests.append({"method": "GET", "path": route, "status_code": response.status_code,
                                     "response": response.json() if route == "/healthz" else response.text})
                for method, route, expected in (("GET", "/v1/version", 200), ("POST", "/v1/allocations", 401)):
                    response = client.request(method, f"http://127.0.0.1:{api_port}{route}", **({"json": {}} if method == "POST" else {}))
                    requests.append({"method": method, "path": route, "status_code": response.status_code, "response": response.json()})
                    if response.status_code != expected:
                        raise ValueError(f"Unexpected clean startup HTTP {route}: {response.status_code}")
            retain("http-history.json", json.dumps({"build": build, "requests": requests}, indent=2).encode() + b"\n")
            record["status"] = "PASS"
            record["actual"] = {"commands": len(record["commands"]), "real_http_responses": len(requests), "clean_build_matches": True}
    except (OSError, ValueError, subprocess.SubprocessError, httpx.HTTPError) as exc:
        record.update(status="NOT_MET", unresolved_issues=[str(exc)])
    finally:
        for process in processes:
            stop_group(process)
        for handle in handles:
            handle.close()
        for name in ("api-startup.txt", "web-startup.txt"):
            path = output / name
            if path.is_file():
                record["evidence"].append({"path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())})
    record["build_at_finish"] = current_build(root)
    if record["build_at_finish"] != build:
        record.update(status="NOT_MET", unresolved_issues=[*record["unresolved_issues"], "Source changed during clean-clone checks"])
    path = output / "agent_qa.json"
    emit(record, path)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--agent-qa", action="store_true", help="Run actual clean clone checks; cannot satisfy the different-human gate")
    args = parser.parse_args(argv)
    if args.agent_qa:
        return 0 if run_agent_qa(args.root)["status"] == "PASS" else 1
    return emit(review(args.root), args.output or args.root / "evidence/cp4/clean_deploy_receipt.json")


if __name__ == "__main__":
    raise SystemExit(main())
