"""Durable dispatch and real process evidence for a fixed CPU reference task.

The command kernel remains the sole resource authority. This adapter owns only
process identity, dispatch receipts and private artifacts. An unconfirmed process
is never reported stopped. Restart reconciliation is conservative: it requests
cooperative stop through its nonce-bound control file, rather than killing a PID
whose identity may have been reused.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from campus_fabric.contracts import models as m

from .cpu_files import advance, atomic_json, canonical, read_signed, signed, workload_id


class ExecutorRejected(ValueError):
    """Dispatch/artifact cannot safely identify a legal reference execution."""


@dataclass(frozen=True)
class CpuReferenceConfig:
    total_steps: int = 1_000_000
    batch_steps: int = 2_048
    batch_pause_seconds: float = 0.002
    seed: int = 17
    stop_timeout_seconds: float = 1.0

    def __post_init__(self):
        for name in ("total_steps", "batch_steps", "seed"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if not 1 <= self.total_steps <= 10_000_000 or not 1 <= self.batch_steps <= 100_000:
            raise ValueError("Reference computation is out of bounds")
        if not 0 <= self.seed < (1 << 64):
            raise ValueError("Seed is out of bounds")
        for name, lower, upper in (
            ("batch_pause_seconds", 0, 0.1),
            ("stop_timeout_seconds", 0.01, 30),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{name} is out of bounds")


class CpuReferenceExecutor:
    def __init__(self, data_dir: str | Path, config: CpuReferenceConfig | None = None):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = config or CpuReferenceConfig()
        self._thread_lock = threading.RLock()
        self._handles: dict[str, subprocess.Popen] = {}
        self._key_path = self.root / "secret.key"
        with self._locked():
            if not self._key_path.exists():
                descriptor = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
            if self._key_path.is_symlink() or len(self._key_path.read_bytes()) != 32:
                raise ExecutorRejected("Invalid local executor key")
            self.key = self._key_path.read_bytes()
            if not self._journal_path.exists():
                self._save(
                    {
                        "schema": 1,
                        "instance": secrets.token_hex(12),
                        "attempts": {},
                        "dispatches": {},
                        "events": [],
                        "artifacts": {},
                        "sequence": 0,
                    }
                )

    @property
    def _journal_path(self):
        return self.root / "journal.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock, (self.root / "executor.lock").open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _read(self):
        return json.loads(self._journal_path.read_bytes())

    def _save(self, state):
        atomic_json(self._journal_path, state)

    @staticmethod
    def _attempt_key(epoch, attempt):
        return hashlib.sha256(canonical({"epoch": epoch, "attempt": attempt})).hexdigest()[:40]

    def _directory(self, record):
        return self.root / "attempts" / record["key"]

    def _sync_controls(self, record):
        for index, control in enumerate(record.get("controls", [])):
            path = self._directory(record) / f"control-{index:010d}.json"
            if not path.exists():
                atomic_json(path, signed(control, self.key))

    def _fence_unstarted_launch(self, record):
        """Cancel a reserved launch only while no worker can execute its workload.

        A protocol-2 worker holds this lock for its entire run and checks the
        durable cancellation marker before publishing started.json. Thus even
        a child delayed between Popen and Python startup cannot run after the
        reservation is retired. Legacy launches remain conservatively unknown.
        """
        if (
            record.get("launch_protocol") != 2
            or record.get("pid") is not None
            or record["key"] in self._handles
            or record["status"] not in {"launching", "stopping"}
        ):
            return
        directory = self._directory(record)
        with (directory / "launch.lock").open("a+b") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                if (directory / "started.json").exists():
                    return
                identity = {
                    name: record[name] for name in ("owner_id", "epoch", "attempt", "token")
                }
                atomic_json(directory / "launch-cancelled.json", signed(identity, self.key))
                record.update(
                    status="launch_failed",
                    launch_error="unstarted_launch_fenced",
                    stop_confirmed=True,
                )
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _alive(self, record):
        handle = self._handles.get(record["key"])
        if handle is not None:
            return handle.poll() is None
        pid = record.get("pid")
        if not pid:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        # A dead, unreaped process cannot execute or retain the simulated claim.
        try:
            probe = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if probe.returncode == 0 and probe.stdout.strip().startswith("Z"):
            return False
        return True if probe.returncode == 0 else None

    def _check_identity(self, payload, record):
        for key in ("owner_id", "epoch", "attempt", "token"):
            if payload.get(key) != record[key]:
                raise ExecutorRejected(f"Execution artifact {key} mismatch")
        if payload.get("workload_id") != record["workload_id"]:
            raise ExecutorRejected("Execution workload mismatch")

    def _register_artifact(self, state, record, path, kind):
        payload = read_signed(path, self.key)
        self._check_identity(payload, record)
        if payload.get("kind") != kind:
            raise ExecutorRejected("Artifact kind mismatch")
        position = payload.get("state", {})
        if (
            type(position.get("step")) is not int
            or not 0 <= position["step"] <= record["config"]["total_steps"]
        ):
            raise ExecutorRejected("Invalid checkpoint position")
        if type(position.get("value")) is not int or not 0 <= position["value"] < (1 << 64):
            raise ExecutorRejected("Invalid checkpoint accumulator")
        if kind == "result" and position["step"] != record["config"]["total_steps"]:
            raise ExecutorRejected("Incomplete CPU result")
        relative = path.relative_to(self.root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact_id = hashlib.sha256((relative + digest).encode()).hexdigest()
        uri = f"fabric-cpu://{state['instance']}/{artifact_id}"
        state["artifacts"][uri] = {
            "path": relative,
            "sha256": digest,
            "kind": kind,
            "record_key": record["key"],
        }
        return uri, payload

    def _artifact(self, state, uri, owner_id, epoch, kind=None):
        entry = state["artifacts"].get(uri)
        if entry is None or (kind and entry["kind"] != kind):
            raise ExecutorRejected("Unknown or incompatible executor artifact")
        path = self.root / entry["path"]
        if not path.resolve().is_relative_to(self.root) or path.is_symlink():
            raise ExecutorRejected("Artifact path escapes the private executor directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ExecutorRejected("Registered execution artifact changed")
        record = state["attempts"][entry["record_key"]]
        if record["owner_id"] != owner_id or record["epoch"] != epoch:
            raise ExecutorRejected("Artifact owner or epoch mismatch")
        payload = read_signed(path, self.key)
        self._check_identity(payload, record)
        return payload, record

    def read_artifact(self, uri: str, *, owner_id: str, epoch: int) -> dict:
        """Caller must first authenticate; this also enforces stored ownership."""
        with self._locked():
            payload, _ = self._artifact(self._read(), uri, owner_id, epoch)
            return {name: value for name, value in payload.items() if name != "token"}

    def dispatch(self, command: m.ExecutorCommand, *, owner_id: str, logical_time: float) -> None:
        if command.mode != "cpu_reference" or not owner_id:
            raise ExecutorRejected("A CPU dispatch requires an authoritative owner and CPU mode")
        if not math.isfinite(logical_time) or logical_time < 0:
            raise ExecutorRejected("Invalid dispatch logical time")
        raw = command.model_dump(mode="json")
        fingerprint = hashlib.sha256(canonical({"command": raw, "owner_id": owner_id})).hexdigest()
        key = self._attempt_key(command.version.epoch, raw["attempt"])
        with self._locked():
            state = self._read()
            previous = state["dispatches"].get(command.dispatch_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ExecutorRejected("Dispatch ID reused with different execution parameters")
                self._sync_controls(state["attempts"][previous["key"]])
                return
            record = state["attempts"].get(key)
            if command.action in ("start", "resume"):
                if record:
                    raise ExecutorRejected(
                        "Attempt already dispatched; reconcile instead of relaunching"
                    )
                if any(
                    other["epoch"] > command.version.epoch for other in state["attempts"].values()
                ):
                    raise ExecutorRejected("Cannot start an older execution epoch")
                initial = {"step": 0, "value": self.config.seed}
                resumed_from = None
                if command.action == "resume":
                    payload, source = self._artifact(
                        state,
                        command.checkpoint.artifact_uri,
                        owner_id,
                        command.version.epoch,
                        "checkpoint",
                    )
                    for field in ("intent_id", "allocation_id", "job_id"):
                        if source["attempt"][field] != raw["attempt"][field]:
                            raise ExecutorRejected("Checkpoint belongs to a different logical work")
                    if command.attempt.generation <= source["attempt"]["generation"]:
                        raise ExecutorRejected("Resume requires a new execution generation")
                    if self._alive(source) is not False or not source.get("stop_confirmed"):
                        raise ExecutorRejected("Checkpoint source has not been confirmed stopped")
                    if source["workload_id"] != workload_id(asdict(self.config)):
                        raise ExecutorRejected("Resume workload configuration changed")
                    initial = payload["state"]
                    resumed_from = command.checkpoint.artifact_uri
                for other in state["attempts"].values():
                    same_job = other["attempt"]["job_id"] == raw["attempt"]["job_id"]
                    old_epoch = other["epoch"] != command.version.epoch
                    if (same_job or old_epoch) and not other.get("stop_confirmed"):
                        raise ExecutorRejected(
                            "Previous or old-epoch execution is not safely stopped"
                        )
                    if same_job and not old_epoch:
                        if other["owner_id"] != owner_id:
                            raise ExecutorRejected("Execution owner changed within a logical job")
                        if command.attempt.generation <= other["attempt"]["generation"]:
                            raise ExecutorRejected("Execution generation must strictly increase")
                record = {
                    "key": key,
                    "owner_id": owner_id,
                    "epoch": command.version.epoch,
                    "attempt": raw["attempt"],
                    "token": secrets.token_hex(24),
                    "config": asdict(self.config),
                    "workload_id": workload_id(asdict(self.config)),
                    "created_monotonic_ns": time.monotonic_ns(),
                    "logical_started_at": logical_time,
                    "version": raw["version"],
                    "pid": None,
                    "status": "launching",
                    "launch_protocol": 2,
                    "stop_confirmed": False,
                    "reported": [],
                    "checkpoint_requests": [],
                    "controls": [],
                    "initial": initial,
                    "resumed_from": resumed_from,
                }
                state["attempts"][key] = record
                directory = self._directory(record)
                directory.mkdir(parents=True, mode=0o700)
                manifest = {
                    name: record[name]
                    for name in (
                        "owner_id",
                        "epoch",
                        "attempt",
                        "token",
                        "config",
                        "initial",
                        "resumed_from",
                        "launch_protocol",
                    )
                }
                atomic_json(directory / "manifest.json", signed(manifest, self.key))
                state["dispatches"][command.dispatch_id] = {"fingerprint": fingerprint, "key": key}
                self._save(state)  # Reserve the launch before the external side effect.
                try:
                    with (directory / "process.log").open("ab") as log:
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                str(Path(__file__).with_name("cpu_worker.py")),
                                str(directory / "manifest.json"),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            cwd=directory,
                            start_new_session=True,
                            close_fds=True,
                        )
                    self._handles[key] = process
                    record.update(pid=process.pid, status="starting")
                except OSError as error:
                    record.update(
                        status="launch_failed",
                        launch_error=type(error).__name__,
                        stop_confirmed=True,
                    )
                self._save(state)
                return
            if record is None or record["owner_id"] != owner_id:
                raise ExecutorRejected("Unknown execution attempt or wrong owner")
            if command.action in ("checkpoint", "stop"):
                control = {
                    "action": command.action,
                    "token": record["token"],
                    "dispatch_id": command.dispatch_id,
                }
                if command.action == "checkpoint":
                    if record["status"] not in ("starting", "running"):
                        raise ExecutorRejected("Checkpoint requires a live CPU attempt")
                    filename = (
                        "checkpoint-"
                        + hashlib.sha256(command.dispatch_id.encode()).hexdigest()
                        + ".json"
                    )
                    control["filename"] = filename
                    record["checkpoint_requests"].append(
                        {"dispatch_id": command.dispatch_id, "filename": filename}
                    )
                else:
                    record.setdefault("stop_requested_ns", time.monotonic_ns())
                    record["status"] = "stopping"
                record.setdefault("controls", []).append(control)
            elif command.action == "probe":
                record["probe_requested"] = True
            state["dispatches"][command.dispatch_id] = {"fingerprint": fingerprint, "key": key}
            self._save(state)
            self._sync_controls(record)

    def _event(self, state, record, marker, kind, logical_time, **extra):
        if marker in record["reported"]:
            return
        state["sequence"] += 1
        callback = m.ExecutorEvent(
            callback_id=f"cpu-{state['instance']}-{state['sequence']}",
            version=m.VersionStamp(**record["version"]),
            attempt=m.AttemptRef(**record["attempt"]),
            kind=kind,
            mode="cpu_reference",
            observed_at=logical_time,
            physical_elapsed_seconds=max(
                0, (time.monotonic_ns() - record["created_monotonic_ns"]) / 1e9
            ),
            **extra,
        )
        state["events"].append(
            {
                "sequence": state["sequence"],
                "acked": False,
                "event": callback.model_dump(mode="json"),
            }
        )
        record["reported"].append(marker)

    def _observe_record(self, state, record, logical_time):
        if record["stop_confirmed"] and "terminal" in record["reported"]:
            return
        self._fence_unstarted_launch(record)
        self._sync_controls(record)
        directory = self._directory(record)
        if (directory / "started.json").exists():
            started = read_signed(directory / "started.json", self.key)
            self._check_identity(started, record)
            if record["pid"] is None:
                record["pid"] = started["pid"]
            if record["status"] in ("launching", "starting"):
                record["status"] = "running"
            checkpoint = None
            if record["resumed_from"]:
                checkpoint = m.CheckpointFact(
                    checkpoint_id="cpu-applied-" + record["key"],
                    report_status="ready",
                    reported_at=record["logical_started_at"],
                    artifact_uri=record["resumed_from"],
                    verification_status="verified",
                    verifier_id="cpu-reference-hmac-v1",
                    verified_at=logical_time,
                    continuation_status="applied",
                    resumed_attempt_id=record["attempt"]["attempt_id"],
                )
            self._event(state, record, "started", "started", logical_time, checkpoint=checkpoint)
        for request in record["checkpoint_requests"]:
            path = directory / request["filename"]
            marker = "checkpoint:" + request["dispatch_id"]
            if path.exists() and marker not in record["reported"]:
                uri, _ = self._register_artifact(state, record, path, "checkpoint")
                checkpoint = m.CheckpointFact(
                    checkpoint_id="cpu-save-"
                    + hashlib.sha256(request["dispatch_id"].encode()).hexdigest()[:32],
                    report_status="ready",
                    reported_at=logical_time,
                    artifact_uri=uri,
                    verification_status="verified",
                    verifier_id="cpu-reference-hmac-v1",
                    verified_at=logical_time,
                )
                self._event(
                    state,
                    record,
                    marker,
                    "checkpoint_reported",
                    logical_time,
                    checkpoint=checkpoint,
                )
        alive = self._alive(record)
        if alive and "stop_requested_ns" in record:
            elapsed = (time.monotonic_ns() - record["stop_requested_ns"]) / 1e9
            handle = self._handles.get(record["key"])
            # Signal only a process whose creation handle this adapter still owns.
            if handle is not None and elapsed >= self.config.stop_timeout_seconds:
                sig = (
                    signal.SIGKILL
                    if elapsed >= self.config.stop_timeout_seconds + 0.5
                    else signal.SIGTERM
                )
                try:
                    os.killpg(handle.pid, sig)
                except ProcessLookupError:
                    pass
            if elapsed >= self.config.stop_timeout_seconds + 1:
                self._event(state, record, "unconfirmed", "unknown", logical_time)
        if alive is False:
            record["stop_confirmed"] = True
            handle = self._handles.get(record["key"])
            record["exit_code"] = handle.poll() if handle is not None else None
            record["status"] = "stopped"
            if (directory / "result.json").exists():
                uri, _ = self._register_artifact(state, record, directory / "result.json", "result")
                record["status"] = "completed"
                self._event(state, record, "terminal", "completed", logical_time, result_uri=uri)
            elif "stop_requested_ns" in record:
                self._event(state, record, "terminal", "stopped", logical_time)
            else:
                self._event(
                    state,
                    record,
                    "terminal",
                    "failed",
                    logical_time,
                    error=m.ErrorDetail(
                        code="execution_unconfirmed",
                        detail="CPU process exited without a complete result",
                    ),
                )
        elif record["status"] == "launch_failed":
            self._event(
                state,
                record,
                "terminal",
                "failed",
                logical_time,
                error=m.ErrorDetail(
                    code="execution_unconfirmed", detail="CPU process launch failed"
                ),
            )
        elif alive is None and time.monotonic_ns() - record["created_monotonic_ns"] > 2_000_000_000:
            self._event(state, record, "unconfirmed", "unknown", logical_time)
        if record.pop("probe_requested", False):
            self._event(
                state,
                record,
                "probe:" + str(state["sequence"]),
                "alive" if alive else "unknown",
                logical_time,
            )

    def poll(self, *, logical_time: float, version: m.VersionStamp) -> tuple[m.ExecutorEvent, ...]:
        """Observe processes and return durable unacknowledged callbacks (at least once).

        The original dispatch epoch is retained even if ``version`` is newer;
        only the command kernel may accept or discard a stale callback.
        """
        if not math.isfinite(logical_time) or logical_time < 0:
            raise ExecutorRejected("Invalid poll logical time")
        with self._locked():
            state = self._read()
            for record in state["attempts"].values():
                try:
                    self._observe_record(state, record, logical_time)
                except (OSError, ValueError, KeyError, TypeError):
                    if self._alive(record) is False:
                        record["stop_confirmed"] = True
                        record["status"] = "failed"
                        self._event(
                            state,
                            record,
                            "terminal",
                            "failed",
                            logical_time,
                            error=m.ErrorDetail(
                                code="execution_unconfirmed",
                                detail="CPU evidence failed integrity or identity validation",
                            ),
                        )
                    else:
                        self._event(state, record, "invalid-evidence", "unknown", logical_time)
            self._save(state)
            return tuple(
                m.ExecutorEvent.model_validate_json(json.dumps(item["event"]))
                for item in state["events"]
                if not item["acked"]
            )

    def acknowledge(self, callback_id: str) -> None:
        with self._locked():
            state = self._read()
            for item in state["events"]:
                if item["event"]["callback_id"] == callback_id:
                    item["acked"] = True
                    self._save(state)
                    return
            raise ExecutorRejected("Unknown executor callback")

    def inspect(self) -> dict:
        """Local audit only; omits secrets. Do not expose without authorization."""
        with self._locked():
            state = self._read()
            return {
                "instance": state["instance"],
                "attempts": [
                    {
                        key: value
                        for key, value in record.items()
                        if key not in ("token", "controls")
                    }
                    for record in state["attempts"].values()
                ],
                "pending_callbacks": sum(not item["acked"] for item in state["events"]),
            }

    def reference_result(self) -> dict:
        return {
            "step": self.config.total_steps,
            "value": advance(self.config.seed, 0, self.config.total_steps),
        }
