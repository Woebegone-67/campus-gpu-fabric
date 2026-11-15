"""Opt-in empirical renewal matching, extending the frozen one-recall model.

The task/window/source contract is the same as reliability.py. An additional
snapshot.metadata datum owner_use_seconds is REQUIRED for each physical GPU,
with source declared_profile/public_metadata, a current causal timestamp and
matching source_id. It describes past/declared downtime, never the next actual
owner-use duration. No source adapter or production registration is installed.
Demand.known_data must also contain remaining_recoveries (integer zero to two)
and execution_window_seconds (at least 900), sourced from public_metadata or
observed_event with the demand's source_id. These must represent authoritative
remaining recovery budget and min(remaining lease, remaining job timeout), or
their explicitly declared bounds before grant. Missing values never inherit
the simulator's budget/horizon; the wrapper falls back to B2.

Both estimators assume independent repeated windows drawn from completed past
observations, with the declared notice/downtime/checkpoint profile. It rounds
compute up and windows down to one second. Saving makes durable progress;
otherwise a recall restarts the entire job. The original unlimited helper is
retained only as a diagnostic: it returns min(E[T], horizon). The finite-budget
helper used by the policy computes E[min(T,horizon)] and completion probability
within both budget and horizon by truncating each path before averaging. Neither
is a calibrated field prediction or measured completion. Unfinished paths cost
the reporting horizon; they are never labelled successful completion.
The initial interval has no restart cost; each subsequent interval spends its
restart time inside the available window. Durable progress is rounded down to
whole compute seconds. This conservative discretization can change decisions
near a window threshold; it is not a bounded percentage prediction error.

The bounded legal-cohort guard, B2 admission, and candidate contracts are reused
from ReliabilityPolicy. Only one legal action is proposed; no cohort reservation
or production behavior changes. As with the frozen model, unsupported/missing
data falls back to B2. Repeated plans recompute the remaining cohort.
"""

from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import NamedTuple, Sequence

from campus_fabric.contracts.models import (
    MAX_SAFE_INTEGER,
    DecisionPlan,
    DecisionSnapshot,
    ScoreComponent,
    VersionedArtifact,
)
from campus_fabric.policies.baselines import _digest
from campus_fabric.policies.reliability import (
    MAX_SAMPLES,
    REPORT_MARGIN,
    ReliabilityPolicy,
    _number,
    _profile,
    _recent,
    batch_assign,
)

MAX_COMPUTE_SECONDS = 900
DEFAULT_HORIZON = 900.
MAX_RECOVERIES = 2
_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _bounded_number(value, *, positive=False):
    result = _number(value, positive=positive)
    if result > MAX_SAFE_INTEGER:
        raise ValueError("Renewal inputs exceed the scalar contract")
    return result


