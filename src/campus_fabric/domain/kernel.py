"""Transactional authority with fenced logical and physical execution.

Every externally visible effect, event, stage binding and response is saved in the
same SQLite transaction. Placement is derived from active claims, not from a
second mutable free-capacity counter. Policy snapshots omit executor due times.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from campus_fabric.contracts import models as m
from campus_fabric.domain.decision_views import owner_decision_view
from campus_fabric.domain.errors import Rejected
from campus_fabric.domain.lifecycle import LifecycleMixin
from campus_fabric.domain.placement import (
    choose_placement,
    validate_node,
    validate_placement,
)
from campus_fabric.storage.sqlite import SQLiteStore

ACTIVE = {"Allocated", "Running"}
JOB_TERMINAL = {"Succeeded", "Failed", "Cancelled"}
ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


def parse(cls, value):
    return cls.model_validate_json(json.dumps(value))


def plain(value):
    return value.model_dump(mode="json")


def timestamp(seconds):
    return (
        None
        if seconds is None
        else (ORIGIN + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    )


class Kernel(LifecycleMixin):
    def __init__(
        self,
        db_path: str | Path,
        *,
        inventory=(),
        simulation_duration_seconds=10.0,
        scheduling_mode="builtin",
        execution_mode="logical_simulation",
    ):
        if scheduling_mode not in {"builtin", "external"}:
            raise ValueError("Unknown scheduling mode")
        if not math.isfinite(simulation_duration_seconds) or simulation_duration_seconds <= 0:
            raise ValueError("Simulation duration must be a positive finite number")
        if execution_mode not in {"logical_simulation", "cpu_reference"}:
            raise ValueError("Unknown execution mode")
        self.store = SQLiteStore(db_path)
        nodes = {}
        for node in inventory:
            validate_node(node, tuple(parse(m.NodeInventory, n) for n in nodes.values()))
            nodes[node.node_id] = plain(node)
        initial = self._empty(0, float(simulation_duration_seconds))
        initial["scheduling_mode"] = scheduling_mode
        initial["nodes"] = nodes
        initial["model"]["execution_mode"] = execution_mode
        self.store.initialize(initial)
        if self.store.read()["model"]["execution_mode"] != execution_mode:
            raise ValueError("Persisted execution mode differs; use a separate instance")
        if self.store.read()["scheduling_mode"] != scheduling_mode:
            raise ValueError("Persisted scheduling mode differs; use its original mode or a new DB")
        if self.store.read()["model"]["duration_seconds"] != simulation_duration_seconds:
            raise ValueError(
                "Persisted execution model differs; use its original duration or a new DB"
            )

    @staticmethod
    def _empty(epoch, duration):
        return dict(
            epoch=epoch,
            revision=0,
            now=0.0,
            seq=0,
            next_id=0,
            nodes={},
            allocations={},
            jobs={},
            intents={},
            events=[],
            decisions={},
            dedup={},
            payloads={},
            outbox={},
            callbacks={},
            recalls={},
            faults={},
            node_generations={},
            retired={},
            scheduling_mode="builtin",
            model={
                "name": "constant-work-v1",
                "duration_seconds": duration,
                "execution_mode": "logical_simulation",
            },
        )

    @staticmethod
    def _version(s):
        return m.VersionStamp(epoch=s["epoch"], revision=s["revision"])

    @staticmethod
    def _id(s, kind):
        s["next_id"] += 1
        return f"e{s['epoch']}-{kind}{s['next_id']}"

    def _event(
        self,
        s,
        command,
        kind,
        owner=None,
        entities=(),
        before=None,
        after=None,
        deltas=(),
        attempt=None,
        decision=None,
    ):
        s["seq"] += 1
        event = m.CommittedEvent(
            event_id=f"e{s['epoch']}-ev{s['seq']}",
            sequence=s["seq"],
            version=self._version(s),
            logical_time=s["now"],
            event_type=kind,
            owner_id=owner,
            entities=tuple(entities),
            cause=m.RequestCause(kind="request", request_id=command.request_id),
            state_changes=tuple(
                m.StateChange(entity=e, before=before, after=after) for e in entities
            )
            if after is not None
            else (),
            resource_deltas=tuple(deltas),
            attempt=attempt,
            decision_id=None if decision is None else decision.decision_id,
            decision_snapshot_version=None if decision is None else decision.snapshot_version,
        )
        s["events"].append(plain(event))
        return event.event_id

    @staticmethod
    def _ref(kind, ident):
        return m.EntityRef(kind=kind, entity_id=ident)

    @staticmethod
    def _owned(s, collection, ident, actor):
        obj = s[collection].get(ident)
        if obj is None or (actor.role != "administrator" and obj["owner_id"] != actor.owner_id):
            raise Rejected("not_found", "Object not found in the authenticated owner's scope")
        return obj

    @staticmethod
    def _fingerprint(command):
        def normalize(value):
            if isinstance(value, dict):
                result = {k: normalize(v) for k, v in value.items()}
                for field in ("node_selector", "env"):
                    if field in result:
                        result[field] = sorted(result[field], key=lambda item: item["key"])
                if "image" in result and "command" in result:
                    if result.get("timeout_seconds") is None:
                        result["timeout_seconds"] = 86400
                    if result.get("max_retries") is None:
                        result["max_retries"] = 0
                return result
            if isinstance(value, list):
                return [normalize(v) for v in value]
            return value

        return json.dumps(
            normalize(plain(command.parameters)), sort_keys=True, separators=(",", ":")
        )

    def _dedup_key(self, c):
        scope = [c.actor.owner_id, c.operation]
        if c.operation == "workflow.register_intent":
            return json.dumps(scope + [c.parameters.registration.intent_id])
        if c.operation.startswith("workflow.create_"):
            # A stage ID cannot silently change from allocation to job creation.
            return json.dumps(
                [c.actor.owner_id, "workflow.stage", c.parameters.intent_id, c.parameters.stage_id]
            )
        if c.idempotency_key is not None:
            return json.dumps(scope + [c.idempotency_key])
        return None

    @staticmethod
    def _payload_key(receipt):
        return f"{receipt.version.epoch}:{receipt.version.revision}:{receipt.request_id}"

    def commit(self, command: m.CommandEnvelope) -> m.CommitReceipt:
        return self.commit_with_payload(command)[0]

    def sync_cpu_clock(self, boot_id: str, monotonic_ns: int):
        """Persist the physical clock anchor and its domain effects atomically.

        Only the single locked CPU driver calls this port. A service crash cannot
        separate a saved time advance from the sample that produced it. A host
        reboot requires explicit recovery instead of guessing a wall-clock delta.
        """
        with self.store.transaction() as conn:
            s = self.store.load(conn)
            if s["model"]["execution_mode"] != "cpu_reference":
                raise RuntimeError("Physical clock belongs to CPU reference mode")
            old = s.get("cpu_clock")
            if old and (old["boot_id"] != boot_id or monotonic_ns < old["monotonic_ns"]):
                raise RuntimeError("CPU clock host/boot changed; explicit recovery is required")
            remaining = 0. if old is None else (monotonic_ns - old["monotonic_ns"]) / 1e9
            while remaining > 0:
                delta = min(remaining, 86400.)
                tick_id = f"wall-{monotonic_ns}-{s['revision']}"
                command = m.CommandEnvelope(
                    request_id=tick_id,
                    actor=m.AuthenticatedActor(actor_id="cpu-driver", role="system"),
                    operation="clock.advance",
                    parameters=m.ClockAdvanceParameters(kind="clock.advance",
                        advance_seconds=delta, source="wall_clock_driver", driver_tick_id=tick_id))
                s["revision"] += 1
                self._apply(s, command)
                self._schedule(s, command)
                remaining -= delta
            s["cpu_clock"] = {"boot_id": boot_id, "monotonic_ns": monotonic_ns}
            for intent in s["intents"].values():
                parse(m.WorkIntent, intent)
            self._inventory(s)
            self.store.save(conn, s)

    def commit_with_payload(self, command: m.CommandEnvelope):
        """Return the HTTP response snapshot from the same transaction as its effect."""
        with self.store.transaction() as conn:
            base = self.store.load(conn)
            return self._commit_in_transaction(conn, base, command)

    def _commit_in_transaction(self, conn, base, command: m.CommandEnvelope):
        """Apply one validated command while the caller holds the writer transaction."""
        key = self._dedup_key(command)
        fingerprint = self._fingerprint(command)
        try:
            self._authorize(command)
            if key is not None and key in base["dedup"]:
                old = base["dedup"][key]
                if old["fingerprint"] != fingerprint:
                    raise Rejected(
                        "stage_conflict"
                        if command.operation.startswith("workflow.")
                        else "idempotency_conflict",
                        "This key already committed different parameters",
                    )
                receipt = parse(m.CommitReceipt, old["receipt"])
                return receipt, copy.deepcopy(base["payloads"][self._payload_key(receipt)])
            if (
                command.expected_version is not None
                and command.expected_version != self._version(base)
            ):
                raise Rejected(
                    "stale_version", "Refresh the snapshot and construct a new command"
                )
            s = copy.deepcopy(base)
            s["revision"] += 1
            first_event = len(s["events"])
            objects, status, body, stage = self._apply(s, command)
            if command.operation != "test.reset":
                self._schedule(s, command)
            event_ids = tuple(e["event_id"] for e in s["events"][first_event:])
            if command.operation == "test.reset":
                event_ids = tuple(e["event_id"] for e in s["events"])
            receipt = m.CommitReceipt(
                request_id=command.request_id,
                outcome="committed" if event_ids else "noop",
                version=self._version(s),
                logical_time=s["now"],
                objects=tuple(objects),
                event_ids=event_ids,
            )
            if stage is not None:
                intent_id, stage_id, operation = stage
                s["intents"][intent_id]["stages"].append(
                    plain(
                        m.StageReceipt(
                            stage_id=stage_id,
                            operation=operation,
                            state="committed",
                            receipt=receipt,
                        )
                    )
                )
            # Validate mutable aggregate against the immutable boundary before saving.
            for intent in s["intents"].values():
                parse(m.WorkIntent, intent)
            self._inventory(s)
            if key is not None:
                s["dedup"][key] = {"fingerprint": fingerprint, "receipt": plain(receipt)}
            if body is None:
                body = {"receipt": plain(receipt)}
            s["payloads"][self._payload_key(receipt)] = {"status_code": status, "body": body}
            self.store.save(conn, s)
            return receipt, copy.deepcopy(s["payloads"][self._payload_key(receipt)])
        except Rejected as exc:
            receipt = m.CommitReceipt(
                request_id=command.request_id,
                outcome="rejected",
                version=self._version(base),
                logical_time=base["now"],
                error=m.ErrorDetail(
                    code=exc.code, detail=exc.detail, current_version=self._version(base)
                ),
            )

            return receipt, self.receipt_payload(receipt)

    @staticmethod
    def _authorize(c):
        if (c.operation.startswith(("allocation.", "job.", "workflow."))
                and not c.actor.can_use_compute):
            raise Rejected("forbidden", "Compute-use authority is required")
        if c.operation.startswith("contribution.") and not c.actor.can_contribute_resources:
            raise Rejected("forbidden", "Resource-contribution authority is required")

    def _apply(self, s, c):
        p = c.parameters
        refs, status, body, stage = [], 200, None, None
        if c.operation in {"allocation.checkpoint", "allocation.resume",
                           "test.contributor_demand", "contribution.recall",
                           "test.node_state", "test.fault", "executor.report"}:
            return self._apply_lifecycle(s, c)
        if c.operation == "allocation.create":
            a = self._allocate(s, c, p)
            refs = [self._ref("allocation", a["allocation_id"])]
            status, body = (201 if a["state"] == "Allocated" else 202), self.allocation_http(a)
        elif c.operation == "allocation.release":
            a = self._owned(s, "allocations", p.allocation_id, c.actor)
            self._release(s, c, a, "Released")
            refs, status = [self._ref("allocation", p.allocation_id)], 202
        elif c.operation == "allocation.renew":
            try:
                a = self._owned(s, "allocations", p.allocation_id, c.actor)
            except Rejected as exc:
                raise Rejected("lease_unavailable", "Lease is unavailable to this caller") from exc
            if a["state"] not in ACTIVE or a["expires_at"] <= s["now"]:
                raise Rejected("lease_unavailable", "Only a live granted lease can be renewed")
            a["expires_at"] = s["now"] + (p.ttl_seconds or a["parameters"]["ttl_seconds"])
            refs = [self._ref("allocation", p.allocation_id)]
            self._event(s, c, "allocation.renewed", a["owner_id"], refs, after=a["state"])
            body = self.allocation_http(a)
        elif c.operation == "job.create":
            a = self._owned(s, "allocations", p.allocation_id, c.actor)
            if a["intent_id"] is not None:
                raise Rejected(
                    "lease_unavailable", "Use the owning WorkIntent job stage for this lease"
                )
            job = self._job(s, c, a, p)
            refs, status, body = [self._ref("job", job["job_id"])], 201, self.job_http(job)
        elif c.operation == "job.cancel":
            job = self._owned(s, "jobs", p.job_id, c.actor)
            if job["state"] not in JOB_TERMINAL:
                self._finish_job(s, c, job, "Cancelled", "Cancelled by owner")
            refs, status = [self._ref("job", p.job_id)], 202
        elif c.operation == "clock.advance":
            self._advance(s, c, s["now"] + p.advance_seconds)
            self._event(s, c, "clock.advanced")
            body = {"now": s["now"], "observed_at": timestamp(s["now"])}
        elif c.operation == "test.reset":
            epoch, duration = s["epoch"] + 1, s["model"]["duration_seconds"]
            mode = s["scheduling_mode"]
            execution_mode = s["model"]["execution_mode"]
            cpu_clock = s.get("cpu_clock")
            retired = dict(s.get("retired", {}))
            if execution_mode == "cpu_reference":
                for job in s["jobs"].values():
                    if job.get("physical_live"):
                        self._dispatch(s, job, "stop")
                        a = s["allocations"][job["allocation_id"]]
                        retired[job["attempt"]["attempt_id"]] = {
                            "epoch": s["epoch"], "owner_id": job["owner_id"],
                            "placement": a["placement"], "attempt": job["attempt"],
                            "physical_gpu_ids": sorted({d["physical_gpu_id"]
                                for part in (a["placement"] or {}).get("slices", [])
                                for d in s["nodes"][part["node_id"]]["devices"]
                                if d["device_id"] in part["device_ids"]}),
                            "commands": [v["command"] for v in s["outbox"].values()
                                         if v["command"]["attempt"] == job["attempt"]],
                        }
            s.clear()
            s.update(self._empty(epoch, duration))
            s["scheduling_mode"] = mode
            s["model"]["execution_mode"] = execution_mode
            s["retired"] = retired
            if cpu_clock is not None:
                s["cpu_clock"] = cpu_clock
            self._event(s, c, "system.reset")
            status, body = 204, {}
        elif c.operation == "contribution.register":
            try:
                if p.node.observed_at != s["now"]:
                    raise ValueError("Inventory observation must match current logical time")
                validate_node(p.node, tuple(parse(m.NodeInventory, n) for n in s["nodes"].values()))
            except ValueError as exc:
                raise Rejected("invalid_command", str(exc)) from exc
            s["nodes"][p.node.node_id] = plain(p.node)
            self._refresh_blocks(s)
            refs, status = [self._ref("node", p.node.node_id)], 201
            self._event(s, c, "contribution.registered", c.actor.owner_id, refs, after=p.node.state)
        elif c.operation == "workflow.register_intent":
            r = p.registration
            if r.execution_mode != s["model"]["execution_mode"]:
                raise Rejected("invalid_command", "Work execution mode must match this instance")
            if r.intent_id in s["intents"]:
                raise Rejected("stage_conflict", "Intent identifier already registered")
            data = plain(r)
            intent = parse(m.WorkIntent, {**data, "first_arrival_at": s["now"], "state": "pending"})
            s["intents"][r.intent_id] = plain(intent)
            refs, status = [self._ref("intent", r.intent_id)], 202
            self._event(s, c, "intent.registered", r.owner_id, refs, after="pending")
        elif c.operation.startswith("workflow.create_"):
            intent = self._owned(s, "intents", p.intent_id, c.actor)
            if intent["state"] in {"completed", "failed", "cancelled"}:
                raise Rejected("stage_conflict", "Terminal intent cannot create new work")
            if c.operation == "workflow.create_allocation_and_bind":
                if intent["allocation_ids"]:
                    a = s["allocations"][intent["allocation_ids"][0]]
                else:
                    a = self._allocate(
                        s,
                        c,
                        parse(m.AllocationCreateParameters, intent["specification"]["allocation"]),
                        p.intent_id,
                    )
                    intent["allocation_ids"].append(a["allocation_id"])
                refs = [
                    self._ref("allocation", a["allocation_id"]),
                    self._ref("intent", p.intent_id),
                ]
                stage = (p.intent_id, p.stage_id, "allocate")
                if not intent["job_ids"] and a["state"] in ACTIVE | {"Pending"}:
                    intent["state"] = "waiting"
            else:
                if not intent["allocation_ids"]:
                    raise Rejected(
                        "lease_unavailable", "Allocate the intent before creating its job"
                    )
                a = s["allocations"][intent["allocation_ids"][0]]
                if intent["job_ids"]:
                    job = s["jobs"][intent["job_ids"][0]]
                else:
                    spec = parse(
                        m.JobCreateParameters,
                        {
                            **intent["specification"]["job"],
                            "kind": "job.create",
                            "allocation_id": a["allocation_id"],
                        },
                    )
                    job = self._job(s, c, a, spec, p.intent_id)
                    intent["job_ids"].append(job["job_id"])
                    intent["attempts"].append(
                        {
                            "ref": job["attempt"],
                            "state": "starting" if job["physical_live"] else "running",
                            "created_at": s["now"],
                            "started_at": None if job["physical_live"] else s["now"],
                            "finished_at": None,
                            "checkpoint": None,
                        }
                    )
                refs = [self._ref("job", job["job_id"]), self._ref("intent", p.intent_id)]
                stage = (p.intent_id, p.stage_id, "create_job_and_bind")
                intent["state"] = "running"
            self._event(s, c, "intent.stage_bound", intent["owner_id"], refs, after=intent["state"])
            status = 202
        elif c.operation == "decision.commit":
            plan = p.plan
            if plan.decision_at != s["now"] or plan.decision_id in s["decisions"]:
                raise Rejected("stale_version", "Decision time or identity is stale")
            selected = next(
                (x for x in plan.candidates if x.candidate_id == plan.selected_candidate_id), None
            )
            if selected is not None:
                self._check_preconditions(s, selected)
            if selected is not None and selected.action == "allocate":
                a = s["allocations"].get(selected.allocation_id)
                if (
                    a is None
                    or a["state"] != "Pending"
                    or a["owner_id"] != selected.owner_id
                    or selected.demand_id != a["allocation_id"]
                ):
                    raise Rejected(
                        "invalid_command", "Selected action does not match a pending demand"
                    )
                try:
                    params = parse(m.AllocationCreateParameters, a["parameters"])
                    validate_placement(
                        params.request,
                        params.node_selector,
                        selected.placement,
                        self._inventory(s),
                    )
                except (ValueError, TypeError, AttributeError) as exc:
                    raise Rejected(
                        "insufficient_resources", "Selected placement is not currently legal"
                    ) from exc
                self._grant(s, c, a, selected.placement, plan)
            elif selected is not None and selected.action != "defer":
                raise Rejected("invalid_command", "This decision action is not supported at CP1")
            s["decisions"][plan.decision_id] = plain(plan)
            refs = [self._ref("decision", plan.decision_id)]
            self._event(
                s,
                c,
                "decision.committed",
                selected.owner_id if selected else None,
                refs,
                decision=plan,
            )
        else:
            raise Rejected(
                "invalid_command", "Unsupported operation"
            )
        return refs, status, body, stage

    def _check_preconditions(self, s, candidate):
        if candidate.action not in {"allocate", "defer"}:
            raise Rejected(
                "invalid_command", "Only allocate/defer decisions are implemented at CP1"
            )
        target = s["allocations"].get(candidate.allocation_id)
        if (
            target is None
            or target["state"] != "Pending"
            or target["owner_id"] != candidate.owner_id
            or candidate.demand_id != target["allocation_id"]
            or (candidate.intent_id is not None and candidate.intent_id != target["intent_id"])
        ):
            raise Rejected("invalid_command", "Candidate must identify its current pending demand")
        for condition in candidate.preconditions:
            if (
                condition.entity.kind != "allocation"
                or condition.entity.entity_id != target["allocation_id"]
            ):
                raise Rejected(
                    "invalid_command", "CP1 preconditions must refer to the selected allocation"
                )
            values = {
                "owner": target["owner_id"],
                "allocation_state": target["state"],
                "lease_valid": target["expires_at"] is not None and target["expires_at"] > s["now"],
            }
            if condition.kind not in values:
                raise Rejected("invalid_command", "This precondition is not implemented at CP1")
            actual = values[condition.kind]
            if (
                type(actual) is not type(condition.expected_value)
                or actual != condition.expected_value
            ):
                raise Rejected("stale_version", "Selected action precondition is false")

    def _allocate(self, s, c, p, intent_id=None):
        ident = self._id(s, "a")
        a = dict(
            allocation_id=ident,
            owner_id=p.owner_id,
            state="Pending",
            parameters=plain(p),
            request=plain(p.request),
            created_at=s["now"],
            placement=None,
            expires_at=None,
            granted_at=None,
            intent_id=intent_id,
            restart_count=0,
            preemption=None,
            checkpoint=None,
            resume_requested=False,
        )
        s["allocations"][ident] = a
        self._event(
            s,
            c,
            "allocation.requested",
            p.owner_id,
            [self._ref("allocation", ident)],
            after="Pending",
        )
        # The new request participates in the same queue as previous pending work.
        self._schedule(s, c)
        return a

    @staticmethod
    def _delta(a, sign):
        result = []
        for piece in a["placement"]["slices"]:
            res = piece["resources"]
            result.append(
                m.ResourceDelta(
                    node_id=piece["node_id"],
                    allocation_id=a["allocation_id"],
                    cpu_millicores=sign * res["cpu_millicores"],
                    memory_mib=sign * res["memory_mib"],
                    ephemeral_storage_mib=sign * res["ephemeral_storage_mib"],
                    device_ids_added=tuple(piece["device_ids"]) if sign == 1 else (),
                    device_ids_removed=tuple(piece["device_ids"]) if sign == -1 else (),
                )
            )
        return result

    def _grant(self, s, c, a, placement, decision=None):
        resuming = a.get("resume_requested", False)
        old_expiry, old_grant = a["expires_at"], a["granted_at"]
        a.update(
            state="Allocated",
            placement=plain(placement),
            granted_at=s["now"],
            expires_at=s["now"] + a["parameters"]["ttl_seconds"],
        )
        self._event(
            s,
            c,
            "allocation.granted",
            a["owner_id"],
            [self._ref("allocation", a["allocation_id"])],
            before="Pending",
            after="Allocated",
            deltas=self._delta(a, 1),
            decision=decision,
        )
        if resuming:
            a.update(expires_at=old_expiry, granted_at=old_grant, resume_requested=False)
            job = self._live_job(s, a)
            if job is not None:
                self._next_attempt(s, c, job, reason="restart")
            a["preemption"] = None

    def _schedule(self, s, c):
        if s["scheduling_mode"] == "external":
            return
        pending = sorted(
            (a for a in s["allocations"].values() if a["state"] == "Pending"),
            key=lambda a: (
                -a["parameters"]["priority"],
                a["created_at"],
                int(a["allocation_id"].split("-a")[-1]),
            ),
        )
        for a in pending:
            p = parse(m.AllocationCreateParameters, a["parameters"])
            placement = choose_placement(
                p.request, p.node_selector, self._inventory(s)
            )
            if placement is not None:
                self._grant(s, c, a, placement)

    def _job(self, s, c, a, p, intent_id=None):
        if a["state"] != "Allocated" or a["expires_at"] <= s["now"]:
            raise Rejected(
                "lease_unavailable", "Job requires a live allocated lease with no active job"
            )
        jid = self._id(s, "j")
        attempt = m.AttemptRef(
            intent_id=intent_id or f"direct-{jid}",
            attempt_id=self._id(s, "attempt"),
            generation=1,
            allocation_id=a["allocation_id"],
            job_id=jid,
        )
        job = dict(
            job_id=jid,
            allocation_id=a["allocation_id"],
            owner_id=a["owner_id"],
            state="Running",
            created_at=s["now"],
            started_at=s["now"],
            finished_at=None,
            attempt=plain(attempt),
            timeout_at=s["now"] + (p.timeout_seconds or 86400),
            completion_at=s["now"] + s["model"]["duration_seconds"],
            specification=plain(p),
            execution_mode=s["model"]["execution_mode"],
            retry_count=0,
            physical_live=s["model"]["execution_mode"] == "cpu_reference",
            checkpoint=None,
            retry_at=None,
            intent_id=intent_id,
            failure=None,
        )
        s["jobs"][jid] = job
        a["state"] = "Running"
        dispatch = m.ExecutorCommand(
            dispatch_id=self._id(s, "dispatch"),
            version=self._version(s),
            attempt=attempt,
            action="start",
            mode=s["model"]["execution_mode"],
            job=p,
        )
        # No external side effect: persisted model execution acknowledges within the transaction.
        s["outbox"][dispatch.dispatch_id] = {
            "command": plain(dispatch),
            "status": "model_acknowledged" if job["execution_mode"] == "logical_simulation" else "pending",
            "acknowledged_at": s["now"] if job["execution_mode"] == "logical_simulation" else None,
        }
        self._event(
            s,
            c,
            "job.dispatched" if job["physical_live"] else "job.started",
            a["owner_id"],
            [self._ref("job", jid)],
            after="Running",
            attempt=attempt,
        )
        return job

    def _finish_job_final(self, s, c, job, state, reason=None, *, settle=True):
        if job["state"] in JOB_TERMINAL:
            return
        old = job["state"]
        job.update(state=state, finished_at=s["now"], failure=reason)
        a = s["allocations"][job["allocation_id"]]
        if a["state"] == "Running":
            a["state"] = "Allocated"
        self._event(
            s,
            c,
            "job." + state.lower(),
            job["owner_id"],
            [self._ref("job", job["job_id"])],
            before=old,
            after=state,
            attempt=parse(m.AttemptRef, job["attempt"]),
        )
        if job["intent_id"]:
            intent = s["intents"][job["intent_id"]]
            intent["state"] = {
                "Succeeded": "completed",
                "Failed": "failed",
                "Cancelled": "cancelled",
            }[state]
            intent["result"] = plain(
                m.WorkResult(
                    kind={"Succeeded": "success", "Failed": "failure", "Cancelled": "cancelled"}[
                        state
                    ],
                    recorded_at=s["now"],
                    artifact_uris=(job["result_uri"],) if job.get("result_uri") else (),
                    failure=m.ErrorDetail(
                        code="execution_unconfirmed", detail=reason or "Modeled execution failed"
                    )
                    if state == "Failed"
                    else None,
                )
            )
            for attempt in intent["attempts"]:
                if attempt["ref"] == job["attempt"]:
                    attempt.update(state=intent["state"], finished_at=s["now"])
            self._event(
                s,
                c,
                "intent." + intent["state"],
                job["owner_id"],
                [self._ref("intent", intent["intent_id"])],
                after=intent["state"],
            )
            if settle and (
                intent["settlement_action"] == "release_on_terminal"
                or (state == "Succeeded" and intent["settlement_action"] == "release_on_success")
            ):
                self._release(s, c, a, "Released")
                self._event(
                    s,
                    c,
                    "intent.lease_settled",
                    job["owner_id"],
                    [self._ref("intent", intent["intent_id"])],
                    after="released",
                )

    def _release_final(self, s, c, a, state):
        if a["state"] not in ACTIVE | {"Pending", "PreemptRequested", "Preempted"}:
            return
        old = a["state"]
        deltas = self._delta(a, -1) if a["placement"] is not None else []
        for job in s["jobs"].values():
            if job["allocation_id"] == a["allocation_id"] and job["state"] not in JOB_TERMINAL:
                self._finish_job(
                    s,
                    c,
                    job,
                    "Failed" if state in {"Expired", "Evicted"} else "Cancelled",
                    "Lease " + state.lower(),
                    settle=False,
                )
        a.update(state=state, placement=None)
        self._event(
            s,
            c,
            "allocation." + state.lower(),
            a["owner_id"],
            [self._ref("allocation", a["allocation_id"])],
            before=old,
            after=state,
            deltas=deltas,
        )
        if a["intent_id"]:
            intent = s["intents"][a["intent_id"]]
            if not intent["job_ids"] and intent["state"] not in {
                "completed",
                "failed",
                "cancelled",
            }:
                # The only lease ended before job creation. No operation may
                # revive its TTL, and there is no job to cancel or retry. Record
                # a complete outcome instead of an unresolvable manual-action
                # state; a new attempt requires an explicit new-work identity.
                previous = intent["state"]
                cancelled = state == "Released"
                intent["state"] = "cancelled" if cancelled else "failed"
                intent["result"] = plain(m.WorkResult(
                    kind="cancelled" if cancelled else "failure",
                    recorded_at=s["now"],
                    failure=None if cancelled else m.ErrorDetail(
                        code="lease_unavailable",
                        detail=f"Lease {state.lower()} before job creation; submit a new work identity to retry.",
                    ),
                ))
                self._event(s, c, "intent." + intent["state"], intent["owner_id"],
                            [self._ref("intent", intent["intent_id"])],
                            before=previous, after=intent["state"])

    @staticmethod
    def allocation_http(a):
        return dict(
            id=a["allocation_id"],
            state=a["state"],
            owner=a["owner_id"],
            request=a["request"],
            placement=a["placement"],
            created_at=timestamp(a["created_at"]),
            expires_at=timestamp(a["expires_at"]),
            preemption=a.get("preemption"),
            resume_token=None,
            restart_count=a.get("restart_count", 0),
            checkpoint=a.get("checkpoint"),
        )

    @staticmethod
    def job_http(job):
        return dict(
            id=job["job_id"],
            allocation_id=job["allocation_id"],
            owner=job["owner_id"],
            state=job["state"],
            created_at=timestamp(job["created_at"]),
            finished_at=timestamp(job["finished_at"]),
            execution_mode=job["execution_mode"],
            failure=job["failure"],
            retry_count=job.get("retry_count", 0),
            attempt=job["attempt"],
            checkpoint=job.get("checkpoint"),
            result_uri=job.get("result_uri"),
        )

    def receipt_payload(self, receipt):
        if receipt.outcome == "rejected":
            status = {
                "not_found": 404,
                "forbidden": 403,
                "unauthorized": 401,
                "invalid_command": 400,
            }.get(receipt.error.code, 409)
            return {
                "status_code": status,
                "body": {
                    "error": receipt.error.code,
                    "detail": receipt.error.detail,
                    "receipt": plain(receipt),
                },
            }
        return self.store.read()["payloads"][self._payload_key(receipt)]

    def export_state(self, actor):
        state = self.store.read()
        allowed = {k: state[k] for k in ("epoch", "revision", "now")}
        allowed["nodes"] = {
            n.node_id: plain(n)
            for n in self._inventory(state)
        }
        for collection in ("allocations", "jobs", "intents"):
            allowed[collection] = {
                k: v
                for k, v in state[collection].items()
                if actor.role == "administrator" or v["owner_id"] == actor.owner_id
            }
        allowed["events"] = [
            e
            for e in state["events"]
            if actor.role == "administrator" or e["owner_id"] == actor.owner_id
        ]
        allowed["decisions"] = {}
        for key, value in state["decisions"].items():
            plan = parse(m.DecisionPlan, value)
            view = (plan if actor.role == "administrator" else
                    owner_decision_view(plan, actor.owner_id, state["allocations"]))
            if view is not None:
                allowed["decisions"][key] = plain(view)
        # Full internal job timing/specifications belong to authority, not read projections.
        for job in allowed["jobs"].values():
            for field in ("completion_at", "specification", "timeout_at", "remaining_seconds", "retry_at"):
                job.pop(field, None)
        for allocation in allowed["allocations"].values():
            allocation.pop("parameters", None)
        return allowed

    def decision_snapshot(self):
        s = self.store.read()
        active = [a for a in s["allocations"].values() if a["placement"] is not None]
        return m.DecisionSnapshot(
            version=self._version(s),
            now=s["now"],
            execution_mode=s["model"]["execution_mode"],
            notices=tuple(parse(m.ContributorNotice, n) for n in s.get("recalls", {}).values()
                          if n["unavailable_until"] > s["now"]),
            inventory=self._inventory(s),
            demands=tuple(
                m.DemandSnapshot(
                    demand_id=a["allocation_id"],
                    owner_id=a["owner_id"],
                    arrived_at=a["created_at"],
                    request=parse(m.ResourceRequest, a["request"]),
                    priority=a["parameters"]["priority"],
                    allocation_id=a["allocation_id"],
                    intent_id=a["intent_id"],
                    node_selector=tuple(
                        parse(m.KeyValue, x) for x in a["parameters"]["node_selector"]
                    ),
                )
                for a in s["allocations"].values()
                if a["state"] == "Pending"
            ),
            occupancy=tuple(
                m.Occupancy(
                    allocation_id=a["allocation_id"],
                    owner_id=a["owner_id"],
                    placement=parse(m.Placement, a["placement"]),
                    since=a["granted_at"],
                    status=("quarantined" if a.get("pending_terminal") else
                            "stopping" if a["state"] == "PreemptRequested" else
                            "executing" if a["state"] == "Running" else "reserved"),
                )
                for a in active
            ),
            leases=tuple(
                m.LeaseSnapshot(
                    allocation_id=a["allocation_id"],
                    owner_id=a["owner_id"],
                    starts_at=a["granted_at"],
                    expires_at=a["expires_at"],
                    placement=parse(m.Placement, a["placement"]),
                )
                for a in active
            ),
        )

    def read_snapshot(self, query):
        s = self.store.read()
        if query.after is not None and query.after.epoch != s["epoch"]:
            raise ValueError("Event cursor belongs to an old epoch; refresh without the cursor")

        def visible(v):
            return query.global_scope or v["owner_id"] == query.actor.owner_id

        selection = {(x.kind, x.entity_id) for x in query.entities}

        def selected(kind, ident):
            return not selection or (kind, ident) in selection

        allocations = tuple(
            parse(m.AllocationView, {k: a[k] for k in m.AllocationView.model_fields if k in a})
            for a in s["allocations"].values()
            if visible(a) and selected("allocation", a["allocation_id"])
        )
        jobs = tuple(
            parse(m.JobView, {k: j[k] for k in m.JobView.model_fields if k in j})
            for j in s["jobs"].values()
            if visible(j) and selected("job", j["job_id"])
        )
        intents = tuple(
            parse(m.WorkIntent, v)
            for v in s["intents"].values()
            if visible(v) and selected("intent", v["intent_id"])
        )
        events = tuple(
            parse(m.CommittedEvent, e)
            for e in s["events"]
            if visible(e)
            and (query.after is None or e["sequence"] > query.after.after_sequence)
            and (
                not selection
                or any((x["kind"], x["entity_id"]) in selection for x in e["entities"])
            )
        )
        decisions = []
        for value in s["decisions"].values():
            plan = parse(m.DecisionPlan, value)
            view = (plan if query.global_scope else
                    owner_decision_view(plan, query.actor.owner_id, s["allocations"]))
            if view is not None:
                decisions.append(view)
        actions = tuple(
            m.LegalAction(
                operation="allocation.release",
                entity=self._ref("allocation", a.allocation_id),
                explanation="Return this lease explicitly",
            )
            for a in allocations
            if a.state in ACTIVE | {"Pending"} and query.actor.can_use_compute
        )
        return m.ReadSnapshot(
            version=self._version(s),
            now=s["now"],
            viewer=query.actor,
            global_scope=query.global_scope,
            allocations=allocations,
            jobs=jobs,
            intents=intents,
            events=events,
            decisions=tuple(decisions),
            legal_actions=actions,
            next_cursor=m.EventCursor(epoch=s["epoch"], after_sequence=s["seq"]),
        )

    def register_current_intent(self, registration, *, actor, request_id):
        """Register immutable inputs without inventing a client snapshot precondition.

        HTTP callers that omit If-Match have not made a versioned scheduling
        decision. Stamp the command from the transaction's state so ordinary CPU
        clock ticks cannot invalidate registration between a read and this write.
        Explicitly versioned workflow ports retain their existing CAS semantics.
        """
        with self.store.transaction() as conn:
            base = self.store.load(conn)
            command = m.CommandEnvelope(
                request_id=request_id,
                actor=actor,
                operation="workflow.register_intent",
                parameters=m.WorkflowIntentParameters(
                    kind="workflow.register_intent", registration=registration
                ),
                expected_version=self._version(base),
            )
            return self._commit_in_transaction(conn, base, command)[0]

    def register_intent(self, registration, expected_version, *, actor, request_id):
        return self.commit(
            m.CommandEnvelope(
                request_id=request_id,
                actor=actor,
                operation="workflow.register_intent",
                parameters=m.WorkflowIntentParameters(
                    kind="workflow.register_intent", registration=registration
                ),
                expected_version=expected_version,
            )
        )

    def create_allocation_and_bind(
        self, intent_id, stage_id, expected_version, *, actor, request_id
    ):
        return self._stage(
            "workflow.create_allocation_and_bind",
            intent_id,
            stage_id,
            expected_version,
            actor,
            request_id,
        )

    def create_job_and_bind(self, intent_id, stage_id, expected_version, *, actor, request_id):
        return self._stage(
            "workflow.create_job_and_bind", intent_id, stage_id, expected_version, actor, request_id
        )

    def _stage(self, operation, intent_id, stage_id, expected_version, actor, request_id):
        return self.commit(
            m.CommandEnvelope(
                request_id=request_id,
                actor=actor,
                operation=operation,
                parameters=m.WorkflowStageParameters(
                    kind=operation, intent_id=intent_id, stage_id=stage_id
                ),
                expected_version=expected_version,
            )
        )
