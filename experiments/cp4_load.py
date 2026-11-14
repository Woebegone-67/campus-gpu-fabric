"""Bounded CP4 load diagnostic over an isolated, real loopback HTTP service.

This is a reproducible public-API workload, not a complete CP4 certification.
The client and server share the observed host; no 4-vCPU/8-GiB claim is inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/acceptance/cp3"))
from accept_cp1_a import Harness  # noqa: E402
from check_checkpoint import current_build  # noqa: E402
from cp3_state_replay import read_sql_state  # noqa: E402

PROFILES = {
    "smoke": {"nodes": 2, "gpus_per_node": 4, "owners": 4, "jobs": 8, "concurrency": 4},
    "pilot": {"nodes": 128, "gpus_per_node": 8, "owners": 64, "jobs": 64, "concurrency": 32},
    "full": {"nodes": 128, "gpus_per_node": 8, "owners": 64, "jobs": 10_000,
             "concurrency": 32},
}
SCALARS = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def make_inventory(nodes, gpus_per_node):
    result = []
    for index in range(nodes):
        node_id = f"cp4-node-{index:03d}"
        devices = []
        for gpu in range(gpus_per_node):
            parent = f"{node_id}-gpu-{gpu}"
            devices.append({"device_id": parent, "physical_gpu_id": parent, "model": "A100",
                            "memory_mib": 40960, "profile": None, "state": "available"})
            # Alternate representations of the same physical GPU are present throughout the run.
            for part in range(2):
                devices.append({"device_id": f"{parent}-slice-{part}", "physical_gpu_id": parent,
                                "model": "A100", "memory_mib": 20480,
                                "profile": "mig-2g.20gb", "state": "available"})
        capacity = {"cpu_millicores": 64000, "memory_mib": 262144,
                    "ephemeral_storage_mib": 262144, "gpu_count": gpus_per_node,
                    "gpu_memory_mib": gpus_per_node * 40960}
        result.append({"node_id": node_id, "contributor_owner_id": "supplier", "state": "Ready",
                       "capacity": capacity, "available": dict(capacity), "observed_at": 0,
                       "devices": devices, "labels": []})
    return {"nodes": result}


def quantile(values, proportion):
    """Nearest-rank percentile; empty groups are explicit null, never zero latency."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * proportion) - 1)]


def latency_summary(records, elapsed):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["category"]].append(record)
        grouped[f'{record["category"]}:{record["operation"]}'].append(record)
    result = {}
    for group, rows in grouped.items():
        values = [row["elapsed_ms"] for row in rows]
        result[group] = {"samples": len(rows), "successful": sum(r["ok"] for r in rows),
                         "errors": sum(not r["ok"] for r in rows),
                         "p50_ms": quantile(values, .5), "p95_ms": quantile(values, .95),
                         "p99_ms": quantile(values, .99),
                         "throughput_requests_per_second": len(rows) / elapsed if elapsed else None}
    return result