def renewal_completion_cost(
    compute_seconds: float, checkpoint_seconds: float, windows: Sequence[float],
    notice_seconds: float, *, owner_use_seconds: float = 10.,
    restart_seconds: float = .5, stop_seconds: float = .1,
    horizon_seconds: float = DEFAULT_HORIZON,
) -> float:
    """Bounded historical-window renewal surrogate in completion seconds.

    Finish-or-save is identical for every policy: finish only while retaining
    the checkpoint/stop/report fallback margin; otherwise save immediately if
    that margin fits. There is no finish-during-notice action when saving cannot
    fit. The policy receives no actual future duration or availability sequence.
    """
    duration = _bounded_number(compute_seconds, positive=True)
    checkpoint = _bounded_number(checkpoint_seconds)
    notice = _bounded_number(notice_seconds)
    owner_use = _bounded_number(owner_use_seconds)
    restart = _bounded_number(restart_seconds)
    stop = _bounded_number(stop_seconds)
    horizon = _bounded_number(horizon_seconds, positive=True)
    if duration > MAX_COMPUTE_SECONDS or horizon > DEFAULT_HORIZON:
        raise ValueError("Renewal model supports compute and reporting horizons up to 900 seconds")
    if not windows:
        raise ValueError("At least one completed historical window is required")
    observed = tuple(_bounded_number(window, positive=True) for window in windows)
    if any(window < 1. for window in observed):
        raise ValueError("Renewal windows must be at least one second")
    samples = tuple(math.floor(window) for window in observed)
    remaining = math.ceil(duration)
    cycle_tail = notice + owner_use
    if checkpoint + stop + REPORT_MARGIN >= notice:
        resumed_successes = sum(window >= restart + remaining for window in samples)
        # A failed resumed attempt discards all progress and loops to this state.
        resumed = (restart + remaining + sum(window + cycle_tail for window in samples
            if window < restart + remaining) / resumed_successes) if resumed_successes else math.inf
        initial = mean(float(remaining) if remaining <= window else window + cycle_tail + resumed
                       for window in samples)
        return min(initial, horizon)

    finish_slack = notice - checkpoint - stop - REPORT_MARGIN
    resumed = [0.] * (remaining + 1)
    for left in range(1, remaining + 1):
        terms, self_loops = [], 0
        for window in samples:
            if restart <= window and restart + left <= window + finish_slack:
                terms.append(restart + left)
                continue
            progress = math.floor(max(0., window - restart))
            if progress == 0:
                # T[left] occurs again on this edge; solve the linear recurrence.
                self_loops += 1
                terms.append(window + cycle_tail)
            else:
                terms.append(window + cycle_tail + resumed[left - progress])
        resumed[left] = sum(terms) / (len(samples) - self_loops) if self_loops < len(samples) else math.inf
    initial = mean(float(remaining) if remaining <= window + finish_slack
                   else window + cycle_tail + resumed[remaining - window]
                   for window in samples)
    # Never clip intermediate states: averaging clipped states is not min(E[T],H).
    return min(initial, horizon)


def renewal_cost_matrix(tasks: Sequence[dict], devices: Sequence[dict]) -> tuple[tuple[float, ...], ...]:
    """Same observed-only helper schema as reliability.cost_matrix plus downtime.

    Devices may specify owner_use_seconds (default 10 only for direct helper use).
    The DecisionSnapshot wrapper below requires an explicit sourced declaration.
    """
    return tuple(tuple(renewal_completion_cost(
        task["estimated_compute_seconds"], task["checkpoint_seconds"],
        device["windows"], device["notice_seconds"],
        owner_use_seconds=device.get("owner_use_seconds", 10.),
        restart_seconds=task.get("restart_seconds", .5),
        stop_seconds=task.get("stop_seconds", .1),
    ) for device in devices) for task in tasks)


class BudgetedCompletionEstimate(NamedTuple):
    expected_restricted_seconds: float
    completion_probability: float


def budgeted_completion_estimate(
    compute_seconds: float, checkpoint_seconds: float, windows: Sequence[float],
    notice_seconds: float, *, owner_use_seconds: float = 10.,
    restart_seconds: float = .5, stop_seconds: float = .1,
    horizon_seconds: float = DEFAULT_HORIZON, max_recoveries: int = 2,
) -> BudgetedCompletionEstimate:
    """Empirical finite-budget E[min(T,H)] and P(completion by H).

    Each unfinished path costs its remaining reporting horizon. Initial execution
    is free of restart overhead; at most two subsequent attempts are supported.
    Samples are completed historical windows, never the held-out device calendar.
    These are discrete-model estimates, not calibrated field probabilities.
    """
    duration = _bounded_number(compute_seconds, positive=True)
    checkpoint = _bounded_number(checkpoint_seconds)
    notice = _bounded_number(notice_seconds)
    owner_use = _bounded_number(owner_use_seconds)
    restart = _bounded_number(restart_seconds)
    stop = _bounded_number(stop_seconds)
    horizon = _bounded_number(horizon_seconds, positive=True)
    if duration > MAX_COMPUTE_SECONDS or horizon > DEFAULT_HORIZON:
        raise ValueError("Budgeted model supports compute and reporting horizons up to 900 seconds")
    if type(max_recoveries) is not int or not 0 <= max_recoveries <= MAX_RECOVERIES:
        raise ValueError("Budgeted matching supports an explicit integer recovery budget from zero to two")
    if not 1 <= len(windows) <= MAX_SAMPLES:
        raise ValueError("Budgeted model requires one to 32 historical window samples")
    observations = tuple(_bounded_number(window, positive=True) for window in windows)
    if any(window < 1. for window in observations):
        raise ValueError("Budgeted windows must be at least one second")
    samples = tuple(math.floor(window) for window in observations)
    can_save = checkpoint + stop + REPORT_MARGIN < notice
    finish_slack = notice - checkpoint - stop - REPORT_MARGIN if can_save else 0.

    @lru_cache(None)
    def estimate(left, budget, remaining_horizon, resumed):
        startup = restart if resumed else 0.
        costs, probabilities = [], []
        for window in samples:
            finish_time = startup + left
            if startup <= window and finish_time <= window + finish_slack:
                costs.append(min(remaining_horizon, finish_time))
                probabilities.append(float(finish_time <= remaining_horizon))
                continue
            elapsed = window + notice + owner_use
            if budget == 0 or elapsed >= remaining_horizon:
                costs.append(remaining_horizon)
                probabilities.append(0.)
                continue
            progress = math.floor(max(0., window - startup)) if can_save else 0
            continuation = estimate(left - progress, budget - 1, remaining_horizon - elapsed, True)
            costs.append(min(remaining_horizon, elapsed + continuation.expected_restricted_seconds))
            probabilities.append(continuation.completion_probability)
        return BudgetedCompletionEstimate(mean(costs), mean(probabilities))

    return estimate(math.ceil(duration), max_recoveries, horizon, False)


