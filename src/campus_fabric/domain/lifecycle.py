"""Interruption, generation and clock transitions under the kernel transaction.

Physical side effects are outbox messages. Only a fenced executor observation
can remove a live CPU claim. Logical execution acknowledges within the model.
"""
from __future__ import annotations

import json

from campus_fabric.contracts import models as m
from campus_fabric.domain.errors import Rejected
from campus_fabric.domain.placement import iter_inventory, validate_node
from campus_fabric.domain.state_views import (
    HELD_ALLOCATIONS,
    LIVE_ALLOCATIONS,
    LIVE_JOBS,
    projected_map_changes,
    values_where,
)
from campus_fabric.policies.reclaim import RETURN_WEIGHTS, ReclaimInputs, plan_reclaim

TERMINAL_JOBS = {"Succeeded", "Failed", "Cancelled"}
TERMINAL_ALLOCATIONS = {"Released", "Expired", "Evicted"}


def plain(value):
    return value.model_dump(mode="json")


def parse(cls, value):
    return cls.model_validate_json(json.dumps(value))


class LifecycleMixin:
    @staticmethod
    def _retired_node_ids(s):
        if not s.get("retired"):
            return set()
        retired_nodes = {p["node_id"] for record in s.get("retired", {}).values()
                         for p in (record["placement"] or {}).get("slices", [])}
        retired_devices = {d for record in s.get("retired", {}).values()
                           for d in record.get("physical_gpu_ids", [])}
        retired_nodes.update(n["node_id"] for n in s["nodes"].values()
                             if any(d["physical_gpu_id"] in retired_devices for d in n["devices"]))
        return retired_nodes

    def _inventory_nodes(self, s, node_ids=None):
        # Derive immutable views without mutating raw nodes/devices.
        # Only quarantined node rows need an overlay; copying all physical devices
        # on every read/commit scales with pool size for no semantic benefit.
        retired = self._retired_node_ids(s)
        nodes = s["nodes"] if not retired else {
            ident: {**node, "state": "NotReady"} if ident in retired else node
            for ident, node in s["nodes"].items()
        }
        # The row's identity is not a resource input. Do not let an accidental
        # duplicate identity collapse two authoritative rows into one claim.
        claims = {index: a for index, a in enumerate(values_where(
            s["allocations"], state=HELD_ALLOCATIONS
        )) if a.get("placement") is not None}
        return iter_inventory(nodes, claims, s["now"], node_ids)

    def _inventory(self, s):
        return tuple(self._inventory_nodes(s))

    def _audit_resources(self, s):
        """Establish the global baseline at startup and after structural changes.

        Availability and recall/quarantine state are derived, so neutralize those
        fields only for the registration-time topology constraints. The full
        claim fold below still uses the actual stored state and quarantine.
        """
        previous = []
        for key in sorted(s["nodes"]):
            node = parse(m.NodeInventory, s["nodes"][key])
            if key != node.node_id:
                raise ValueError("Inventory node key mismatch")
            topology = node.model_copy(update={
                "available": node.capacity,
                "devices": tuple(d.model_copy(update={"state": "available"}) for d in node.devices),
            })
            validate_node(topology, tuple(previous))
            previous.append(topology)
        self._inventory(s)

    @staticmethod
    def _resource_row(value):
        if not isinstance(value, dict):
            raise ValueError("Resource entity must be an object")
        return value

    @classmethod
    def _held_placement(cls, value):
        cls._resource_row(value)
        return value.get("placement") if value["state"] in HELD_ALLOCATIONS else None

    def _validate_resources(self, s):
        """Validate the actual post-scheduling resource changes in this transaction.

        The SQL originals and current entities determine the affected set, not
        command names or caller-maintained dirty flags. Every affected node is
        folded against all its current claims. A plain snapshot or structural
        replacement has no bounded proof and therefore needs the global audit.
        """
        nodes = projected_map_changes(s, "nodes", self._resource_row)
        retired = projected_map_changes(s, "retired", self._resource_row)
        if nodes is None or retired is None or nodes or retired:
            self._audit_resources(s)
            return
        claims = projected_map_changes(s, "allocations", self._held_placement)
        if claims is None:
            self._audit_resources(s)
            return
        affected = set()
        for _, before, after in claims:
            for placement in (before, after):
                if placement is not None:
                    affected.update(p.node_id for p in parse(m.Placement, placement).slices)
        if affected:
            tuple(self._inventory_nodes(s, affected))

    def _save_state(self, connection, s):
        self._validate_resources(s)
        self.store.save(connection, s)

    def acknowledge_dispatch(self, dispatch_id, epoch):
        # Transport bookkeeping only: no clock, placement, or decision version mutation.
        with self.store.transaction() as connection:
            s = self.store.load(connection)
            entry = s["outbox"].get(dispatch_id) if s["epoch"] == epoch else None
            if entry:
                entry.update(status="delivered", acknowledged_at=s["now"])
                command = entry["command"]
                allocation = s["allocations"].get(command["attempt"]["allocation_id"])
                plan = allocation.get("reclaim_plan") if allocation else None
                if (plan and command["action"] in {"checkpoint", "stop"}
                        and plan["attempt_id"] == command["attempt"]["attempt_id"]):
                    plan.setdefault(command["action"] + "_dispatched_at", s["now"])
                self._save_state(connection, s)

    def reconcile_retired(self, event):
        """Trusted driver stop receipt removes a private old-epoch quarantine, never work."""
        with self.store.transaction() as connection:
            s = self.store.load(connection)
            key = event.attempt.attempt_id
            record = s.get("retired", {}).get(key)
            if (not record or record["epoch"] != event.version.epoch
                    or record["attempt"] != plain(event.attempt)
                    or event.kind not in {"stopped", "completed", "failed"}):
                return False
            del s["retired"][key]
            s["revision"] += 1
            self._refresh_blocks(s)
            c = m.CommandEnvelope(request_id="reconcile-" + event.callback_id,
                actor=m.AuthenticatedActor(actor_id="cpu-driver", role="system"),
                operation="executor.report",
                parameters=m.ExecutorReportParameters(kind="executor.report", event=event))
            self._event(s, c, "execution.retired_reconciled")
            self._schedule(s, c)
            self._save_state(connection, s)
            return True

    @staticmethod
    def _live_job(s, allocation):
        return next((j for j in values_where(
                        s["jobs"], allocation_id=allocation["allocation_id"], state=LIVE_JOBS)
                     if j["allocation_id"] == allocation["allocation_id"]
                     and j["state"] not in TERMINAL_JOBS), None)

    def _apply_lifecycle(self, s, c):
        p = c.parameters
        refs, status, body = [], 200, None
        if c.operation == "allocation.resume":
            a = self._owned(s, "allocations", p.allocation_id, c.actor)
            self._resume(s, c, a)
            refs = [self._ref("allocation", p.allocation_id)]
            status = 202 if a["state"] == "Pending" else 200
            body = self.allocation_http(a)
        elif c.operation == "allocation.checkpoint":
            a = self._owned(s, "allocations", p.allocation_id, c.actor)
            self._checkpoint(s, c, a, p.status, p.artifact_uri)
            refs = [self._ref("allocation", p.allocation_id)]
            body = self.allocation_http(a)
        elif c.operation in {"test.contributor_demand", "contribution.recall"}:
            notices = self._recall(s, c, p)
            refs = [self._ref("gpu", x) for x in p.device_ids]
            status, body = 201, {"notices": notices}
        elif c.operation == "test.node_state":
            node = s["nodes"].get(p.node_id)
            if node is None:
                raise Rejected("not_found", "Unknown node")
            previous = node["state"]
            node["state"] = p.state
            generations = s.setdefault("node_generations", {})
            if p.state == "NotReady" and previous != "NotReady":
                generations[p.node_id] = generations.get(p.node_id, 0) + 1
                for a in values_where(s["allocations"], state=LIVE_ALLOCATIONS):
                    if a["placement"] and any(x["node_id"] == p.node_id
                                              for x in a["placement"]["slices"]):
                        self._release(s, c, a, "Evicted")
            refs = [self._ref("node", p.node_id)]
            self._event(s, c, "node.state_changed", entities=refs, before=previous, after=p.state)
            body = {"node_id": p.node_id, "state": p.state,
                    "generation": generations.get(p.node_id, 0)}
        elif c.operation == "test.fault":
            s.setdefault("faults", {})[p.target_operation] = {
                "mode": p.mode, "remaining": 1 if p.count is None else p.count,
                "delay_ms": p.delay_ms or 0,
            }
            self._event(s, c, "fault.configured")
            status, body = 204, {}
        elif c.operation == "executor.report":
            self._executor_report(s, c, p.event)
        return refs, status, body, None

    def take_fault(self, target_operation):
        """Atomically consume a transport fault, without a domain revision or clock tick.

        An inactive rule linearizes at this read snapshot: a configuration that
        commits later applies to later checks. An active read is not a claim;
        recheck under the writer lock so competing requests, replacement and
        reset cannot consume a stale rule or the same remaining count twice.
        """
        with self.store.snapshot() as state:
            fault = state.get("faults", {}).get(target_operation)
            if not fault or fault["mode"] == "none" or not fault["remaining"]:
                return None
        with self.store.transaction() as connection:
            s = self.store.load(connection)
            fault = s.get("faults", {}).get(target_operation)
            if not fault or fault["mode"] == "none" or not fault["remaining"]:
                return None
            fault["remaining"] -= 1
            self._save_state(connection, s)
            return {"mode": fault["mode"], "delay_ms": fault["delay_ms"]}

    def _recall(self, s, c, p):
        devices = {d["device_id"]: (node, d)
                   for node in s["nodes"].values() for d in node["devices"]}
        if not p.device_ids or any(x not in devices for x in p.device_ids):
            raise Rejected("invalid_command", "Every recalled GPU must exist")
        if c.operation == "contribution.recall" and c.actor.role != "administrator":
            if any(devices[x][0]["contributor_owner_id"] != c.actor.owner_id
                   for x in p.device_ids):
                raise Rejected("forbidden", "Contributor may recall only owned GPUs")
        # The notice has one owner. Test control may cover several owners atomically.
        grouped = {}
        for ident in p.device_ids:
            grouped.setdefault(devices[ident][0]["contributor_owner_id"], []).append(ident)
        notices = []
        for owner, ids in sorted(grouped.items()):
            notice = plain(m.ContributorNotice(
                notice_id=self._id(s, "notice"), contributor_owner_id=owner,
                device_ids=tuple(ids), issued_at=s["now"],
                deadline=s["now"] + p.notice_seconds,
                unavailable_until=s["now"] + p.notice_seconds + p.duration_seconds,
            ))
            s.setdefault("recalls", {})[notice["notice_id"]] = notice
            notices.append(notice)
        self._refresh_blocks(s)
        for a in values_where(s["allocations"], state=LIVE_ALLOCATIONS):
            if not a["placement"] or a["state"] in TERMINAL_ALLOCATIONS:
                continue
            held = [devices[d][1] for part in a["placement"]["slices"] for d in part["device_ids"]]
            affected = []
            for notice in notices:
                if any(self._device_overlap(d, devices[n][1])
                       for d in held for n in notice["device_ids"]):
                    affected.append(notice)
            if not affected:
                continue
            new_preemption = a.get("preemption") is None
            deadline = min(n["deadline"] for n in affected)
            if a.get("preemption") is not None:
                deadline = min(deadline, a["preemption"]["deadline"])
                cutoff = min(deadline, a["preemption"]["checkpoint_due"])
            else:
                cutoff = min(deadline, s["now"] + a["parameters"]["checkpoint_timeout_seconds"])
            a["preemption"] = {"requested_at": s["now"], "deadline": deadline,
                               "checkpoint_due": cutoff, "reason": "contributor_recall"}
            before = a["state"]
            a["state"] = "PreemptRequested"
            job = self._live_job(s, a)
            if new_preemption:
                a["checkpoint"] = None
                if job:
                    job["checkpoint"] = None
                    job.pop("remaining_seconds", None)
                    job.pop("model_continuation", None)
            if job and not job.get("pending_finish") and not a.get("stop_requested"):
                if job["execution_mode"] == "cpu_reference" and c.operation == "contribution.recall":
                    self._plan_reclaim(s, c, a, job, p.return_policy,
                                       [notice["notice_id"] for notice in affected])
                else:
                    job["state"] = "Checkpointing"
                    self._attempt_state(s, job, "checkpointing")
                    if job["execution_mode"] == "cpu_reference":
                        self._dispatch(s, job, "checkpoint")
            self._event(s, c, "allocation.preempt_requested", a["owner_id"],
                        [self._ref("allocation", a["allocation_id"])],
                        before=before, after=a["state"])
        return notices

    def _plan_reclaim(self, s, c, allocation, job, return_policy, notice_ids):
        observation = job.get("observed_progress") or {}
        previous = allocation.get("reclaim_plan")
        inputs = ReclaimInputs(
            now=s["now"], checkpoint_due=allocation["preemption"]["checkpoint_due"],
            owner_deadline=allocation["preemption"]["deadline"],
            progress_steps=observation.get("step"), total_steps=observation.get("total_steps"),
            rate_lower=observation.get("rate_lower"), rate_upper=observation.get("rate_upper"),
            checkpoint_cost_upper=observation.get("checkpoint_cost_upper", 0.5),
            stop_cost_upper=getattr(self, "reclaim_stop_cost_upper", 2.0),
            restart_cost_upper=observation.get("restart_cost_upper", 0.5),
            owner_wait_weight=RETURN_WEIGHTS[return_policy],
            observed_at=observation.get("observed_at"), trusted=bool(observation),
            allow_cold_restart=allocation["parameters"]["preemption_mode"] != "checkpoint",
        )
        plan = plan_reclaim(inputs)
        plan.update(return_policy=return_policy, planned_at=s["now"], phase="planned",
                    attempt_id=job["attempt"]["attempt_id"],
                    notice_ids=notice_ids,
                    estimate_basis={"progress": "hmac_verified_worker_observation" if observation else "unavailable",
                        "rate": "recent_observation_envelope" if observation.get("rate_lower") else "unavailable",
                        "checkpoint_cost": ("inflated_observation_interval_estimate" if observation
                                            else "declared_cold_start_estimate"),
                        "stop_cost": "configured_escalation_plus_poll_reserve",
                        "restart_cost": "declared_reference_profile_estimate",
                        "hard_realtime_guarantee": False},
                    evidence_digest=observation.get("evidence_digest"))
        if previous:
            allocation.setdefault("reclaim_history", []).append(previous)
            plan["stop_at"] = min(plan["stop_at"], previous["stop_at"])
            plan["dispatch_at"] = min(plan["dispatch_at"], previous["dispatch_at"])
            if previous.get("checkpoint_requested_at") is not None:
                plan.update(phase="saving", checkpoint_requested_at=previous["checkpoint_requested_at"])
                if previous.get("checkpoint_dispatched_at") is not None:
                    plan["checkpoint_dispatched_at"] = previous["checkpoint_dispatched_at"]
            plan["notice_ids"] = sorted(set(notice_ids + previous.get("notice_ids", [])))
        allocation["reclaim_plan"] = plan
        self._event(s, c, "recall.planned", allocation["owner_id"],
                    [self._ref("allocation", allocation["allocation_id"])], after=plan["action"])
        if plan["action"] == "stop" or plan["stop_at"] <= s["now"]:
            self._reclaim_stop(s, c, allocation)
        elif plan["dispatch_at"] <= s["now"]:
            self._reclaim_checkpoint(s, allocation, job)

    def _reclaim_checkpoint(self, s, allocation, job):
        plan = allocation["reclaim_plan"]
        if plan.get("checkpoint_requested_at") is not None:
            return
        plan.update(phase="saving", checkpoint_requested_at=s["now"])
        job["state"] = "Checkpointing"
        self._attempt_state(s, job, "checkpointing")
        self._dispatch(s, job, "checkpoint")

    def _reclaim_stop(self, s, c, allocation):
        strict = (self._live_job(s, allocation) is not None
                  and allocation["parameters"]["preemption_mode"] == "checkpoint"
                  and (allocation.get("checkpoint") or {}).get("verification_status") != "verified")
        self._interrupt(s, c, allocation, "Evicted" if strict else "Preempted")

    @staticmethod
    def _device_overlap(first, second):
        return (first["physical_gpu_id"] == second["physical_gpu_id"]
                and (first["profile"] is None or second["profile"] is None
                     or first["device_id"] == second["device_id"]))

    def _refresh_blocks(self, s):
        devices = {d["device_id"]: d for n in s["nodes"].values() for d in n["devices"]}
        recalled = [devices[ident] for notice in values_where(
                        s.get("recalls", {}), unavailable_until__gt=s["now"])
                    if notice["unavailable_until"] > s["now"]
                    for ident in notice["device_ids"]]
        retired_nodes = self._retired_node_ids(s)
        for node in s["nodes"].values():
            for device in node["devices"]:
                device["state"] = ("quarantined" if node["node_id"] in retired_nodes else
                                   "unavailable" if any(self._device_overlap(device, r)
                                                        for r in recalled) else "available")
        # CPU-only claims also need protection after reset; inventory derives this fence.
        s["retired_nodes"] = sorted(retired_nodes)


    def _checkpoint(self, s, c, a, status, uri=None, *, fact=None):
        preemption = a.get("preemption")
        if (a["state"] != "PreemptRequested" or not preemption
                or s["now"] >= preemption["checkpoint_due"] or a.get("stop_requested")):
            raise Rejected("lease_unavailable", "Checkpoint report is outside its live window")
        # Public ready is a report, not proof. Only the trusted executor can supply verification.
        cp = fact or m.CheckpointFact(checkpoint_id=self._id(s, "checkpoint"),
                                     report_status=status, reported_at=s["now"], artifact_uri=uri)
        a["checkpoint"] = plain(cp)
        if a.get("reclaim_plan"):
            a["reclaim_plan"].update(checkpoint_reported_at=s["now"],
                checkpoint_status=cp.report_status, checkpoint_verified_at=cp.verified_at,
                checkpoint_uri=cp.artifact_uri)
        job = self._live_job(s, a)
        if job:
            job["checkpoint"] = plain(cp)
            if status == "ready" and job["execution_mode"] == "logical_simulation":
                job["remaining_seconds"] = max(0., job["completion_at"] - s["now"])
                job["model_continuation"] = "saved"
            self._attempt_state(s, job, "checkpointing", checkpoint=plain(cp))
        self._event(s, c, "checkpoint.reported", a["owner_id"],
                    [self._ref("allocation", a["allocation_id"])], after=status)
        # A lease can be recalled after placement but before any job exists.
        # There is no execution state to save in that interval. Failure to produce
        # a nonexistent checkpoint must not destroy an otherwise resumable lease.
        strict_failed = (job is not None and status != "ready"
                         and a["parameters"]["preemption_mode"] == "checkpoint")
        self._interrupt(s, c, a, "Evicted" if strict_failed else "Preempted")

    def _dispatch(self, s, job, action, checkpoint=None):
        # One durable command of each action per generation. Delivery itself is repeatable.
        for record in values_where(
            s["outbox"], attempt_id=job["attempt"]["attempt_id"], action=action
        ):
            command = record["command"]
            if command["attempt"] == job["attempt"] and command["action"] == action:
                return command["dispatch_id"]
        command = m.ExecutorCommand(
            dispatch_id=self._id(s, "dispatch"), version=self._version(s),
            attempt=parse(m.AttemptRef, job["attempt"]), action=action,
            mode=job["execution_mode"],
            job=parse(m.JobCreateParameters, job["specification"])
            if action in {"start", "resume"} else None,
            checkpoint=parse(m.CheckpointFact, checkpoint) if checkpoint else None,
        )
        s["outbox"][command.dispatch_id] = {"command": plain(command), "status": "pending",
                                          "acknowledged_at": None}
        allocation = s["allocations"].get(job["allocation_id"])
        plan = allocation.get("reclaim_plan") if allocation else None
        if plan and action in {"checkpoint", "stop"}:
            plan.setdefault(action + "_requested_at", s["now"])
            plan["phase"] = "saving" if action == "checkpoint" else "stopping"
        return command.dispatch_id

    @staticmethod
    def _attempt_state(s, job, state, *, finished=None, checkpoint=None):
        if not job["intent_id"]:
            return
        for attempt in s["intents"][job["intent_id"]]["attempts"]:
            if attempt["ref"] == job["attempt"]:
                attempt["state"] = state
                if finished is not None:
                    attempt["finished_at"] = finished
                if checkpoint is not None:
                    attempt["checkpoint"] = checkpoint

    def _interrupt(self, s, c, a, target):
        if a.get("stop_requested"):
            return
        job = self._live_job(s, a)
        if job and job["execution_mode"] == "cpu_reference" and job.get("physical_live"):
            a["stop_requested"] = True
            a["interruption_target"] = target
            a["state"] = "PreemptRequested"
            self._attempt_state(s, job, "stopping")
            self._dispatch(s, job, "stop")
            return
        self._complete_interruption(s, c, a, target)

    def _complete_interruption(self, s, c, a, target):
        if a.get("reclaim_plan"):
            a["reclaim_plan"].update(phase="complete", stop_confirmed_at=s["now"])
        a.pop("stop_requested", None)
        a.pop("interruption_target", None)
        if target == "Evicted":
            self._release(s, c, a, target)
            return
        if a["placement"] is None:
            return
        deltas = self._delta(a, -1)
        a.update(state="Preempted", placement=None)
        job = self._live_job(s, a)
        if job:
            job.update(state="Preempted", retry_at=None)
            self._attempt_state(s, job, "stopped", finished=s["now"])
            if job["intent_id"]:
                s["intents"][job["intent_id"]]["state"] = "waiting"
        self._event(s, c, "allocation.preempted", a["owner_id"],
                    [self._ref("allocation", a["allocation_id"])], after="Preempted", deltas=deltas)

    def _resume(self, s, c, a):
        if a["state"] == "Pending" and a.get("resume_requested"):
            return
        if a["state"] != "Preempted" or a["expires_at"] <= s["now"]:
            raise Rejected("lease_unavailable", "Only a live preempted lease can resume")
        job = self._live_job(s, a)
        if job and a.get("restart_count", 0) >= a["parameters"]["max_restarts"]:
            raise Rejected("budget_exhausted", "Allocation restart budget exhausted")
        if job and job["execution_mode"] == "cpu_reference":
            cp = job.get("checkpoint")
            if (a["parameters"]["preemption_mode"] == "checkpoint"
                    and (not cp or cp["verification_status"] != "verified")):
                raise Rejected("execution_unconfirmed", "Verified CPU checkpoint is required")
        a.update(state="Pending", resume_requested=True)
        self._event(s, c, "allocation.resume_requested", a["owner_id"],
                    [self._ref("allocation", a["allocation_id"])], after="Pending")
        self._schedule(s, c)

    def _next_attempt(self, s, c, job, *, reason):
        a = s["allocations"][job["allocation_id"]]
        previous = job["attempt"]
        attempt = m.AttemptRef(intent_id=previous["intent_id"],
                              attempt_id=self._id(s, "attempt"),
                              generation=previous["generation"] + 1,
                              allocation_id=a["allocation_id"], job_id=job["job_id"])
        if reason == "restart":
            a["restart_count"] = a.get("restart_count", 0) + 1
        else:
            job["retry_count"] = job.get("retry_count", 0) + 1
        job.update(attempt=plain(attempt), state="Running", retry_at=None,
                   physical_live=job["execution_mode"] == "cpu_reference", finished_at=None)
        job.pop("observed_progress", None)
        if a.get("reclaim_plan"):
            a.setdefault("reclaim_history", []).append(a.pop("reclaim_plan"))
        a["state"] = "Running"
        if job["intent_id"]:
            intent = s["intents"][job["intent_id"]]
            intent["state"] = "running"
            intent["attempts"].append(plain(m.AttemptSnapshot(
                ref=attempt, state="starting" if job["physical_live"] else "running",
                created_at=s["now"], started_at=None if job["physical_live"] else s["now"])))
        cp = job.get("checkpoint") if reason == "restart" else None
        if job["execution_mode"] == "logical_simulation":
            remaining = (job.get("remaining_seconds", s["model"]["duration_seconds"])
                         if reason == "restart" else s["model"]["duration_seconds"])
            job["completion_at"] = s["now"] + remaining
            if cp and cp["report_status"] == "ready":
                job["model_continuation"] = "restored"
            dispatch_id = self._dispatch(s, job, "start")
            s["outbox"][dispatch_id].update(status="model_acknowledged", acknowledged_at=s["now"])
        else:
            verified = cp and cp["verification_status"] == "verified"
            job["resuming_checkpoint"] = bool(verified)
            self._dispatch(s, job, "resume" if verified else "start", cp if verified else None)
        self._event(s, c, "job." + ("restarted" if reason == "restart" else "retried"),
                    job["owner_id"], [self._ref("job", job["job_id"])],
                    after="Running", attempt=attempt)

    def _execution_failed(self, s, c, job, reason):
        a = s["allocations"][job["allocation_id"]]
        if (a["state"] == "Running" and a["expires_at"] > s["now"]
                and job["timeout_at"] > s["now"]
                and job.get("retry_count", 0) < (job["specification"].get("max_retries") or 0)):
            job.update(state="Pending", retry_at=s["now"] + 1, failure=reason)
            self._attempt_state(s, job, "failed", finished=s["now"])
            # Keep the lease in Running to prevent a second job occupying its retry slot.
            self._event(s, c, "job.retry_waiting", job["owner_id"],
                        [self._ref("job", job["job_id"])], after="Pending")
        else:
            self._finish_job(s, c, job, "Failed", reason)

    def _finish_job(self, s, c, job, state, reason=None, *, settle=True):
        if job["state"] in TERMINAL_JOBS:
            return
        if job["execution_mode"] == "cpu_reference" and job.get("physical_live"):
            pending = {"state": state, "reason": reason, "settle": settle}
            changed = job.get("pending_finish") != pending
            job["pending_finish"] = pending
            if changed:
                self._event(s, c, "job.stop_requested", job["owner_id"],
                            [self._ref("job", job["job_id"])], after="stopping",
                            attempt=parse(m.AttemptRef, job["attempt"]))
            self._attempt_state(s, job, "stopping")
            self._dispatch(s, job, "stop")
            return
        job["retry_at"] = None
        self._finish_job_final(s, c, job, state, reason, settle=settle)

    def _release(self, s, c, a, state):
        if a["state"] in TERMINAL_ALLOCATIONS and not a.get("pending_terminal"):
            return
        job = self._live_job(s, a)
        if job and job["execution_mode"] == "cpu_reference" and job.get("physical_live"):
            a["pending_terminal"] = state
            a["state"] = "PreemptRequested"
            self._finish_job(s, c, job, "Failed" if state in {"Expired", "Evicted"} else "Cancelled",
                             "Lease " + state.lower(), settle=False)
            return
        a.pop("pending_terminal", None)
        a.pop("stop_requested", None)
        a.pop("interruption_target", None)
        self._release_final(s, c, a, state)

    def _executor_report(self, s, c, event):
        recorded = s.setdefault("callbacks", {}).get(event.callback_id)
        if recorded is not None:
            if recorded != plain(event):
                raise Rejected("idempotency_conflict", "Callback identity changed payload")
            return
        job = s["jobs"].get(event.attempt.job_id)
        if (event.version.epoch != s["epoch"] or event.version.revision >= s["revision"]
                or job is None or job["attempt"] != plain(event.attempt)
                or job["state"] in TERMINAL_JOBS
                or (job["state"] in {"Pending", "Preempted"} and not job.get("physical_live"))
                or event.observed_at > s["now"]
                or event.mode != job["execution_mode"]):
            raise Rejected("attempt_stale", "Callback does not identify the live generation")
        a = s["allocations"][job["allocation_id"]]
        physical = job["execution_mode"] == "cpu_reference"
        if event.kind == "checkpoint_reported":
            if not physical:
                raise Rejected("invalid_command", "Logical checkpoints use the explicit report port")
            self._checkpoint(s, c, a, event.checkpoint.report_status,
                             event.checkpoint.artifact_uri, fact=event.checkpoint)
        elif event.kind in {"completed", "failed", "stopped"}:
            if not physical and event.kind == "completed" and job["completion_at"] > s["now"]:
                raise Rejected("invalid_command", "Premature modeled completion")
            # This trusted port is not reachable from public HTTP credentials.
            job["physical_live"] = False
            if event.result_uri:
                job["result_uri"] = event.result_uri
            if a.get("reclaim_plan"):
                a["reclaim_plan"].update(phase="complete", stop_confirmed_at=s["now"])
                if event.kind == "completed":
                    a["reclaim_plan"]["completed_at"] = s["now"]
            if job.get("pending_finish"):
                pending = job.pop("pending_finish")
                self._finish_job(s, c, job, pending["state"], pending["reason"],
                                 settle=pending["settle"])
            elif a.get("stop_requested"):
                self._complete_interruption(s, c, a, a["interruption_target"])
            elif event.kind == "completed":
                self._finish_job(s, c, job, "Succeeded")
                if a.get("reclaim_plan") and a.get("placement") is not None:
                    self._complete_interruption(s, c, a, "Preempted")
            elif event.kind == "failed":
                self._execution_failed(s, c, job, event.error.detail)
            else:
                self._execution_failed(s, c, job, "Unexpected execution stop")
            if a.get("pending_terminal"):
                self._release(s, c, a, a["pending_terminal"])
            elif a.get("stop_requested") and a["state"] not in TERMINAL_ALLOCATIONS:
                # A concurrent cancel/timeout settles the job, but the same confirmed
                # stop must still reconcile the contributor's allocation interruption.
                self._complete_interruption(s, c, a, a["interruption_target"])
        elif event.kind == "started":
            if job["intent_id"]:
                for attempt in s["intents"][job["intent_id"]]["attempts"]:
                    if attempt["ref"] == job["attempt"]:
                        attempt.update(state="running", started_at=event.observed_at)
            if job.get("resuming_checkpoint") and (job.get("checkpoint") or {}).get("verification_status") == "verified":
                cp = job["checkpoint"]
                cp.update(continuation_status="applied", resumed_attempt_id=job["attempt"]["attempt_id"])
                a["checkpoint"] = dict(cp)
                self._attempt_state(s, job, "running", checkpoint=dict(cp))
        elif event.kind == "unknown":
            self._attempt_state(s, job, "quarantined")
        elif event.kind == "progress" and event.progress is not None and physical:
            if event.progress.observed_at > event.observed_at:
                raise Rejected("invalid_command", "Progress cannot observe future work")
            previous = job.get("observed_progress")
            if previous is None or event.progress.observed_at > previous["observed_at"]:
                job["observed_progress"] = plain(event.progress)
        elif event.kind not in {"progress", "alive"}:
            raise Rejected("invalid_command", "Unsupported execution observation")
        s["callbacks"][event.callback_id] = plain(event)
        self._event(s, c, "execution." + event.kind, job["owner_id"],
                    [self._ref("job", job["job_id"])], attempt=event.attempt)

    def _advance(self, s, c, target):
        while True:
            due = []
            for a in values_where(s["allocations"], state=LIVE_ALLOCATIONS):
                if (a["expires_at"] is not None and a["state"] not in TERMINAL_ALLOCATIONS
                        and not a.get("pending_terminal") and a["expires_at"] <= target):
                    due.append((a["expires_at"], 0, a["allocation_id"]))
                if (a["state"] == "PreemptRequested" and a.get("preemption")
                        and not a.get("stop_requested") and not a.get("pending_terminal")
                        and a["preemption"]["checkpoint_due"] <= target):
                    due.append((a["preemption"]["checkpoint_due"], 1, a["allocation_id"]))
                plan = a.get("reclaim_plan")
                if (plan and a["state"] == "PreemptRequested" and not a.get("stop_requested")
                        and not a.get("pending_terminal") and plan["phase"] != "complete"):
                    if plan["stop_at"] <= target:
                        due.append((plan["stop_at"], 7, a["allocation_id"]))
                    elif (plan.get("checkpoint_requested_at") is None
                          and plan["dispatch_at"] <= target):
                        due.append((plan["dispatch_at"], 6, a["allocation_id"]))
            for job in values_where(s["jobs"], state=LIVE_JOBS):
                if job["state"] in TERMINAL_JOBS or job.get("pending_finish"):
                    continue
                if job["timeout_at"] <= target:
                    due.append((job["timeout_at"], 2, job["job_id"]))
                if (job["execution_mode"] == "logical_simulation"
                        and job["state"] in {"Running", "Checkpointing"}
                        and job["completion_at"] <= target):
                    due.append((job["completion_at"], 3, job["job_id"]))
                if job.get("retry_at") is not None and job["retry_at"] <= target:
                    due.append((job["retry_at"], 5, job["job_id"]))
            for notice in values_where(s.get("recalls", {}), unavailable_until__gt=s["now"]):
                if s["now"] < notice["unavailable_until"] <= target:
                    due.append((notice["unavailable_until"], 4, notice["notice_id"]))
            if not due:
                break
            at = min(x[0] for x in due)
            s["now"] = max(s["now"], at)
            # Numeric sequence order, not lexical e0-a10 before e0-a2.
            current = sorted((x for x in due if x[0] == at),
                             key=lambda x: (x[1], int(''.join(filter(str.isdigit, x[2].split('-')[-1])))))
            for _, priority, ident in current:
                if priority == 0:
                    self._release(s, c, s["allocations"][ident], "Expired")
                elif priority == 1:
                    a = s["allocations"][ident]
                    if a["state"] == "PreemptRequested" and not a.get("pending_terminal"):
                        strict = (self._live_job(s, a) is not None
                                  and a["parameters"]["preemption_mode"] == "checkpoint")
                        self._interrupt(s, c, a, "Evicted" if strict else "Preempted")
                elif priority == 4:
                    self._refresh_blocks(s)
                    self._event(s, c, "recall.ended")
                elif priority in {6, 7}:
                    a = s["allocations"][ident]
                    if a["state"] != "PreemptRequested" or a.get("stop_requested") or a.get("pending_terminal"):
                        continue
                    if priority == 7:
                        self._reclaim_stop(s, c, a)
                    else:
                        job = self._live_job(s, a)
                        if job:
                            self._reclaim_checkpoint(s, a, job)
                        else:
                            self._interrupt(s, c, a, "Preempted")
                else:
                    job = s["jobs"][ident]
                    if job["state"] in TERMINAL_JOBS or job.get("pending_finish"):
                        continue
                    if priority == 2:
                        self._finish_job(s, c, job, "Failed", "Job timeout")
                    elif priority == 3 and job["state"] in {"Running", "Checkpointing"}:
                        self._finish_job(s, c, job, "Succeeded")
                    elif priority == 5 and job["retry_at"] is not None:
                        a = s["allocations"][job["allocation_id"]]
                        if a["state"] == "Running":
                            self._next_attempt(s, c, job, reason="retry")
                        else:
                            job["retry_at"] = None
            self._schedule(s, c)
        s["now"] = target
