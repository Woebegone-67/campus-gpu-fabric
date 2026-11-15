"""Opt-in, read-only input adapter for a reviewed external observation export.

This module does NOT collect contributor history or assert that a file is true.
An operator reviews the export, pins its bytes, and binds it to one instance
namespace, epoch, execution mode and model hash. Missing evidence leaves the
original snapshot intact. The caller must obtain an administrator/global read
at the decision's exact version and time; no retry or mutation occurs here.

Registry schema (unknown fields are rejected):
  schema_version=1, namespace, epoch, execution_mode,
  calendar_origin="logical_day0_monday", utc_offset_hours, model_sha256;
  devices[physical_gpu_id] = {contributor_owner_id, completed_windows,
    current_window_started_at, observed_at, available_at, notice_seconds,
    owner_use_seconds};
  completed_windows = [{started_at, ended_at, available_at, duration_seconds}];
  tasks[intent_id] = {owner_id, specification_sha256, observed_at, available_at,
    estimated_compute_seconds, checkpoint_seconds, restart_seconds?, stop_seconds?}.

Only first allocations in genuinely newly opened windows are enabled. Task
profiles estimate compute; TTL/timeout only bound the execution horizon. A
resume requires a separately reviewed remaining-work contract, not reuse of
an initial total-duration profile. The frozen policy remains responsible for
model cutoff, candidate coverage and legal placement. Model training and this
export must explicitly share the declared logical day-zero calendar.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from campus_fabric.contracts import models as m
from campus_fabric.policies.availability_model import historical_features
from campus_fabric.policies.reliability import MAX_AGE

Number = Annotated[float, Field(ge=0, le=m.MAX_SAFE_INTEGER, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, le=m.MAX_SAFE_INTEGER, allow_inf_nan=False)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MAX_REGISTRY_BYTES = 1_048_576
_LIVE_JOBS = {"Pending", "Running", "Checkpointing", "Preempted"}


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class _Window(_Record):
    started_at: Number
    ended_at: Number
    available_at: Number
    duration_seconds: Positive


class _Device(_Record):
    contributor_owner_id: m.Identifier
    completed_windows: Annotated[list[_Window], Field(min_length=4, max_length=32)]
    current_window_started_at: Number
    observed_at: Number
    available_at: Number
    notice_seconds: Number
    owner_use_seconds: Number


class _Task(_Record):
    owner_id: m.Identifier
    specification_sha256: Digest
    observed_at: Number
    available_at: Number
    estimated_compute_seconds: Positive
    checkpoint_seconds: Number
    restart_seconds: Number = .5
    stop_seconds: Number = .1


class _Registry(_Record):
    schema_version: Literal[1]
    namespace: m.Identifier
    epoch: m.NonnegativeInt
    execution_mode: m.ExecutionMode
    calendar_origin: Literal["logical_day0_monday"]
    utc_offset_hours: Annotated[float, Field(ge=-14, le=14, allow_inf_nan=False)]
    model_sha256: Digest
    devices: Annotated[dict[m.Identifier, _Device], Field(min_length=1, max_length=64)]
    tasks: Annotated[dict[m.Identifier, _Task], Field(min_length=1, max_length=1024)]


def specification_sha256(specification: m.WorkSpecification) -> str:
    """Hash the complete persisted specification, including explicit defaults."""
    raw = json.dumps(specification.model_dump(mode="json"), sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def execution_limits(intent: m.WorkIntent, allocation: m.AllocationView,
                     jobs: tuple[m.JobView, ...], now: float) -> tuple[int, float]:
    """Authoritative future recoveries and remaining lease/timeout, never work.

    A pending resume with a live job consumes one recovery upon its next grant.
    Its existing lease and job timeout are not restarted. This calculation is
    exposed for validation; enrich still rejects resumes without a separate
    remaining-compute profile contract.
    """
    if allocation.state != "Pending" or allocation.owner_id != intent.owner_id:
        raise ValueError("allocation is not an owned pending allocation")
    live = [job for job in jobs if job.allocation_id == allocation.allocation_id
            and job.state in _LIVE_JOBS]
    if len(live) > 1 or any(job.owner_id != intent.owner_id
                            or job.job_id not in intent.job_ids for job in live):
        raise ValueError("live job ownership or linkage is ambiguous")
    resuming = allocation.expires_at is not None
    if live and not resuming:
        raise ValueError("ungranted allocation unexpectedly has a live job")
    budget = (intent.specification.allocation.max_restarts - allocation.restart_count
              - int(resuming and bool(live)))
    lease = (allocation.expires_at - now if resuming
             else float(intent.specification.allocation.ttl_seconds))
    timeout = float(intent.specification.job.timeout_seconds or 86400)
    if live:
        timeout = live[0].created_at + timeout - now
    if budget < 0 or lease <= 0 or timeout <= 0:
        raise ValueError("recovery budget or execution deadline has expired")
    return budget, min(lease, timeout)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _times(record, now, label):
    if not 0 <= record.observed_at <= record.available_at <= now:
        raise ValueError(label + ": observation is not available by the snapshot cutoff")
    if now - record.observed_at > MAX_AGE:
        raise ValueError(label + ": observation is stale; its timestamp was not refreshed")


def _merge(existing, additions, label):
    """Never overwrite or silently supersede a pre-existing managed field."""
    result = list(existing)
    managed = {(item.name, item.source_id) for item in additions}
    for item in existing:
        if (item.name, item.source_id) in managed and item not in additions:
            raise ValueError(label + ": conflicting existing observation")
    for item in additions:
        if item not in result:
            result.append(item)
    return tuple(result)


def _datum(name, value, source, source_id, observed_at, available_at):
    return m.ObservedDatum(name=name, value=value, source=source, source_id=source_id,
                           observed_at=observed_at, available_at=available_at)


class AvailabilityInputProducer:
    """Load a pinned export once; invalid inputs never interrupt normal scheduling."""

    def __init__(self, path, *, expected_sha256: str, model_sha256: str, namespace: str):
        self.last_diagnostics: tuple[str, ...] = ()
        self._registry = None
        self._load_error = None
        try:
            if any(not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
                   for value in (expected_sha256, model_sha256)):
                raise ValueError("explicit registry and model SHA256 pins are required")
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_REGISTRY_BYTES + 1)
            if len(raw) > MAX_REGISTRY_BYTES:
                raise ValueError("registry exceeds the bounded byte limit")
            if hashlib.sha256(raw).hexdigest() != expected_sha256:
                raise ValueError("registry SHA256 does not match its reviewed pin")
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if type(data.get("schema_version")) is not int:
                raise ValueError("schema_version must be the integer 1")
            registry = _Registry.model_validate(data)
            if registry.namespace != namespace or registry.model_sha256 != model_sha256:
                raise ValueError("registry namespace or model SHA256 binding differs")
            self._registry = registry
        except ValidationError as error:
            detail = error.errors(include_input=False)[0]
            self._load_error = "registry schema invalid at " + ".".join(map(str, detail["loc"]))
        except (OSError, ValueError, TypeError, AttributeError) as error:
            self._load_error = str(error)
        if self._load_error:
            self.last_diagnostics = ("availability inputs inactive: " + self._load_error,)

    def enrich(self, decision: m.DecisionSnapshot, read: m.ReadSnapshot) -> m.DecisionSnapshot:
        """All-or-nothing enrichment; retain original objects on any evidence gap."""
        try:
            if self._registry is None:
                raise ValueError(self._load_error or "registry unavailable")
            result = self._enrich(decision, read)
            self.last_diagnostics = (
                "availability inputs enriched from a pinned, operator-reviewed external export; "
                "not automatic history collection or proof of model accuracy",
            )
            return result
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            self.last_diagnostics = ("availability inputs inactive: " + str(error),)
            return decision

    def _enrich(self, decision, read):
        registry = self._registry
        # Revalidate even if a caller constructed a DTO via model_copy/model_construct.
        decision = m.DecisionSnapshot.model_validate(decision.model_dump())
        read = m.ReadSnapshot.model_validate(read.model_dump())
        if not read.global_scope or read.viewer.role != "administrator":
            raise ValueError("an authoritative global administrator read is required")
        if read.version != decision.version or read.now != decision.now:
            raise ValueError("read and decision must have the exact same epoch, revision and time")
        if registry.epoch != decision.version.epoch or registry.execution_mode != decision.execution_mode:
            raise ValueError("registry epoch or execution mode differs from this instance")
        if not decision.demands:
            raise ValueError("no pending demands to enrich")
        intents = {item.intent_id: item for item in read.intents}
        allocations = {item.allocation_id: item for item in read.allocations}
        if len(intents) != len(read.intents) or len(allocations) != len(read.allocations):
            raise ValueError("authoritative read contains duplicate work or allocation IDs")
        demands = []
        for demand in decision.demands:
            intent, allocation = intents.get(demand.intent_id), allocations.get(demand.allocation_id)
            if intent is None or allocation is None:
                raise ValueError("pending demand has no matching authoritative work/allocation")
            if (demand.owner_id != intent.owner_id or allocation.owner_id != intent.owner_id
                    or allocation.allocation_id not in intent.allocation_ids
                    or demand.request != allocation.request
                    or demand.request != intent.specification.allocation.request
                    or demand.priority != intent.specification.allocation.priority
                    or demand.node_selector != intent.specification.allocation.node_selector
                    or demand.arrived_at != allocation.created_at
                    or intent.execution_mode != decision.execution_mode):
                raise ValueError("pending demand ownership, specification or linkage differs")
            task = registry.tasks.get(intent.intent_id)
            if task is None:
                raise ValueError("missing explicitly declared task profile")
            _times(task, decision.now, "task profile")
            if (task.owner_id != intent.owner_id
                    or task.specification_sha256 != specification_sha256(intent.specification)):
                raise ValueError("task owner or full specification SHA256 differs")
            budget, horizon = execution_limits(intent, allocation, read.jobs, decision.now)
            if (allocation.expires_at is not None or allocation.restart_count
                    or any(attempt.ref.allocation_id == allocation.allocation_id
                           for attempt in intent.attempts)):
                raise ValueError("resume requires a separately bound remaining-compute profile; "
                                 "initial total-compute profile is not reused")
            if budget > 2 or horizon < 900:
                raise ValueError("explicit recovery budget 0..2 and execution horizon >=900 are required")
            facts = tuple(_datum(name, getattr(task, name), "declared_profile", demand.demand_id,
                                 task.observed_at, task.available_at)
                          for name in ("estimated_compute_seconds", "checkpoint_seconds",
                                       "restart_seconds", "stop_seconds"))
            facts += tuple(_datum(name, value, "public_metadata", demand.demand_id,
                                   read.now, read.now) for name, value in (
                                       ("remaining_recoveries", budget),
                                       ("execution_window_seconds", horizon)))
            demands.append(m.DemandSnapshot.model_validate({**demand.model_dump(),
                "known_data": _merge(demand.known_data, facts, "task profile")}))

        history, metadata, seen = [], [], set()
        for node in decision.inventory:
            if len(node.devices) != 1 or node.capacity.gpu_count != 1 or node.devices[0].profile is not None:
                raise ValueError("only independent single whole-GPU nodes are supported")
            physical = node.devices[0].physical_gpu_id
            if physical in seen:
                raise ValueError("physical GPU appears in multiple nodes")
            seen.add(physical)
            device = registry.devices.get(physical)
            if device is None or device.contributor_owner_id != node.contributor_owner_id:
                raise ValueError("missing device observations or contributor ownership mismatch")
            _times(device, decision.now, "device observations")
            if not device.current_window_started_at <= device.observed_at:
                raise ValueError("current supply window starts after its observation")
            age = decision.now - device.current_window_started_at
            # Retain the real timestamp; inventory.observed_at is NOT a window opening.
            if age != 0 or device.observed_at != decision.now:
                raise ValueError("current supply window is not newly opened at this snapshot; "
                                 "age and observation timestamps were not refreshed")
            recalled = {ident for notice in decision.notices
                        if notice.unavailable_until > decision.now for ident in notice.device_ids}
            if physical in recalled or node.devices[0].device_id in recalled:
                raise ValueError("declared open window conflicts with an active authoritative recall")
            windows = sorted(device.completed_windows, key=lambda item: item.started_at)
            previous_end = None
            for window in windows:
                if (not window.started_at < window.ended_at <= window.available_at <= device.observed_at
                        or window.ended_at > device.current_window_started_at
                        or decision.now - window.ended_at > MAX_AGE
                        or (previous_end is not None and window.started_at < previous_end)):
                    raise ValueError("completed supply windows are overlapping, stale or unavailable")
                if not math.isclose(window.duration_seconds, window.ended_at - window.started_at,
                                    rel_tol=0., abs_tol=1e-9):
                    raise ValueError("completed window duration does not match end minus start")
                previous_end = window.ended_at
                history.append(_datum("availability_window_seconds", window.duration_seconds,
                                      "history", physical, window.ended_at, window.available_at))
            features = historical_features(device.observed_at,
                [window.model_dump() for window in windows], utc_offset_hours=registry.utc_offset_hours)
            metadata.extend(_datum(name, value, "history", physical,
                                   device.observed_at, device.available_at)
                            for name, value in features.items())
            metadata.extend(_datum(name, getattr(device, name), "declared_profile", physical,
                                   device.observed_at, device.available_at)
                            for name in ("notice_seconds", "owner_use_seconds"))
            metadata.append(_datum("availability_window_age_seconds", age, "observed_event", physical,
                                   device.observed_at, device.available_at))
        return m.DecisionSnapshot.model_validate({**decision.model_dump(), "demands": tuple(demands),
            "history": _merge(decision.history, tuple(history), "device history"),
            "metadata": _merge(decision.metadata, tuple(metadata), "device metadata")})
