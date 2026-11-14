"""Transactional CP1 authority. Logical simulation never launches a workload process.

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
from campus_fabric.domain.placement import (
    choose_placement,
    inventory,
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


class Rejected(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(detail)


class Kernel:
    def __init__(
        self,
        db_path: str | Path,
        *,
        inventory=(),
        simulation_duration_seconds=10.0,
        scheduling_mode="builtin",
    ):
        if scheduling_mode not in {"builtin", "external"}:
            raise ValueError("Unknown scheduling mode")
        if not math.isfinite(simulation_duration_seconds) or simulation_duration_seconds <= 0:
            raise ValueError("Simulation duration must be a positive finite number")
        self.store = SQLiteStore(db_path)
        nodes = {}
        for node in inventory:
            validate_node(node, tuple(parse(m.NodeInventory, n) for n in nodes.values()))
            nodes[node.node_id] = plain(node)
        initial = self._empty(0, float(simulation_duration_seconds))
        initial["scheduling_mode"] = scheduling_mode
        initial["nodes"] = nodes
        self.store.initialize(initial)
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

    def commit_with_payload(self, command: m.CommandEnvelope):
        """Return the HTTP response snapshot from the same transaction as its effect."""
        with self.store.transaction() as conn:
            base = self.store.load(conn)
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
                inventory(s["nodes"], s["allocations"], s["now"])
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
        if c.operation.startswith(("allocation.", "job.", "workflow.")) and c.actor.role not in (
            "researcher",
            "administrator",
        ):
            raise Rejected("forbidden", "This role cannot submit tenant work")

    def _apply(self, s, c):
        p = c.parameters
        refs, status, body, stage = [], 200, None, None
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
            s.clear()
            s.update(self._empty(epoch, duration))
            s["scheduling_mode"] = mode
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
            refs, status = [self._ref("node", p.node.node_id)], 201
            self._event(s, c, "contribution.registered", c.actor.owner_id, refs, after=p.node.state)
        elif c.operation == "workflow.register_intent":
            r = p.registration
            if r.execution_mode != "logical_simulation":
                raise Rejected("invalid_command", "CP1 supports logical_simulation only")
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
                            "state": "running",
                            "created_at": s["now"],
                            "started_at": s["now"],
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
                        inventory(s["nodes"], s["allocations"], s["now"]),
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
        elif c.operation == "executor.report":
            event = p.event
            job = s["jobs"].get(event.attempt.job_id)
            if (
                event.version.epoch != s["epoch"]
                or event.version.revision > s["revision"] - 1
                or job is None
                or job["state"] != "Running"
                or job["attempt"] != plain(event.attempt)
                or event.observed_at != s["now"]
                or event.mode != "logical_simulation"
            ):
                raise Rejected(
                    "attempt_stale", "Callback does not identify the current live attempt"
                )
            # Only the internal simulation driver can settle its modeled completion.
            if event.kind == "completed" and job["completion_at"] <= s["now"]:
                self._finish_job(s, c, job, "Succeeded")
            else:
                raise Rejected("invalid_command", "Unsupported or premature CP1 executor callback")
        else:
            raise Rejected(
                "invalid_command", "This operation belongs to CP2 and is not implemented"
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
                p.request, p.node_selector, inventory(s["nodes"], s["allocations"], s["now"])
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
            execution_mode="logical_simulation",
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
            mode="logical_simulation",
            job=p,
        )
        # No external side effect: persisted model execution acknowledges within the transaction.
        s["outbox"][dispatch.dispatch_id] = {
            "command": plain(dispatch),
            "status": "model_acknowledged",
            "acknowledged_at": s["now"],
        }
        self._event(
            s,
            c,
            "job.started",
            a["owner_id"],
            [self._ref("job", jid)],
            after="Running",
            attempt=attempt,
        )
        return job

    def _finish_job(self, s, c, job, state, reason=None, *, settle=True):
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

    def _release(self, s, c, a, state):
        if a["state"] not in ACTIVE | {"Pending"}:
            return
        old = a["state"]
        deltas = self._delta(a, -1) if a["placement"] is not None else []
        for job in s["jobs"].values():
            if job["allocation_id"] == a["allocation_id"] and job["state"] not in JOB_TERMINAL:
                self._finish_job(
                    s,
                    c,
                    job,
                    "Failed" if state == "Expired" else "Cancelled",
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
                intent["state"] = "manual_action"

    def _advance(self, s, c, target):
        # Rebuild the small CP1 timer frontier after each boundary. No skipped expiries.
        while True:
            due = [
                (a["expires_at"], 0, a["allocation_id"])
                for a in s["allocations"].values()
                if a["state"] in ACTIVE and a["expires_at"] <= target
            ]
            for job in s["jobs"].values():
                if job["state"] == "Running":
                    if job["timeout_at"] <= target:
                        due.append((job["timeout_at"], 1, job["job_id"]))
                    if job["completion_at"] <= target:
                        due.append((job["completion_at"], 2, job["job_id"]))
            if not due:
                break
            at = min(x[0] for x in due)
            s["now"] = at
            for _, priority, ident in sorted(x for x in due if x[0] == at):
                if priority == 0:
                    self._release(s, c, s["allocations"][ident], "Expired")
                else:
                    job = s["jobs"][ident]
                    if job["state"] == "Running":
                        self._finish_job(
                            s,
                            c,
                            job,
                            "Failed" if priority == 1 else "Succeeded",
                            "Job timeout" if priority == 1 else None,
                        )
            self._schedule(s, c)
        s["now"] = target

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
            preemption=None,
            resume_token=None,
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
            execution_mode="logical_simulation",
            failure=job["failure"],
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
            for n in inventory(state["nodes"], state["allocations"], state["now"])
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
        allowed["decisions"] = {
            k: p
            for k, p in state["decisions"].items()
            if actor.role == "administrator"
            or all(x["owner_id"] == actor.owner_id for x in p["candidates"])
        }
        # Full internal job timing/specifications belong to authority, not read projections.
        for job in allowed["jobs"].values():
            for field in ("completion_at", "specification", "timeout_at"):
                job.pop(field, None)
        for allocation in allowed["allocations"].values():
            allocation.pop("parameters", None)
        return allowed

    def decision_snapshot(self):
        s = self.store.read()
        active = [a for a in s["allocations"].values() if a["state"] in ACTIVE]
        return m.DecisionSnapshot(
            version=self._version(s),
            now=s["now"],
            execution_mode="logical_simulation",
            inventory=inventory(s["nodes"], s["allocations"], s["now"]),
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
                    status="executing" if a["state"] == "Running" else "reserved",
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
            parse(m.AllocationView, {k: a[k] for k in m.AllocationView.model_fields})
            for a in s["allocations"].values()
            if visible(a) and selected("allocation", a["allocation_id"])
        )
        jobs = tuple(
            parse(m.JobView, {k: j[k] for k in m.JobView.model_fields})
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
        decisions = tuple(
            parse(m.DecisionPlan, p)
            for p in s["decisions"].values()
            if (
                query.global_scope
                or (
                    p["candidates"]
                    and all(x["owner_id"] == query.actor.owner_id for x in p["candidates"])
                )
            )
        )
        actions = tuple(
            m.LegalAction(
                operation="allocation.release",
                entity=self._ref("allocation", a.allocation_id),
                explanation="Return this lease explicitly",
            )
            for a in allocations
            if a.state in ACTIVE | {"Pending"}
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
            decisions=decisions,
            legal_actions=actions,
            next_cursor=m.EventCursor(epoch=s["epoch"], after_sequence=s["seq"]),
        )

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
