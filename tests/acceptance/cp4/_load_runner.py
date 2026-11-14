"""Declared CP4 HTTP load and independent ledger/latency evidence review.

No production reducer is imported by the reviewer. The history contains actual
TCP responses; the ledger fold checks every committed resource delta.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import platform
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/acceptance/cp3"))
from check_checkpoint import current_build  # noqa: E402
from cp3_state_replay import read_sql_state  # noqa: E402

TARGET = {"nodes": 128, "physical_gpus": 1024, "owners": 64,
          "jobs": 10000, "commands": 50000, "concurrency": 32}
DIMENSIONS = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")
OPERATIONS = {"allocate", "create_job", "renew", "cancel", "release"}
EXPECTED_STATUS = {"allocate": 201, "create_job": 201, "renew": 200,
                   "cancel": 202, "release": 202, "job_detail": 200}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def artifact(path, root=ROOT):
    path = path.resolve()
    return {"path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def verified(root, ref):
    path = (root / ref["path"]).resolve()
    assert path.is_relative_to((root / "evidence/cp4").resolve()), "Evidence path escape"
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"], str(path)
    return path


def read_json(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return json.load(handle)


def environment():
    values = {"platform": platform.platform(), "cpu_count": os.cpu_count(),
              "python": sys.version, "measurement": "Client-observed real TCP wall time, queueing included"}
    for name, path in {"cpu_max": "/sys/fs/cgroup/cpu.max", "memory_max": "/sys/fs/cgroup/memory.max"}.items():
        try:
            values[name] = Path(path).read_text().strip()
        except OSError:
            values[name] = None
    try:
        quota, period = values["cpu_max"].split()
        values["limited_vcpus"] = int(quota) / int(period)
        values["limited_memory_bytes"] = int(values["memory_max"])
    except (AttributeError, ValueError, TypeError, ZeroDivisionError):
        values["limited_vcpus"] = values["limited_memory_bytes"] = None
    values["reference_4vcpu_8gib"] = (values["limited_vcpus"] == 4
                                      and values["limited_memory_bytes"] == 8 * 1024**3)
    return values


def reference_limits(value):
    """Recompute the claim from recorded kernel controls, not its summary flag."""
    try:
        quota, period = value["cpu_max"].split()
        return (int(period) > 0 and int(quota) == 4 * int(period)
                and int(value["memory_max"]) == 8 * 1024**3)
    except (AttributeError, KeyError, ValueError, TypeError):
        return False


def inventory():
    nodes = []
    for number in range(128):
        capacity = {"cpu_millicores": 32000, "memory_mib": 131072,
                    "ephemeral_storage_mib": 65536, "gpu_count": 8,
                    "gpu_memory_mib": 8 * 24576}
        devices = [{"device_id": f"cp4-n{number:03}-gpu{gpu}",
                    "physical_gpu_id": f"cp4-n{number:03}-gpu{gpu}",
                    "model": "A10", "memory_mib": 24576, "profile": None,
                    "state": "available"} for gpu in range(8)]
        nodes.append({"node_id": f"cp4-n{number:03}", "contributor_owner_id": f"owner-{number % 64:03}",
                      "state": "Ready", "capacity": capacity, "available": capacity,
                      "observed_at": 0.0, "devices": devices, "labels": []})
    return nodes


def quantiles(values):
    values = sorted(values)
    def at(p):
        return values[max(0, math.ceil(len(values) * p) - 1)] if values else None
    return {"count": len(values), "p50_ms": at(.50), "p95_ms": at(.95),
            "p99_ms": at(.99), "max_ms": max(values) if values else None}


def verify_renewal(before, after):
    original = datetime.fromisoformat(before.replace("Z", "+00:00"))
    renewed = datetime.fromisoformat(after.replace("Z", "+00:00"))
    assert (renewed - original).total_seconds() == 300, "Renewal did not extend the actual deadline by 300 seconds"


def complete_query_sample(flows, count, query_jobs, declared_jobs):
    expected = {flow["job_id"] for flow in flows if flow["index"] % 8 == 0}
    return (count == (declared_jobs + 7) // 8 and count == len(query_jobs)
            and query_jobs == expected)


def fold_ledger(nodes, state):
    capacities = {n["node_id"]: n["capacity"] for n in nodes}
    devices = {n["node_id"]: {d["device_id"] for d in n["devices"]} for n in nodes}
    totals = {n: dict.fromkeys(DIMENSIONS, 0) for n in capacities}
    holders, claims, seen = {}, {}, set()
    peak = 0
    for seq, event in enumerate(state["events"], 1):
        assert event["sequence"] == seq and event["event_id"] not in seen, "Event order/identity violation"
        seen.add(event["event_id"])
        for delta in event["resource_deltas"]:
            node, alloc = delta["node_id"], delta["allocation_id"]
            assert node in capacities, "Unknown node"
            claim = claims.setdefault((node, alloc), dict.fromkeys(DIMENSIONS, 0))
            for dim in DIMENSIONS:
                claim[dim] += delta[dim]
                totals[node][dim] += delta[dim]
                assert claim[dim] >= 0 and 0 <= totals[node][dim] <= capacities[node][dim], "Resource oversell/double return"
            for device in delta["device_ids_added"]:
                assert device in devices[node] and (node, device) not in holders, "Physical GPU double allocation"
                holders[node, device] = alloc
            for device in delta["device_ids_removed"]:
                assert holders.get((node, device)) == alloc, "Unowned/double GPU return"
                del holders[node, device]
            peak = max(peak, len(holders))
    final_holders = {}
    final_totals = {n: dict.fromkeys(DIMENSIONS, 0) for n in capacities}
    for alloc in state["allocations"].values():
        placement = alloc.get("placement")
        if placement:
            for piece in placement["slices"]:
                node = piece["node_id"]
                for dim in DIMENSIONS:
                    final_totals[node][dim] += piece["resources"][dim]
                for device in piece["device_ids"]:
                    assert (node, device) not in final_holders
                    final_holders[node, device] = alloc["allocation_id"]
    assert holders == final_holders and totals == final_totals, "Final ledger differs from independent committed-event fold"
    return {"events": len(seen), "peak_held_physical_gpus": peak,
            "final_held_physical_gpus": len(holders), "every_resource_delta_checked": True}


class Load:
    def __init__(self, directory, jobs, deadline_seconds, runtime_directory=None):
        self.directory, self.jobs = directory, jobs
        if runtime_directory is None:
            self.runtime_directory = Path(tempfile.mkdtemp(prefix="fabric-cp4-load-"))
        else:
            self.runtime_directory = runtime_directory.resolve()
            self.runtime_directory.mkdir(parents=True, exist_ok=False)
        self.deadline_seconds = deadline_seconds
        self.lock = threading.Lock()
        self.counter = self.inflight = self.peak_inflight = 0
        self.started = time.monotonic()
        self.timings, self.errors, self.completed = [], [], []
        self.owner_tokens = {f"owner-{i:03}": secrets.token_urlsafe(24) for i in range(64)}
        self.process = self.client = None
        self.history = gzip.open(directory / "http_history.jsonl.gz", "wt")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FABRIC_")}
        auth_path = directory / "private-auth.json"
        write_json(auth_path, {token: {"actor_id": owner, "owner_id": owner, "role": "researcher"}
                              for owner, token in self.owner_tokens.items()})
        auth_path.chmod(0o600)
        write_json(directory / "inventory.json", {"nodes": inventory()})
        self.env.update(FABRIC_DATA_DIR=str(self.runtime_directory), FABRIC_AUTH_FILE=str(auth_path),
                        FABRIC_INVENTORY_FILE=str(directory / "inventory.json"),
                        FABRIC_MODE="logical_simulation", FABRIC_SCHEDULING_MODE="builtin")

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.log = (self.directory / "server.log").open("wb")
        command = [sys.executable, "-m", "uvicorn", "campus_fabric.workflows.devhost:create_app",
                   "--factory", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"]
        self.process = subprocess.Popen(command, cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120,
                                   trust_env=False, limits=httpx.Limits(max_connections=40))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError((self.directory / "server.log").read_text())
            try:
                if self.client.get("/healthz").status_code == 200:
                    self.started = time.monotonic()
                    return
            except httpx.ConnectError:
                pass
            time.sleep(.05)
        raise TimeoutError("Isolated product host did not start")

    def request(self, owner, operation, method, path, body=None, expected=(200,)):
        if time.monotonic() - self.started > self.deadline_seconds:
            raise TimeoutError("Predeclared load wall-time limit exceeded; partial run cannot pass")
        with self.lock:
            self.counter += 1
            request_id = f"cp4-load-{self.counter}"
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
        before = time.perf_counter()
        data, status, error = None, None, None
        try:
            response = self.client.request(method, path, json=body, headers={
                "Authorization": "Bearer " + self.owner_tokens[owner],
                "X-Owner": owner, "X-Request-Id": request_id})
            status = response.status_code
            data = response.json() if response.content else None
            assert status in expected, (operation, status, data)
            return data
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            elapsed = (time.perf_counter() - before) * 1000
            row = {"request_id": request_id, "owner": owner, "operation": operation,
                   "method": method, "path": path, "body": body, "status": status,
                   "response": data, "error": error, "elapsed_ms": elapsed,
                   "started_offset_seconds": before - self.started}
            with self.lock:
                self.inflight -= 1
                self.timings.append({"operation": operation, "elapsed_ms": elapsed})
                self.history.write(json.dumps(row, separators=(",", ":")) + "\n")
                if len(self.timings) in (100, 500, 1000) or len(self.timings) % 5000 == 0:
                    self.history.flush()
                    print(json.dumps({"requests_finished": len(self.timings),
                                      "elapsed_seconds": time.monotonic() - self.started,
                                      "recent": quantiles([x["elapsed_ms"] for x in self.timings[-100:]])}), flush=True)

    def flow(self, index):
        owner = f"owner-{index % 64:03}"
        body = {"owner": owner, "request": {"cpu_millicores": 1000, "memory_mib": 1024,
                "ephemeral_storage_mib": 32, "gpu": {"count": 1, "model": "A10", "memory_mib": 4096}},
                "ttl_seconds": 300}
        alloc = self.request(owner, "allocate", "POST", "/v1/allocations", body, (201,))
        aid = alloc["id"]
        job = self.request(owner, "create_job", "POST", "/v1/jobs", {
            "allocation_id": aid, "image": "cp4-logical-fixed-model", "command": ["work"],
            "timeout_seconds": 100, "max_retries": 0}, (201,))
        jid = job["id"]
        self.request(owner, "renew", "POST", f"/v1/allocations/{aid}/renew", {"ttl_seconds": 600})
        if index % 8 == 0:
            self.request(owner, "job_detail", "GET", f"/v1/jobs/{jid}")
        self.request(owner, "cancel", "DELETE", f"/v1/jobs/{jid}", expected=(202,))
        self.request(owner, "release", "DELETE", f"/v1/allocations/{aid}", expected=(202,))
        with self.lock:
            self.completed.append({"index": index, "owner": owner, "allocation_id": aid, "job_id": jid})

    def run(self):
        with ThreadPoolExecutor(max_workers=32) as pool:
            # A bounded window keeps queued tasks from obscuring the observed 32-request load.
            for start in range(0, self.jobs, 32):
                futures = {pool.submit(self.flow, i): i for i in range(start, min(start + 32, self.jobs))}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        self.errors.append({"index": futures[future], "error": str(exc)})
                if self.errors:
                    break

    def close(self):
        if self.client:
            self.client.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if hasattr(self, "log"):
            self.log.close()
        self.history.close()
        (self.directory / "private-auth.json").unlink(missing_ok=True)


def execute(directory, jobs=10000, deadline_seconds=1200, runtime_directory=None):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    before = current_build(ROOT)
    metadata = {"schema": "cp4-load-v1", "workload_version": "cp4-mixed-lifecycle-v2-effective-renewal",
                "build": before, "started_at": datetime.now(timezone.utc).isoformat(),
                "environment": environment(), "declared": {**TARGET, "jobs": jobs, "commands": jobs * 5},
                "protocol": "Five writes per job: allocate TTL300, create, renew TTL600, cancel, release; one job detail per eight jobs; 32 concurrent flows; no history reset or compaction",
                "deadline_seconds": deadline_seconds, "excluded_workloads": {"clock_batches": "not measured", "fault_injection": "not measured", "policy_experiments": "not measured"},
                "execution": "Real TCP integrated host; logical jobs, not real GPU work"}
    write_json(directory / "run.json", metadata)
    load = Load(directory, jobs, deadline_seconds, runtime_directory)
    metadata["database_directory"] = str(load.runtime_directory)
    metadata["database_storage"] = "Native runtime filesystem, SQLite synchronous=FULL; raw database retained at database_directory"
    write_json(directory / "run.json", metadata)
    try:
        load.start()
        load.run()
    except Exception as exc:
        load.errors.append({"error": str(exc)})
    finally:
        elapsed = time.monotonic() - load.started
        load.close()
    database = load.runtime_directory / "control-plane.sqlite3"
    candidates = list(load.runtime_directory.glob("*.sqlite*"))
    if not database.exists():
        candidates = [p for p in candidates if not p.name.endswith(("-wal", "-shm"))]
        database = candidates[0] if candidates else None
    if database:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            conn.execute("BEGIN")
            state = read_sql_state(conn)
        with gzip.open(directory / "final_state.json.gz", "wt") as handle:
            json.dump(state, handle, separators=(",", ":"))
    metadata.update(build_at_finish=current_build(ROOT), elapsed_seconds=elapsed,
                    completed_flows=load.completed, peak_inflight_requests=load.peak_inflight,
                    errors=load.errors, timings_summary=quantiles([x["elapsed_ms"] for x in load.timings]))
    write_json(directory / "run.json", metadata)
    return metadata


def inspect(root, refs):
    paths = {Path(ref["path"]).name: verified(root, ref) for ref in refs}
    run = read_json(paths["run.json"])
    nodes = read_json(paths["inventory.json"])["nodes"]
    state = read_json(paths["final_state.json.gz"])
    assert state["nodes"] == {n["node_id"]: n for n in nodes}, "Load inventory changed or final topology differs"
    counts, owners, durations, by_operation, errors = Counter(), set(), [], {}, []
    seen, allocation_ids, job_ids, intervals = set(), set(), set(), []
    allocation_expiries, renewed_ids, query_jobs = {}, set(), set()
    with gzip.open(paths["http_history.jsonl.gz"], "rt") as handle:
        for line in handle:
            row = json.loads(line)
            assert row["request_id"] not in seen, "Duplicate request record"
            seen.add(row["request_id"])
            counts[row["operation"]] += 1
            owners.add(row["owner"])
            durations.append(row["elapsed_ms"])
            assert row["elapsed_ms"] >= 0 and math.isfinite(row["elapsed_ms"]), "Invalid latency measurement"
            assert row["started_offset_seconds"] >= 0 and math.isfinite(row["started_offset_seconds"]), "Invalid request start time"
            intervals.extend(((row["started_offset_seconds"], 1),
                              (row["started_offset_seconds"] + row["elapsed_ms"] / 1000, -1)))
            by_operation.setdefault(row["operation"], []).append(row["elapsed_ms"])
            if row["operation"] in ("allocate", "create_job") and row["status"] == 201:
                collection = "allocations" if row["operation"] == "allocate" else "jobs"
                identities = allocation_ids if collection == "allocations" else job_ids
                identity = row["response"]["id"]
                assert identity not in identities, "Two independent creates returned one identity"
                assert state[collection][identity]["owner_id"] == row["owner"], "Created object owner mismatch"
                identities.add(identity)
                if collection == "allocations":
                    assert row["body"]["ttl_seconds"] == 300
                    allocation_expiries[identity] = row["response"]["expires_at"]
            if row["operation"] == "renew" and row["status"] == 200:
                identity = row["response"]["id"]
                assert row["body"]["ttl_seconds"] == 600 and identity not in renewed_ids
                verify_renewal(allocation_expiries[identity], row["response"]["expires_at"])
                assert state["allocations"][identity]["expires_at"] == 600
                renewed_ids.add(identity)
            if row["operation"] == "job_detail" and row["status"] == 200:
                identity = row["response"]["id"]
                assert row["path"] == f"/v1/jobs/{identity}" and identity not in query_jobs
                assert state["jobs"][identity]["owner_id"] == row["owner"]
                query_jobs.add(identity)
            if row["error"] or row["status"] != EXPECTED_STATUS.get(row["operation"]):
                errors.append({"request_id": row["request_id"], "error": row["error"], "status": row["status"]})
    ledger = fold_ledger(nodes, state)
    concurrent = peak = 0
    for _, delta in sorted(intervals):
        concurrent += delta
        peak = max(peak, concurrent)
    assert peak <= run["peak_inflight_requests"], "Raw intervals exceed recorded inflight counter"
    observed = {"nodes": len(nodes), "physical_gpus": len({d["physical_gpu_id"] for n in nodes for d in n["devices"]}),
                "owners": len(owners), "jobs": len(state["jobs"]),
                "commands": sum(counts[k] for k in OPERATIONS), "concurrency": peak}
    completed = len(run["completed_flows"])
    queries_complete = complete_query_sample(run["completed_flows"], counts["job_detail"],
                                             query_jobs, run["declared"]["jobs"])
    assert allocation_ids == set(state["allocations"]) and job_ids == set(state["jobs"]), "HTTP-created identities differ from persisted objects"
    lifecycle = (completed == run["declared"]["jobs"] == len(state["jobs"]) == len(state["allocations"])
                 and all(a["state"] == "Released" and not a["placement"] for a in state["allocations"].values())
                 and all(j["state"] == "Cancelled" for j in state["jobs"].values())
                 and all(counts[k] == completed for k in OPERATIONS)
                 and renewed_ids == allocation_ids and state["now"] == 0 and queries_complete)
    return {"run": run, "observed": observed, "ledger": ledger, "lifecycle_consistent": lifecycle,
            "request_errors": errors, "operation_counts": dict(counts), "ordinary": quantiles(durations),
            "effective_renewals_verified": len(renewed_ids),
            "declared_query_sample_complete": queries_complete,
            "by_operation": {k: quantiles(v) for k, v in by_operation.items()},
            "throughput_requests_per_second": len(durations) / run["elapsed_seconds"],
            "source_unchanged": run["build"] == run["build_at_finish"] == current_build(root)}


def refs_for(directory):
    return [artifact(directory / name) for name in ("run.json", "inventory.json", "http_history.jsonl.gz", "final_state.json.gz")
            if (directory / name).is_file()]