def budgeted_completion_cost(*args, **kwargs) -> float:
    """Cost-only convenience interface for the bounded empirical expectation."""
    return budgeted_completion_estimate(*args, **kwargs).expected_restricted_seconds


def budgeted_cost_matrix(tasks: Sequence[dict], devices: Sequence[dict]) -> tuple[tuple[float, ...], ...]:
    """Observed-only finite-budget matrix; every task explicitly supplies its budget."""
    return tuple(tuple(budgeted_completion_cost(
        task["estimated_compute_seconds"], task["checkpoint_seconds"],
        device["windows"], device["notice_seconds"],
        owner_use_seconds=device.get("owner_use_seconds", 10.),
        restart_seconds=task.get("restart_seconds", .5), stop_seconds=task.get("stop_seconds", .1),
        max_recoveries=task["remaining_recoveries"],
    ) for device in devices) for task in tasks)


class RenewalReliabilityPolicy:
    """Research policy with conservative inherited guards; never the default P."""

    def __init__(self, *, max_candidates=512):
        self.guard = ReliabilityPolicy(max_candidates=max_candidates)

    def plan(self, snapshot: DecisionSnapshot) -> DecisionPlan:
        checked = self.guard.plan(snapshot)
        base = self.guard.baseline.plan(snapshot)
        fingerprint = _digest({"source": _SOURCE_SHA, "guard": checked.policy.sha256,
                               "compute_bound": MAX_COMPUTE_SECONDS, "horizon": DEFAULT_HORIZON,
                               "max_recoveries": MAX_RECOVERIES})

        def result(reason, *, selected=None, candidates=None):
            return base.model_copy(update={
                "decision_id": "decision-" + _digest([fingerprint, snapshot.model_dump(mode="json")])[:32],
                "policy": VersionedArtifact(name="R-renewal", version="0.1.0", sha256=fingerprint),
                "model": VersionedArtifact(name="past-window-budgeted-renewal", version="0.1.0", sha256=_SOURCE_SHA),
                "candidates": candidates if candidates is not None else base.candidates,
                "selected_candidate_id": selected if selected is not None else base.selected_candidate_id,
                "rationale": reason,
                "tie_break_rule": "B2 admission; bounded renewal-completion matching; equal totals preserve "
                                  "B2 first choice, then node/device IDs; kernel revalidates.",
            })

        def fallback(reason):
            return result("Renewal matching inactive; B2 fallback: " + reason)

        # Composition deliberately retains the frozen guard's strict inventory,
        # cohort feasibility, profile provenance, timestamp and numeric checks.
        if checked.rationale.startswith("Reliability matching inactive;"):
            return fallback(checked.rationale)
        legal = [c for c in base.candidates if c.feasibility == "legal"]
        node_ids = sorted({c.placement.slices[0].node_id for c in legal})
        demand_ids = list(dict.fromkeys(c.demand_id for c in legal))[:len(node_ids)]
        by_demand = {d.demand_id: d for d in snapshot.demands}
        by_node = {n.node_id: n for n in snapshot.inventory}
        tasks, devices = [], []
        try:
            for ident in demand_ids:
                task = {name: _profile(by_demand[ident].known_data, name, ident,
                    {"declared_profile", "history"}, snapshot.now, default=default, positive=positive)
                    for name, default, positive in (("estimated_compute_seconds", None, True),
                        ("checkpoint_seconds", None, False), ("restart_seconds", .5, False),
                        ("stop_seconds", .1, False))}
                budget = _profile(by_demand[ident].known_data, "remaining_recoveries", ident,
                                  {"public_metadata", "observed_event"}, snapshot.now)
                if not budget.is_integer() or not 0 <= budget <= MAX_RECOVERIES:
                    raise ValueError("Only explicit recovery budgets from zero to two are supported")
                execution_window = _profile(by_demand[ident].known_data, "execution_window_seconds", ident,
                                            {"public_metadata", "observed_event"}, snapshot.now)
                if execution_window < DEFAULT_HORIZON:
                    raise ValueError("Explicit remaining lease and timeout window must cover the 900-second horizon")
                task["remaining_recoveries"] = int(budget)
                tasks.append(task)
            for ident in node_ids:
                physical = by_node[ident].devices[0].physical_gpu_id
                history = sorted((item for item in snapshot.history
                    if item.name == "availability_window_seconds" and item.source_id == physical
                    and item.source in {"history", "observed_event"} and _recent(item, snapshot.now)),
                    key=lambda item: (item.observed_at, item.available_at))
                windows = {item.observed_at: item.value for item in history}
                devices.append({"windows": list(windows.values())[-MAX_SAMPLES:],
                    **{name: _profile(snapshot.metadata, name, physical,
                        {"declared_profile", "public_metadata"}, snapshot.now)
                       for name in ("notice_seconds", "owner_use_seconds")}})
            estimated = budgeted_cost_matrix(tasks, devices)
        except ValueError as error:
            return fallback(str(error))
        edges = {(c.demand_id, c.placement.slices[0].node_id): c for c in legal}
        matrix = tuple(tuple(estimated[i][j] if (d, n) in edges else None
            for j, n in enumerate(node_ids)) for i, d in enumerate(demand_ids))
        assignment = batch_assign(matrix)
        if assignment is None:
            return fallback("no complete legal matching for the B2-admitted cohort")
        total = sum(matrix[i][j] for i, j in enumerate(assignment))
        original = next(c for c in legal if c.candidate_id == base.selected_candidate_id)
        original_column = node_ids.index(original.placement.slices[0].node_id)
        restricted = (tuple(value if j == original_column else None for j, value in enumerate(matrix[0])),
                      *matrix[1:])
        original_match = batch_assign(restricted)
        if original_match is not None:
            original_total = sum(matrix[i][j] for i, j in enumerate(original_match))
            if math.isclose(total, original_total, rel_tol=1e-12, abs_tol=1e-12):
                assignment, total = original_match, original_total
        chosen = edges[demand_ids[0], node_ids[assignment[0]]]
        values = {edges[d, n].candidate_id: matrix[i][j]
                  for i, d in enumerate(demand_ids) for j, n in enumerate(node_ids) if (d, n) in edges}
        candidates = tuple(c.model_copy(update={"scores": (*c.scores, ScoreComponent(
            name="budgeted_expected_restricted_seconds", value=values[c.candidate_id],
            explanation="Empirical finite-budget E[min(T,900)] with conservative one-second discretization; not a future guarantee.",
        ))}) if c.candidate_id in values else c for c in base.candidates)
        return result(f"Opt-in renewal matching for {len(demand_ids)} B2-admitted demands; "
                      f"sum of budgeted expected restricted completion costs {total:.6g} seconds. "
                      "Repeated historical-window model; one legal action, no reservations or production promotion.",
                      selected=chosen.candidate_id, candidates=candidates)
