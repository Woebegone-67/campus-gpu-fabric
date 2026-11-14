"""Exercise a real local HTTP process, reporting only CP0 host observations."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from check_checkpoint import current_build

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [
        sys.executable, "-m", "uvicorn", "campus_fabric.app:create_app", "--factory",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "error",
    ]
    temporary = tempfile.TemporaryDirectory(prefix="fabric-host-smoke-")
    env = {key: value for key, value in os.environ.items() if not key.startswith("FABRIC_")}
    env.update(FABRIC_DATA_DIR=temporary.name, FABRIC_MODE="logical_simulation")
    proc = subprocess.Popen(command, cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    checks = []
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
            deadline = time.monotonic() + 15
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stdout.read().decode())
                try:
                    response = client.get("/healthz")
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            assert response.status_code == 200
            assert response.json()["control_plane_ready"] is True
            assert response.json()["checkpoint_passed"] is False
            checks.append({"id": "host-health", "status": "PASS", "http_status": 200})
            response = client.get("/v1/version")
            assert response.status_code == 200
            assert response.json()["api_version"] == "2.0.0"
            assert response.json()["capabilities"]["resource_ledger"]
            assert not response.json()["capabilities"]["real_gpu_execution"]
            checks.append({"id": "version-capability-disclosure", "status": "PASS"})
            response = client.post("/v1/allocations", json={})
            assert response.status_code == 401
            checks.append({"id": "unauthenticated-command-rejected", "status": "PASS"})
            response = client.post("/_test/clock", json={"advance_seconds": 1})
            assert response.status_code == 403
            checks.append({"id": "disabled-test-control-rejected", "status": "PASS"})
            response = client.post("/v1/allocations/missing/checkpoint",
                                   headers={"X-Owner": "smoke"}, json={"status": "ready"})
            assert response.status_code == 501
            assert response.json()["error"] == "not_implemented"
            checks.append({"id": "honest-cp2-stub", "status": "PASS"})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        temporary.cleanup()
    receipt = {
        "schema_version": "1.0.0",
        "build": current_build(ROOT),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "CP0 local HTTP startup only, NOT product conformance",
        "checks": checks,
        "product_checks": "NOT_RUN",
        "command": command,
    }
    path = ROOT / "evidence/cp0/runtime_receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"CP0 real-process smoke passed ({len(checks)} checks); product checks NOT_RUN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