def check_state(state):
    """Independent read-only claim accounting; does not call the kernel's validator.

    Checks coherent committed snapshots, not every concurrent linearization prefix.
    """
    nodes = state["nodes"]
    devices = {device["device_id"]: (node_id, device)
               for node_id, node in nodes.items() for device in node["devices"]}
    parents = {key for key, (_, device) in devices.items() if device["profile"] is None}
    if len(parents) != sum(node["capacity"]["gpu_count"] for node in nodes.values()):
        raise ValueError("Physical GPU count differs from parent inventory")
    claimed = {}
    used = defaultdict(Counter)
    per_parent = defaultdict(list)
    for aid, allocation in state["allocations"].items():
        placement = allocation.get("placement")
        active = allocation["state"] in {"Allocated", "Running", "PreemptRequested"}
        if placement and not active:
            raise ValueError(f"Terminal/nonholding lease retains placement: {aid}")
        if active and not placement:
            raise ValueError(f"Active lease has no placement: {aid}")
        if not placement:
            continue
        for part in placement["slices"]:
            node_id = part["node_id"]
            if node_id not in nodes:
                raise ValueError("Claim refers to unknown node")
            for key in SCALARS:
                value = part["resources"][key]
                # Validate each claim before summation: negative claims must never
                # cancel another claim's overcommit, and bool is not an integer resource.
                if type(value) is not int or value < 0:
                    raise ValueError(f"Invalid scalar claim for {aid}/{node_id}/{key}: {value!r}")
                used[node_id][key] += value
            for did in part["device_ids"]:
                if did in claimed or did not in devices or devices[did][0] != node_id:
                    raise ValueError("Duplicate or unknown GPU claim")
                claimed[did] = aid
                per_parent[devices[did][1]["physical_gpu_id"]].append(devices[did][1])
    for node_id, resources in used.items():
        if any(value < 0 or value > nodes[node_id]["capacity"][key]
               for key, value in resources.items()):
            raise ValueError("Scalar resources overcommitted")
    for parent, claims in per_parent.items():
        if parent not in parents:
            raise ValueError("Claim has no physical parent")
        if any(item["profile"] is None for item in claims) and len(claims) > 1:
            raise ValueError("Whole GPU overlaps a MIG slice")
        if sum(item["memory_mib"] for item in claims) > devices[parent][1]["memory_mib"]:
            raise ValueError("GPU memory overcommitted")
    released = Counter()
    for event in state["events"]:
        if event["event_type"] == "allocation.released":
            released.update(ref["entity_id"] for ref in event["entities"]
                            if ref["kind"] == "allocation")
    if any(value != 1 for value in released.values()):
        raise ValueError("Repeated release effect")
    return {"version": {"epoch": state["epoch"], "revision": state["revision"]},
            "physical_gpus": len(parents), "device_representations": len(devices),
            "allocations": len(state["allocations"]), "jobs": len(state["jobs"]),
            "claimed_devices": len(claimed), "claimed_physical_gpus": len(per_parent),
            "allocation_states": dict(Counter(a["state"] for a in state["allocations"].values())),
            "job_states": dict(Counter(j["state"] for j in state["jobs"].values())),
            "released_effects": sum(released.values()), "state_sha256": digest(state)}


