"""Owner-scoped recall progress from one authoritative, read-only snapshot."""

from __future__ import annotations

from campus_fabric.domain.state_views import HELD_ALLOCATIONS, LIVE_JOBS, values_where


def _decision(plan):
    if not plan:
        return None
    return {key: plan[key] for key in ("action", "dispatch_at", "stop_at", "phase", "reason",
                                      "return_policy")}


def _phase(state, allocation):
    intent = state["intents"].get(allocation.get("intent_id"))
    jobs = tuple(values_where(state["jobs"], allocation_id=allocation["allocation_id"],
                              state=LIVE_JOBS))
    current_attempts = {job["attempt"]["attempt_id"] for job in jobs}
    if intent and any(attempt["ref"]["attempt_id"] in current_attempts
                      and attempt["state"] == "quarantined" for attempt in intent["attempts"]):
        return "attention"
    if allocation["state"] == "Preempted":
        return "interrupted"
    if allocation.get("stop_requested") or allocation.get("pending_terminal"):
        return "stopping"
    return "saving"


def recall_status(state, actor, overlaps):
    """Expose own work detail and own device counts, never another borrower's records."""
    result = {"version": {"epoch": state["epoch"], "revision": state["revision"]},
              "now": state["now"], "execution_mode": state["model"]["execution_mode"],
              "borrowed": [], "owned": []}
    if actor.owner_id is None:
        return result
    if actor.can_use_compute:
        for allocation in values_where(state["allocations"], owner_id=actor.owner_id,
                                       state=("PreemptRequested", "Preempted")):
            preemption = allocation.get("preemption")
            if not preemption or preemption["reason"] != "contributor_recall":
                continue
            intent = state["intents"].get(allocation.get("intent_id"))
            if (intent and intent["state"] in {"completed", "failed", "cancelled"}
                    and allocation.get("placement") is None):
                continue
            checkpoint = allocation.get("checkpoint") or {}
            result["borrowed"].append({
                "allocation_id": allocation["allocation_id"],
                "intent_id": allocation.get("intent_id"),
                "display_name": intent.get("display_name") if intent else None,
                "state": _phase(state, allocation),
                "checkpoint_due": preemption["checkpoint_due"],
                "deadline": preemption["deadline"],
                "checkpoint_status": checkpoint.get("report_status"),
                "checkpoint_verified": checkpoint.get("verification_status") == "verified",
                **({"decision": _decision(allocation["reclaim_plan"])}
                   if allocation.get("reclaim_plan") else {}),
            })
    if actor.can_contribute_resources:
        devices = {device["device_id"]: device for node in state["nodes"].values()
                   for device in node["devices"]}
        held = [allocation for allocation in values_where(state["allocations"],
                                                          state=HELD_ALLOCATIONS)
                if allocation.get("placement") is not None and allocation.get("preemption")]
        plans = [plan for allocation in state["allocations"].values()
                 for plan in [allocation.get("reclaim_plan")] if plan]
        for notice in state["recalls"].values():
            if notice["contributor_owner_id"] != actor.owner_id:
                continue
            recalled = [devices[ident] for ident in notice["device_ids"] if ident in devices]
            affected = []
            for allocation in held:
                preemption = allocation["preemption"]
                # A later lease/resumed generation must not reopen a completed notice.
                if (allocation["created_at"] > notice["issued_at"]
                        or preemption["reason"] != "contributor_recall"
                        or preemption["requested_at"] < notice["issued_at"]
                        or preemption["deadline"] > notice["deadline"]):
                    continue
                placement_devices = [devices[ident]
                    for part in allocation["placement"]["slices"] for ident in part["device_ids"]
                    if ident in devices]
                if any(overlaps(first, second) for first in recalled for second in placement_devices):
                    affected.append(allocation)
            phases = {_phase(state, allocation) for allocation in affected}
            phase = ("attention" if "attention" in phases else
                     "saving" if "saving" in phases else "stopping" if affected else
                     "ready" if state["now"] < notice["unavailable_until"] else "ended")
            related_plans = ([allocation["reclaim_plan"] for allocation in affected
                              if allocation.get("reclaim_plan")] if affected else
                             [plan for plan in plans if notice["notice_id"] in plan.get("notice_ids", [])])
            decision = (_decision(related_plans[0]) if related_plans
                        and all(all(plan[key] == related_plans[0][key]
                                    for key in ("action", "phase", "reason", "dispatch_at", "stop_at"))
                                for plan in related_plans) else None)
            if decision and not affected:
                decision["phase"] = "complete"
            result["owned"].append({
                "notice_id": notice["notice_id"], "device_ids": list(notice["device_ids"]),
                "issued_at": notice["issued_at"], "deadline": notice["deadline"],
                "unavailable_until": notice["unavailable_until"],
                "state": phase, "held_count": len(affected),
                **({"decision": decision} if decision else {}),
            })
        active = [notice for notice in result["owned"] if notice["state"] != "ended"]
        ended = sorted((notice for notice in result["owned"] if notice["state"] == "ended"),
                       key=lambda notice: (notice["issued_at"], notice["notice_id"]), reverse=True)
        result["owned"] = active + ended[:10]
    result["borrowed"].sort(key=lambda row: (row["checkpoint_due"], row["allocation_id"]))
    result["owned"].sort(key=lambda row: (row["issued_at"], row["notice_id"]), reverse=True)
    return result
