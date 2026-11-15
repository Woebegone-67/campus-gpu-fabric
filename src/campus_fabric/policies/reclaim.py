"""A bounded bilateral decision for an announced recall, using observations only.

The objective is remaining user compute after reclaim plus save/restart overhead
and the owner's weighted wait. It is deliberately not a claim to minimize total
compute or rollback alone. One second of continued useful work cannot be treated
as free; the separate owner wait term prices that tradeoff explicitly.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

RETURN_WEIGHTS = {"return_first": 1.0, "preserve_work": 0.25}
REPORT_MARGIN_SECONDS = 0.1


@dataclass(frozen=True)
class ReclaimInputs:
    now: float
    checkpoint_due: float
    owner_deadline: float
    progress_steps: int | None
    total_steps: int | None
    rate_lower: float | None
    rate_upper: float | None
    checkpoint_cost_upper: float
    stop_cost_upper: float
    restart_cost_upper: float
    owner_wait_weight: float = 1.0
    observed_at: float | None = None
    trusted: bool = False
    allow_cold_restart: bool = True


def plan_reclaim(inputs: ReclaimInputs) -> dict:
    x = inputs
    for key in ("now", "checkpoint_due", "owner_deadline", "checkpoint_cost_upper",
                "stop_cost_upper", "restart_cost_upper", "owner_wait_weight"):
        value = getattr(x, key)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Reclaim times, costs and weight must be finite and nonnegative")
    stop_at = max(x.now, min(x.checkpoint_due, x.owner_deadline - x.stop_cost_upper))
    latest_save = stop_at - x.checkpoint_cost_upper - REPORT_MARGIN_SECONDS
    known = (x.trusted and x.observed_at is not None and 0 <= x.now - x.observed_at <= 1.0
             and x.progress_steps is not None and x.total_steps is not None
             and 0 <= x.progress_steps <= x.total_steps and x.total_steps > 0
             and x.rate_lower is not None and x.rate_upper is not None
             and math.isfinite(x.rate_lower) and math.isfinite(x.rate_upper)
             and 0 < x.rate_lower <= x.rate_upper)
    candidates = []
    remaining = (x.total_steps - x.progress_steps) / x.rate_lower if known else None

    def candidate(action, dispatch_at, wait, remaining_compute, overhead, feasible=True):
        deadline_feasible = wait <= max(0.0, x.owner_deadline - x.now)
        feasible = feasible and deadline_feasible
        score = (remaining_compute + overhead + x.owner_wait_weight * wait
                 if remaining_compute is not None and feasible else None)
        row = dict(action=action, dispatch_at=dispatch_at, estimated_wait=wait,
                   remaining_compute=remaining_compute, overhead=overhead, score=score,
                   feasible=feasible, deadline_feasible=deadline_feasible)
        candidates.append(row)
        return row

    # Stopping without a new save loses the currently observed attempt's progress.
    stop = candidate("stop", x.now, x.stop_cost_upper,
                     x.total_steps / x.rate_lower if known else None, x.restart_cost_upper,
                     x.allow_cold_restart)
    immediate = candidate("save_now", x.now, x.checkpoint_cost_upper + x.stop_cost_upper,
                          remaining, x.checkpoint_cost_upper + x.restart_cost_upper,
                          latest_save >= x.now)
    delay = max(0.0, latest_save - x.now)
    candidate("save_at", max(x.now, latest_save), delay + x.checkpoint_cost_upper + x.stop_cost_upper,
              max(0.0, remaining - delay) if known else None,
              x.checkpoint_cost_upper + x.restart_cost_upper,
              known and delay > 0 and remaining > delay)
    candidate("finish_first", max(x.now, latest_save),
              remaining + x.stop_cost_upper if known else 0.0,
              0.0, 0.0, known and remaining <= max(0.0, latest_save - x.now))
    if latest_save < x.now:
        selected, reason = stop, "insufficient_window"
    elif not known:
        selected, reason = immediate, "cold_observation"
    else:
        selected = min((row for row in candidates if row["feasible"]),
                       key=lambda row: (round(row["score"], 9), row["estimated_wait"], row["action"]))
        reason = {"finish_first": "finish_fits", "save_at": "delay_valuable",
                  "save_now": "save_minimizes_cost", "stop": "save_not_worth_cost"}[selected["action"]]
    return {**selected, "stop_at": stop_at, "reason": reason,
            "completion_feasible": selected["action"] != "stop" or x.allow_cold_restart,
            "inputs": asdict(x), "candidates": candidates, "version": "reclaim-v1"}