class LoadHarness(Harness):
    def __init__(self, directory, profile, journal, request_timeout):
        super().__init__(directory, app_target="campus_fabric.workflows.devhost:create_app")
        # The evidence build must be the code served, irrespective of the caller's shell.
        self.env["PYTHONPATH"] = os.pathsep.join(map(str, (ROOT / "src", ROOT, ROOT / "scripts")))
        auth_path = self.directory / "auth.json"
        auth = json.loads(auth_path.read_text())
        for index in range(profile["owners"]):
            alias = f"owner-{index:03d}"
            self.tokens[alias] = secrets.token_urlsafe(24)
            self.owners[alias] = alias
            auth[self.tokens[alias]] = {"actor_id": f"cp4-{alias}", "owner_id": alias,
                                        "role": "researcher"}
        auth_path.write_text(json.dumps(auth))
        inventory = make_inventory(profile["nodes"], profile["gpus_per_node"])
        (self.directory / "inventory.json").write_text(json.dumps(inventory))
        self.profile = profile
        self.journal = journal
        self.samples = []
        self.request_timeout = request_timeout
        self.stop_load = threading.Event()
        self.deadline = float("inf")
        self.active_sequences = 0
        self.peak_active_sequences = 0
        self.active_http_requests = 0
        self.peak_http_requests = 0
        self.active_http_commands = 0
        self.peak_http_commands = 0
        self.completed_sequences = 0
        self.effective_renewals = 0
        self.sequence_failures = []

    def measured(self, method, path, *, operation, category, actor, expected, body=None):
        if category not in {"validation", "cleanup"}:
            if time.monotonic() >= self.deadline:
                self.stop_load.set()
                raise TimeoutError("Load admission wall-time budget exhausted")
            if self.stop_load.is_set():
                raise RuntimeError("Load admission stopped after an earlier failure or stop signal")
        with self.lock:
            self.request_counter += 1
            request_id = f"cp4-load-{self.request_counter}"
            started = time.monotonic()
            self.active_http_requests += 1
            self.active_http_commands += category == "command"
            self.peak_http_requests = max(self.peak_http_requests, self.active_http_requests)
            self.peak_http_commands = max(self.peak_http_commands, self.active_http_commands)
            requests_at_start = self.active_http_requests
            commands_at_start = self.active_http_commands
        data = None
        error = None
        status = None
        try:
            try:
                response = self.client.request(method, path, json=body,
                    headers={**self.headers(actor), "X-Request-Id": request_id},
                    timeout=self.request_timeout)
            finally:
                # Count overlapping client HTTP calls, not idle lifecycle threads or
                # subsequent JSON parsing/journal writes. Failed calls also leave the window.
                with self.lock:
                    http_finished = time.monotonic()
                    self.active_http_requests -= 1
                    self.active_http_commands -= category == "command"
            status = response.status_code
            data = response.json() if response.content else None
            ok = status in expected
            if not ok:
                error = f"Unexpected HTTP {status}: {self.redact(data)}"
        except Exception as exc:
            ok = False
            error = self.redact(f"{type(exc).__name__}: {exc}")
        elapsed = (time.monotonic() - started) * 1000
        record = {"request_id": request_id, "method": method, "path": path, "actor": actor,
                  "operation": operation, "category": category, "request_body": body,
                  "started_monotonic": started, "http_finished_monotonic": http_finished,
                  "http_requests_in_flight_at_start": requests_at_start,
                  "http_commands_in_flight_at_start": commands_at_start,
                  "elapsed_ms": elapsed, "status": status,
                  "ok": ok, "response_sha256": digest(data), "error": error,
                  "response_identity": {key: data[key] for key in ("id", "owner", "state", "version", "expires_at")
                                        if isinstance(data, dict) and key in data}}
        with self.lock:
            self.samples.append(record)
            self.journal.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.journal.flush()
        if not ok:
            self.stop_load.set()
            raise RuntimeError(error)
        return data

    def sequence(self, index):
        owner = f'owner-{index % self.profile["owners"]:03d}'
        aid = None
        released = False
        with self.lock:
            self.active_sequences += 1
            self.peak_active_sequences = max(self.peak_active_sequences, self.active_sequences)
        try:
            allocation = self.measured("POST", "/v1/allocations", operation="allocation.create",
                category="command", actor=owner, expected=(201,), body={"owner": owner,
                    "request": {"cpu_millicores": 1000, "memory_mib": 1024,
                                "ephemeral_storage_mib": 32,
                                "gpu": {"count": 1, "model": "A100", "memory_mib": 20480}},
                    "ttl_seconds": 300})
            aid = allocation["id"]
            job = self.measured("POST", "/v1/jobs", operation="job.create", category="command",
                actor=owner, expected=(201, 202), body={"allocation_id": aid,
                    "image": "cp4-logical-fixed-model", "command": ["work"],
                    "timeout_seconds": 100, "max_retries": 0})
            jid = job["id"]
            view = self.measured("GET", f"/v1/jobs/{jid}", operation="job.get",
                category="query", actor=owner, expected=(200,))
            if view["owner"] != owner or view["allocation_id"] != aid:
                raise ValueError("Job ownership/binding mismatch")
            self.measured("DELETE", f"/v1/jobs/{jid}", operation="job.cancel", category="command",
                          actor=owner, expected=(200, 202, 204))
            renewed = self.measured("POST", f"/v1/allocations/{aid}/renew", operation="allocation.renew",
                          category="command", actor=owner, expected=(200,), body={"ttl_seconds": 600})
            expected_expiry = datetime.fromisoformat(allocation["expires_at"]) + timedelta(seconds=300)
            if (renewed["id"] != aid or renewed["owner"] != owner or
                    datetime.fromisoformat(renewed["expires_at"]) != expected_expiry):
                raise ValueError("Renewal did not extend the same lease deadline by 300 seconds")
            with self.lock:
                self.effective_renewals += 1
            self.measured("DELETE", f"/v1/allocations/{aid}", operation="allocation.release",
                          category="command", actor=owner, expected=(200, 202, 204))
            released = True
            view = self.measured("GET", f"/v1/allocations/{aid}", operation="allocation.get",
                category="query", actor=owner, expected=(200,))
            if (view["owner"] != owner or view["state"] != "Released" or
                    datetime.fromisoformat(view["expires_at"]) != expected_expiry):
                raise ValueError("Released allocation does not match owner/state/renewed deadline")
            with self.lock:
                self.completed_sequences += 1
        except Exception as exc:
            self.stop_load.set()
            with self.lock:
                self.sequence_failures.append({"index": index, "owner": owner,
                                               "error": self.redact(f"{type(exc).__name__}: {exc}")})
        finally:
            # Cleanup is separate from measured command counts and cannot hide a failed run.
            if aid and not released:
                try:
                    self.measured("DELETE", f"/v1/allocations/{aid}",
                        operation="allocation.release", category="cleanup", actor=owner,
                        expected=(200, 202, 204))
                except Exception:
                    pass  # The measured cleanup failure is retained in the journal.
            with self.lock:
                self.active_sequences -= 1


