"""Exercise A's CP1 backend over TCP; this does not certify joint UI acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
from check_checkpoint import current_build

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evidence/cp1"
SCOPE = "A_CP1_BACKEND_ONLY"


def inventory_node():
    capacity = {"cpu_millicores": 4000, "memory_mib": 8192,
                "ephemeral_storage_mib": 1024, "gpu_count": 1, "gpu_memory_mib": 8192}
    return {"node_id": "acceptance-node", "contributor_owner_id": "supplier",
            "state": "Ready", "capacity": capacity, "available": capacity,
            "observed_at": 0.0, "devices": [{
                "device_id": "acceptance-gpu", "physical_gpu_id": "acceptance-gpu",
                "model": "A10", "memory_mib": 8192, "profile": None, "state": "available",
            }], "labels": []}


def allocation_body(*, ttl=300, cpu=1000):
    return {"owner": "alice", "request": {
        "cpu_millicores": cpu, "memory_mib": 1024, "ephemeral_storage_mib": 32,
        "gpu": {"count": 1, "model": "A10", "memory_mib": 4096},
    }, "ttl_seconds": ttl}


def registration(intent, *, settlement="keep_lease", timeout=100):
    allocation = allocation_body()
    allocation["owner_id"] = allocation.pop("owner")
    allocation["kind"] = "allocation.create"
    return {"intent_id": intent, "owner_id": "alice", "execution_mode": "logical_simulation",
            "settlement_action": settlement, "specification": {
                "allocation": allocation,
                "job": {"image": "acceptance-logical-work", "command": ["work"],
                        "timeout_seconds": timeout, "max_retries": 0},
            }}


def object_id(receipt, kind):
    return next(ref["entity_id"] for ref in receipt["objects"] if ref["kind"] == kind)


class Harness:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.history = []
        self.checks = []
        self.lock = threading.Lock()
        self.request_counter = 0
        self.process = None
        self.client = None
        self.log = None
        self.tokens = {name: secrets.token_urlsafe(24) for name in ("a", "b", "contributor", "admin")}
        self.test_key = secrets.token_urlsafe(24)
        actors = {
            "a": {"actor_id": "acceptance-alice", "owner_id": "alice", "role": "researcher"},
            "b": {"actor_id": "acceptance-bob", "owner_id": "bob", "role": "researcher"},
            "contributor": {"actor_id": "acceptance-supplier", "owner_id": "supplier",
                            "role": "contributor"},
            "admin": {"actor_id": "acceptance-admin", "role": "administrator"},
        }
        self.owners = {"a": "alice", "b": "bob", "contributor": "supplier"}
        auth_path = self.directory / "auth.json"
        inventory_path = self.directory / "inventory.json"
        auth_path.write_text(json.dumps({self.tokens[name]: actor for name, actor in actors.items()}))
        auth_path.chmod(0o600)
        inventory_path.write_text(json.dumps({"nodes": [inventory_node()]}))
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("FABRIC_")}
        self.env.update(FABRIC_DATA_DIR=str(self.directory / "database"),
                        FABRIC_INVENTORY_FILE=str(inventory_path), FABRIC_AUTH_FILE=str(auth_path),
                        FABRIC_TEST_KEY=self.test_key, FABRIC_MODE="logical_simulation",
                        FABRIC_SCHEDULING_MODE="builtin")

    def redact(self, value):
        text = str(value)
        for token in [*self.tokens.values(), self.test_key]:
            text = text.replace(token, "<redacted>")
        return text

    def headers(self, actor):
        if actor == "test":
            return {"X-Test-Key": self.test_key}
        headers = {"Authorization": "Bearer " + self.tokens[actor]}
        if actor in self.owners:
            headers["X-Owner"] = self.owners[actor]
        return headers

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.command = [sys.executable, "-m", "uvicorn", "campus_fabric.app:create_app", "--factory",
                        "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "error"]
        self.log = (self.directory / "server.log").open("ab")
        self.process = subprocess.Popen(self.command, cwd=ROOT, env=self.env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=15,
                                   trust_env=False, limits=httpx.Limits(max_connections=40))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(self.redact((self.directory / "server.log").read_text()))
            try:
                response = self.client.get("/healthz")
                if response.status_code == 200:
                    self.history.append({"kind": "process_started", "health": response.json()})
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.05)
        raise TimeoutError("HTTP server did not become healthy")

    def stop(self, *, kill=False):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.history.append({"kind": "process_stopped", "signal": "SIGKILL" if kill else "SIGTERM",
                                 "returncode": self.process.returncode})
        if self.log is not None:
            self.log.close()
            self.log = None

    def request(self, method, path, *, body=None, actor="a", expected=None, key=None):
        with self.lock:
            self.request_counter += 1
            request_id = f"accept-{self.request_counter}"
        headers = self.headers(actor)
        headers["X-Request-Id"] = request_id
        if key is not None:
            headers["Idempotency-Key"] = key
        response = self.client.request(method, path, json=body, headers=headers)
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = {"unparsed": self.redact(response.text)}
        with self.lock:
            self.history.append({"kind": "http", "request_id": request_id,
                                 "method": method, "path": path, "actor_alias": actor,
                                 "idempotency_key": key, "request_body": body,
                                 "status": response.status_code, "response_body": data})
        if expected is not None:
            allowed = (expected,) if isinstance(expected, int) else tuple(expected)
            assert response.status_code in allowed, (method, path, response.status_code, data)
        return response.status_code, data

    def get(self, path, actor="a"):
        return self.request("GET", path, actor=actor, expected=200)[1]

    def check(self, name, observed):
        self.checks.append({"id": name, "status": "PASS", "observed": observed})
        print(f"PASS {name}: {observed}", flush=True)

    def reset(self):
        self.request("POST", "/_test/reset", actor="test", expected=204)
        empty = self.get("/v1/capacity")
        assert empty["nodes"] == []
        self.request("POST", "/x/v1/contributions", body=inventory_node(),
                     actor="contributor", expected=201)

    def advance(self, seconds):
        return self.request("POST", "/_test/clock", actor="test",
                            body={"advance_seconds": seconds}, expected=200)[1]

    def stage(self, intent, action, stage_id, version, *, expected=200):
        return self.request("POST", f"/x/v1/work-intents/{intent}/actions",
                            body={"action": action, "stage_id": stage_id,
                                  "expected_version": version}, expected=expected)[1]

    def register_and_allocate(self, intent, *, settlement="keep_lease", timeout=100):
        receipt = self.request("POST", "/x/v1/work-intents",
                               body=registration(intent, settlement=settlement, timeout=timeout),
                               expected=202)[1]
        allocated = self.stage(intent, "allocate", "allocate", receipt["version"])
        return receipt, allocated

    def send_without_reading(self, path, body):
        raw = json.dumps(body).encode()
        headers = {**self.headers("a"), "Host": f"127.0.0.1:{self.port}",
                   "Content-Type": "application/json", "Content-Length": str(len(raw)),
                   "X-Request-Id": "lost-stage-response", "Connection": "close"}
        request = f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(request.encode() + b"\r\n" + raw)
        self.history.append({"kind": "http_sent_response_not_read", "method": "POST", "path": path,
                             "actor_alias": "a", "request_id": "lost-stage-response",
                             "request_body": body, "response_read": False})
        return sock


def run_checks(h):
    h.start()
    version = h.get("/v1/version")
    assert version["execution_mode"] == "logical_simulation"
    assert not version["capabilities"]["real_cpu_execution"]
    assert not version["capabilities"]["checkpoint_recovery"]
    h.check("execution-disclosure", "Logical simulation; no CPU execution or recovery claimed")
    before = h.get("/v1/capacity")
    after = h.get("/v1/capacity")
    assert before == after
    assert before["totals"]["available"]["gpu_count"] == 1
    assert "contributor_owner_id" not in json.dumps(before)
    h.check("capacity-read-only", "Repeated reads preserve version and inventory; one physical GPU")

    barrier = threading.Barrier(32)

    def compete(index):
        barrier.wait(timeout=10)
        return h.request("POST", "/v1/allocations", body=allocation_body(),
                         key=f"distinct-{index}", expected=(201, 202))

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(compete, range(32)))
    counts = Counter(code for code, _ in results)
    assert counts == {201: 1, 202: 31}, counts
    allocations = h.get("/v1/allocations")["allocations"]
    assert len(allocations) == 32
    assert sum(item["state"] == "Allocated" for item in allocations) == 1
    assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 0
    h.check("32-network-last-resource", "32 TCP requests: one allocated,31 pending,zero free GPUs")

    h.reset()
    barrier = threading.Barrier(32)

    def repeat(index):
        barrier.wait(timeout=10)
        return h.request("POST", "/v1/allocations", body=allocation_body(),
                         key="same-key", expected=201)[1]

    with ThreadPoolExecutor(max_workers=32) as pool:
        repeated = list(pool.map(repeat, range(32)))
    assert all(value == repeated[0] for value in repeated)
    assert len(h.get("/v1/allocations")["allocations"]) == 1
    aid = repeated[0]["id"]
    h.request("POST", "/v1/allocations", body=allocation_body(cpu=2000),
              key="same-key", expected=409)
    h.check("32-network-idempotency", "32 identical responses,one allocation; changed payload409")
    h.request("GET", f"/v1/allocations/{aid}", actor="b", expected=404)
    h.request("DELETE", f"/v1/allocations/{aid}", actor="b", expected=404)
    assert h.get("/v1/allocations", "b")["allocations"] == []
    assert h.get("/v1/events", "b")["events"] == []
    metrics = h.get("/v1/metrics", "b")
    assert metrics["allocations"] == metrics["jobs"] == 0
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Allocated"
    h.check("owner-isolation", "Bob cannot read/write Alice's allocation; lists/events empty,metrics0")

    h.reset()
    allocation = h.request("POST", "/v1/allocations", body=allocation_body(ttl=30), expected=201)[1]
    aid = allocation["id"]
    h.advance(20)
    renewed = h.request("POST", f"/v1/allocations/{aid}/renew", body={"ttl_seconds": 30}, expected=200)[1]
    assert renewed["expires_at"].endswith("00:00:50Z")
    h.advance(29)
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Allocated"
    h.advance(1)
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Expired"
    h.request("POST", f"/v1/allocations/{aid}/renew", body={}, expected=409)
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    h.request("DELETE", f"/v1/allocations/{aid}", expected=202)
    capacity = h.get("/v1/capacity")["totals"]
    assert capacity["available"] == capacity["capacity"]
    h.check("leases-and-once-only-return", "Renewed deadline50; expires exactly50; late renew409; no double return")

    h.reset()
    registered, allocated = h.register_and_allocate("restart-intent")
    aid = object_id(allocated, "allocation")
    stage_body = {"action": "create_job", "stage_id": "job", "expected_version": allocated["version"]}
    sock = h.send_without_reading("/x/v1/work-intents/restart-intent/actions", stage_body)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            intent = h.get("/x/v1/work-intents/restart-intent")["intent"]
            if intent["job_ids"]:
                break
            time.sleep(0.05)
        assert len(intent["job_ids"]) == 1
        jid = intent["job_ids"][0]
        h.stop(kill=True)
    finally:
        sock.close()
    h.start()
    # Old expected_version is deliberately retained: durable replay precedes staleness checking.
    replay = h.stage("restart-intent", "create_job", "job", allocated["version"])
    assert object_id(replay, "job") == jid
    assert replay["request_id"] == "lost-stage-response"
    replay_a = h.stage("restart-intent", "allocate", "allocate", registered["version"])
    assert object_id(replay_a, "allocation") == aid
    assert len(h.get("/v1/jobs")["jobs"]) == len(h.get("/v1/allocations")["allocations"]) == 1
    intent = h.get("/x/v1/work-intents/restart-intent")["intent"]
    assert intent["first_arrival_at"] == 0
    h.check("sigkill-after-unread-response", "Persisted job/alloc IDs and stage receipts survive SIGKILL and stale-version replay")
    h.advance(10)
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Succeeded"
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Allocated"
    assert h.get("/x/v1/work-intents/restart-intent")["intent"]["state"] == "completed"
    h.check("normal-model-and-keep-lease", "Modeled job succeeds at10s despite100s timeout; keep_lease remainsAllocated")

    h.reset()
    registered, allocated = h.register_and_allocate("partial-intent", timeout=1)
    aid = object_id(allocated, "allocation")
    h.request("POST", "/v1/jobs", body={"allocation_id": aid, "command": ["work"]}, expected=400)
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Allocated"
    replay_a = h.stage("partial-intent", "allocate", "allocate", registered["version"])
    assert object_id(replay_a, "allocation") == aid
    started = h.stage("partial-intent", "create_job", "job", allocated["version"])
    jid = object_id(started, "job")
    h.advance(1)
    assert h.get(f"/v1/jobs/{jid}")["state"] == "Failed"
    assert h.get(f"/v1/allocations/{aid}")["state"] == "Allocated"
    retry = h.stage("partial-intent", "create_job", "job", allocated["version"])
    assert object_id(retry, "job") == jid
    assert len(h.get("/v1/jobs")["jobs"]) == len(h.get("/v1/allocations")["allocations"]) == 1
    h.check("partial-success-and-timeout", "Invalid job400 retainslease; retry alloc/job createsno duplicates;1s timeoutfails10s work")

    for settlement, timeout, end_state in (("release_on_success", 100, "Succeeded"),
                                           ("release_on_terminal", 1, "Failed")):
        h.reset()
        name = "settlement-" + settlement
        _, allocated = h.register_and_allocate(name, settlement=settlement, timeout=timeout)
        started = h.stage(name, "create_job", "job", allocated["version"])
        h.advance(10 if timeout == 100 else 1)
        assert h.get(f"/v1/jobs/{object_id(started, 'job')}")["state"] == end_state
        assert h.get(f"/v1/allocations/{object_id(allocated, 'allocation')}")["state"] == "Released"
        assert h.get("/v1/capacity")["totals"]["available"]["gpu_count"] == 1
    h.check("explicit-settlement", "release_on_success and release_on_terminal both return exactlyone GPU")


def main():
    started = time.monotonic()
    before = current_build(ROOT)
    failure = None
    h = None
    with tempfile.TemporaryDirectory(prefix="fabric-cp1-acceptance-") as directory:
        h = Harness(directory)
        try:
            run_checks(h)
        except Exception:
            failure = h.redact(traceback.format_exc())
        finally:
            h.stop()
        after = current_build(ROOT)
        if before != after and failure is None:
            failure = "Source tree changed during acceptance; rerun after completing edits."
        history = {"schema_version": "1.0", "scope": SCOPE,
                   "credentials_recorded": False, "records": h.history}
        serialized = json.dumps(history, ensure_ascii=False, indent=2) + "\n"
        assert all(token not in serialized for token in [*h.tokens.values(), h.test_key])
        OUTPUT.mkdir(parents=True, exist_ok=True)
        history_path = OUTPUT / "a_core_http_history.json"
        history_path.write_text(serialized)
        receipt = {
            "schema_version": "1.0", "scope": SCOPE, "checkpoint": "CP1",
            "status": "PASS" if failure is None else "FAIL", "joint_cp1": "NOT_RUN",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "command": "uv run python scripts/accept_cp1_a.py",
            "build": before, "build_after": after, "execution_mode": "logical_simulation",
            "scheduling_mode": "builtin", "elapsed_seconds": round(time.monotonic() - started, 3),
            "checks": h.checks, "failure": failure,
            "evidence": {"path": "evidence/cp1/a_core_http_history.json",
                         "sha256": hashlib.sha256(serialized.encode()).hexdigest()},
            "not_validated": ["B UI and two-operator journeys", "CP2 preemption and recovery",
                              "Real CPU/GPU execution", "External competition acceptance",
                              "CP4 scale and latency targets"],
        }
        (OUTPUT / "a_core_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    if failure:
        print(failure, file=sys.stderr)
        return 1
    print(f"A CP1 backend: {len(h.checks)} checks PASS; joint CP1 NOT_RUN.")
    print(OUTPUT / "a_core_receipt.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