def read_state(directory):
    """Rebuild a single read-only SQL snapshot independently of production storage."""
    path = Path(directory) / "database/fabric.sqlite3"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as connection:
        connection.execute("BEGIN")
        state = read_sql_state(connection)
        if not isinstance(state, dict):
            raise ValueError("No authoritative state is available for the load observation")
        return state


def write_ledger_snapshot(output, label, state, harness):
    """Preserve a small fixed number of raw states for independent later inspection."""
    serialized = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2,
                            allow_nan=False) + "\n"
    if any(secret in serialized for secret in [*harness.tokens.values(), harness.test_key]):
        raise RuntimeError("Refusing to export ledger snapshot containing credentials")
    path = output / f"ledger-{label}.json"
    path.write_text(serialized, encoding="utf-8")
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "state_sha256": digest(state),
            "version": {"epoch": state["epoch"], "revision": state["revision"]}}


def run_load(output, *, profile_name="smoke", max_wall_seconds=120, request_timeout=15):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    profile = dict(PROFILES[profile_name])
    before = current_build(ROOT)
    began = time.monotonic()
    failure = None
    checks = []
    h = None
    observed = None
    load_elapsed = 0.0
    ledger_snapshots = {}
    snapshot_errors = []
    with tempfile.TemporaryDirectory(prefix="fabric-cp4-load-") as runtime:
        with (output / "requests.jsonl").open("w") as journal:
            def observe(label=None):
                state = read_state(runtime)
                if label:
                    ledger_snapshots[label] = write_ledger_snapshot(output, label, state, h)
                try:
                    return check_state(state)
                except Exception:
                    # Preserve the exact state that failed, not a later reread after more commands.
                    ledger_snapshots["failure"] = write_ledger_snapshot(output, "failure", state, h)
                    raise

            try:
                h = LoadHarness(runtime, profile, journal, request_timeout)
                h.start()
                capacity = h.measured("GET", "/v1/capacity", operation="capacity.get",
                    category="validation", actor="admin", expected=(200,))
                physical = profile["nodes"] * profile["gpus_per_node"]
                if capacity["totals"]["available"]["gpu_count"] != physical:
                    raise ValueError("Initial HTTP capacity does not count physical GPUs once")
                (output / "initial-capacity.json").write_text(json.dumps(capacity, indent=2) + "\n")
                checks.append({"elapsed_seconds": 0.0, **observe("initial")})
                load_started = time.monotonic()
                h.deadline = load_started + max_wall_seconds
                next_index = 0
                last_observer = 0.0
                with ThreadPoolExecutor(max_workers=profile["concurrency"]) as pool:
                    pending = set()
                    while pending or next_index < profile["jobs"]:
                        if time.monotonic() >= h.deadline:
                            h.stop_load.set()
                        while (len(pending) < profile["concurrency"] and
                               next_index < profile["jobs"] and not h.stop_load.is_set()):
                            pending.add(pool.submit(h.sequence, next_index))
                            next_index += 1
                        if not pending:
                            break
                        done, pending = wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                        for future in done:
                            future.result()
                        if time.monotonic() - last_observer >= 2:
                            observed = observe()
                            checks.append({"elapsed_seconds": time.monotonic() - load_started,
                                           **observed})
                            last_observer = time.monotonic()
                load_elapsed = time.monotonic() - load_started
                observed = observe("final")
                checks.append({"elapsed_seconds": load_elapsed, **observed})
                final = h.measured("GET", "/v1/capacity", operation="capacity.get",
                    category="validation", actor="admin", expected=(200,))
                (output / "final-capacity.json").write_text(json.dumps(final, indent=2) + "\n")
                if final["totals"]["available"] != final["totals"]["capacity"]:
                    raise ValueError("Final resources were not fully returned")
                if observed["claimed_devices"]:
                    raise ValueError("Final authoritative state retains claims")
                if not h.sequence_failures and h.completed_sequences == profile["jobs"]:
                    expected = profile["jobs"]
                    if (observed["jobs"] != expected or observed["allocations"] != expected or
                            observed["job_states"] != {"Cancelled": expected} or
                            observed["allocation_states"] != {"Released": expected} or
                            observed["released_effects"] != expected):
                        raise ValueError("Final ledger counts/states differ from completed lifecycles")
                if h.completed_sequences != profile["jobs"] or h.sequence_failures:
                    failure = "Workload stopped before all requested successful lifecycles completed"
                    ledger_snapshots["failure"] = write_ledger_snapshot(
                        output, "failure", read_state(runtime), h)
            except Exception:
                failure = h.redact(traceback.format_exc()) if h else traceback.format_exc()
                if h and "failure" not in ledger_snapshots:
                    try:
                        ledger_snapshots["failure"] = write_ledger_snapshot(
                            output, "failure", read_state(runtime), h)
                    except Exception as exc:
                        snapshot_errors.append(h.redact(f"{type(exc).__name__}: {exc}"))
            finally:
                if h:
                    h.stop_load.set()
                    h.stop()
                    (output / "server.log").write_text(h.redact(
                        (Path(runtime) / "server.log").read_text(errors="replace")))
        (output / "inventory.json").write_text(json.dumps(
            make_inventory(profile["nodes"], profile["gpus_per_node"]), indent=2) + "\n")
    after = current_build(ROOT)
    samples = h.samples if h else []
    if before != after:
        failure = (failure + "; " if failure else "") + "Build changed during run; evidence is diagnostic only"
    commands = [row for row in samples if row["category"] == "command"]
    created_jobs = sum(row["operation"] == "job.create" and row["ok"] for row in commands)
    successful_commands = sum(row["ok"] for row in commands)
    metrics = latency_summary(samples, load_elapsed)
    command_peak = h.peak_http_commands if h else 0
    concurrency_met = command_peak >= PROFILES["full"]["concurrency"]
    measured_target = all(metrics.get(group, {}).get("samples", 0) and
                          metrics[group]["errors"] == 0 and metrics[group]["p95_ms"] < 200
                          for group in ("query", "command"))
    record = {"schema_version": "1.0", "scope": "CP4_PUBLIC_API_LOAD_DIAGNOSTIC",
        "status": "PASS" if failure is None else "FAIL", "profile": profile_name,
        "build": before, "build_after": after, "observed_at": datetime.now(UTC).isoformat(),
        "requested": {**profile, "physical_gpus": profile["nodes"] * profile["gpus_per_node"],
                      "commands": profile["jobs"] * 5, "queries": profile["jobs"] * 2},
        "actual": {"jobs_created": created_jobs, "successful_commands": successful_commands,
                   "command_attempts": len(commands), "completed_lifecycles": h.completed_sequences if h else 0,
                   "effective_renewals": h.effective_renewals if h else 0,
                   "peak_client_lifecycles": h.peak_active_sequences if h else 0,
                   "peak_http_requests_in_flight": h.peak_http_requests if h else 0,
                   "peak_http_commands_in_flight": command_peak,
                   "final_snapshot": observed},
        "workload": {"version": "effective-renewal-v2", "initial_ttl_seconds": 300,
            "renewed_ttl_seconds": 600, "expected_extension_seconds": 300, "seed": 0, "owner_assignment": "job_index modulo owner_count",
            "command_mix": {"allocation.create": .2, "job.create": .2, "job.cancel": .2,
                            "allocation.renew": .2, "allocation.release": .2},
            "scheduling_mode": "builtin", "http_host": "B devhost with A core and B query projections",
            "execution_mode": "logical_simulation", "clock_advances": 0,
            "jobs_are_cumulative": True, "max_concurrent_lifecycles": profile["concurrency"],
            "mig_workload": "MIG aliases present; requests use whole GPUs; no MIG slice claims tested"},
        "environment": {"platform": platform.platform(), "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(), "python": platform.python_version(),
            "reference_4vcpu_8gib_enforced": False, "client_and_server_share_host": True,
            "other_host_processes_isolated": False},
        "limits": {"admission_wall_seconds": max_wall_seconds, "per_request_timeout_seconds": request_timeout,
            "note": "in-flight requests and separately recorded cleanup may drain after admission deadline"},
        "elapsed_seconds": time.monotonic() - began, "load_elapsed_seconds": load_elapsed,
        "latency": metrics,
        "queue": {"client_outstanding_lifecycles_bound": profile["concurrency"],
                  "backend_queue_length": None, "backend_queue_status": "NOT_OBSERVABLE"},
        "cp4_concurrency": {"required_command_http_peak": PROFILES["full"]["concurrency"],
            "observed_command_http_peak": command_peak,
            "status": "MET" if concurrency_met else "NOT_MET",
            "measurement": "overlapping client HTTP calls; excludes JSON decoding and journal writes; "
                           "includes client connection-pool wait; not simultaneous server handlers"},
        "cp4_scale": "MET_FOR_DECLARED_WORKLOAD" if (
            profile_name == "full" and not failure and concurrency_met) else "NOT_MET",
        "observed_host_p95_below_200ms": bool(measured_target),
        "cp4_reference_latency": "NOT_RUN", "joint_cp4": "NOT_RUN",
        "failure": failure, "sequence_failures": h.sequence_failures if h else [],
        "ledger_snapshots": ledger_snapshots, "ledger_snapshot_errors": snapshot_errors,
        "invariant_snapshot_count": len(checks),
        "not_validated": ["4 vCPU / 8 GiB controlled reference host", "server queue length",
            "every concurrent authoritative prefix", "MIG slice workload", "other command mixes",
            "10,000 simultaneous active jobs", "UI and independent human acceptance"]}
    (output / "invariant-snapshots.json").write_text(json.dumps(checks, indent=2) + "\n")
    record["evidence"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(output.iterdir()) if path.is_file()}
    serialized = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if h and any(secret in serialized for secret in [*h.tokens.values(), h.test_key]):
        raise RuntimeError("Refusing to export credentials")
    (output / "summary.json").write_text(serialized)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwrite")
    parser.add_argument("--profile", choices=PROFILES, default="smoke")
    parser.add_argument("--max-wall-seconds", type=float, default=120)
    parser.add_argument("--request-timeout", type=float, default=15)
    args = parser.parse_args()
    if not 0 < args.max_wall_seconds <= 86400 or not 0 < args.request_timeout <= 120:
        parser.error("Use finite positive wall budget <=86400 and request timeout <=120")
    result = run_load(args.output, profile_name=args.profile, max_wall_seconds=args.max_wall_seconds,
                      request_timeout=args.request_timeout)
    print(json.dumps({"status": result["status"], "actual": result["actual"],
                      "cp4_scale": result["cp4_scale"], "cp4_reference_latency": "NOT_RUN",
                      "output": str(args.output.resolve())}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
