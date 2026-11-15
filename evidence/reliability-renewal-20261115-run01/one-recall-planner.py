"""Opt-in, bounded reliability/cost matching; production defaults are unchanged.

Wire contract (all times in logical seconds, available_at <= snapshot.now):
* Demand.known_data: estimated_compute_seconds, checkpoint_seconds, optionally
  restart_seconds (default .5), stop_seconds (default .1). source must be
  declared_profile/history and source_id must equal the demand_id. TTL and job
  timeout are never read as compute estimates.
* Snapshot.history: availability_window_seconds, source history/observed_event,
  source_id = physical GPU ID. These are completed past availability windows,
  NOT the current window's hidden end. At least four distinct observations in
  the last 86400 logical seconds are needed; at most the latest 32 are used.
* Snapshot.metadata: notice_seconds, source declared_profile/public_metadata,
  source_id = physical GPU ID. This is a declared notice profile, not knowledge
  of the next recall. Profiles must also be at most 86400 seconds old.

The edge model estimates one interruption's wasted compute plus save/restart
overhead, using a common finish-or-save responder. It does not claim total JCT,
owner welfare, calibrated survival probabilities, or multi-recall optimality.
Completed-window sampling can be biased under censoring/distribution shift.

Only independent nodes with one whole GPU are supported. Up to six B2-admitted
demands are matched injectively to at most six currently legal nodes. The policy
returns ONE existing legal candidate for B2's first admitted demand. It reserves
nothing; after each commit the remaining assignment is recomputed. Missing
profiles, incomplete candidate coverage, unsupported shapes, or no complete
matching all fall back to B2. A still validates every committed placement.
"""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Sequence

from campus_fabric.contracts.models import (
    MAX_SAFE_INTEGER,
    DecisionPlan,
    DecisionSnapshot,
    ScoreComponent,
    VersionedArtifact,
)
from campus_fabric.policies.baselines import BaselinePolicy, _digest

MAX_BATCH = 6
MAX_AGE = 86400.0
MIN_SAMPLES = 4
MAX_SAMPLES = 32
REPORT_MARGIN = 0.1
_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _number(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Costs and durations must be finite numbers, not booleans")
    if value < 0 or (positive and value == 0):
        raise ValueError("Costs must be nonnegative and durations positive")
    return float(value)


def expected_interruption_cost(compute_seconds: float, checkpoint_seconds: float,
                               windows: Sequence[float], notice_seconds: float, *,
                               restart_seconds: float = .5, stop_seconds: float = .1) -> float:
    """Empirical expected interruption overhead in compute seconds, not a score.

    Windows are only historical observations. A notice permits finishing only
    before the conservative checkpoint fallback boundary; saving costs actual
    compute time and an interrupted task must restart. No interruption costs
    zero. When saving cannot fit, the elapsed attempt is discarded.
    """
    duration = _number(compute_seconds, positive=True)
    checkpoint = _number(checkpoint_seconds)
    notice = _number(notice_seconds)
    restart = _number(restart_seconds)
    stop = _number(stop_seconds)
    if not windows:
        raise ValueError("At least one historical window is required")
    observations = tuple(_number(w, positive=True) for w in windows)
    fallback_time = max(0., notice - checkpoint - stop - REPORT_MARGIN)
    costs = []
    for window in observations:
        if duration <= window or duration - window <= fallback_time:
            costs.append(0.)
        elif checkpoint + stop + REPORT_MARGIN < notice:
            costs.append(checkpoint + restart)
        else:
            costs.append(min(window, duration) + restart)
    return mean(costs)


def cost_matrix(tasks: Sequence[dict], devices: Sequence[dict]) -> tuple[tuple[float, ...], ...]:
    """Pure helper shared with the independent paired runner.

    Task keys match known_data names above; device keys are windows (sequence)
    and notice_seconds. The helper has no access to actual/future outcomes.
    """
    return tuple(tuple(expected_interruption_cost(
        task["estimated_compute_seconds"], task["checkpoint_seconds"],
        device["windows"], device["notice_seconds"],
        restart_seconds=task.get("restart_seconds", .5),
        stop_seconds=task.get("stop_seconds", .1),
    ) for device in devices) for task in tasks)


def batch_assign(matrix: Sequence[Sequence[float | None]]) -> tuple[int, ...] | None:
    """Exact bounded injective assignment; None edges are physically illegal.

    All admitted rows must be matched. Equal totals use column-index order,
    making tie behavior deterministic. There is no task skipping or reordering.
    """
    rows = tuple(tuple(row) for row in matrix)
    if not rows:
        return ()
    columns = len(rows[0])
    if not 1 <= columns <= MAX_BATCH or not 1 <= len(rows) <= MAX_BATCH:
        raise ValueError("Assignment is bounded to six tasks and devices")
    if any(len(row) != columns for row in rows):
        raise ValueError("Ragged cost matrix")
    for row in rows:
        for value in row:
            if value is not None:
                _number(value)
    if len(rows) > columns:
        return None

    @lru_cache(None)
    def solve(index, used):
        if index == len(rows):
            return 0., ()
        options = []
        for col, value in enumerate(rows[index]):
            if value is None or used & (1 << col):
                continue
            suffix = solve(index + 1, used | (1 << col))
            if suffix is not None:
                options.append((value + suffix[0], (col,) + suffix[1]))
        return min(options, key=lambda item: (item[0], item[1])) if options else None

    result = solve(0, 0)
    return None if result is None else result[1]


def _recent(item, now):
    # Check again even if a caller bypassed Pydantic with model_copy(update=...).
    return 0 <= item.observed_at <= item.available_at <= now and now - item.observed_at <= MAX_AGE


def _profile(items, name, source_id, sources, now, *, default=None, positive=False):
    found = [item for item in items if item.name == name and item.source_id == source_id
             and item.source in sources and _recent(item, now)]
    if not found:
        if default is None:
            raise ValueError("Missing or stale profile: " + name)
        return default
    latest_time = max(item.observed_at for item in found)
    latest = [item for item in found if item.observed_at == latest_time]
    if len({(type(item.value), item.value) for item in latest}) != 1:
        raise ValueError("Ambiguous simultaneous profile: " + name)
    return _number(latest[0].value, positive=positive)


class ReliabilityPolicy:
    """Explicitly instantiated research extension, not registered as default P."""

    def __init__(self, *, max_candidates=512):
        self.baseline = BaselinePolicy("B2", max_candidates=max_candidates)
        self.max_candidates = max_candidates

    def plan(self, snapshot: DecisionSnapshot) -> DecisionPlan:
        base = self.baseline.plan(snapshot)
        fingerprint = _digest({"source": _SOURCE_SHA, "baseline": base.policy.sha256,
                               "min_samples": MIN_SAMPLES, "max_age": MAX_AGE,
                               "max_batch": MAX_BATCH, "max_samples": MAX_SAMPLES})

        def result(reason, *, selected=None, candidates=None):
            return base.model_copy(update={
                "decision_id": "decision-" + _digest([fingerprint, snapshot.model_dump(mode="json")])[:32],
                "policy": VersionedArtifact(name="R", version="0.1.0", sha256=fingerprint),
                "model": VersionedArtifact(name="past-window-interruption-cost", version="0.1.0", sha256=_SOURCE_SHA),
                "candidates": candidates if candidates is not None else base.candidates,
                "selected_candidate_id": selected if selected is not None else base.selected_candidate_id,
                "rationale": reason,
                "tie_break_rule": "B2 admission; exact bounded cohort interruption-cost matching; "
                                  "equal cost preserves B2 first choice; then node/device IDs. Kernel revalidates.",
            })

        def fallback(reason):
            return result("Reliability matching inactive; B2 fallback: " + reason)

        if base.selected_candidate_id is None:
            return fallback("no selected legal candidate")
        if len(base.candidates) >= self.max_candidates:
            return fallback("candidate coverage may be truncated")
        if any(d.request.gpu.count != 1 or d.request.gpu.profile is not None for d in snapshot.demands):
            return fallback("only one whole GPU per demand is supported")
        if any(len(n.devices) != 1 or n.devices[0].profile is not None
               or n.capacity.gpu_count != 1 for n in snapshot.inventory):
            return fallback("only independent single whole-GPU nodes are supported")
        physical_ids = [n.devices[0].physical_gpu_id for n in snapshot.inventory]
        if len(set(physical_ids)) != len(physical_ids):
            return fallback("physical GPU is shared by multiple node records")
        legal = [c for c in base.candidates if c.feasibility == "legal"]
        node_ids = sorted({c.placement.slices[0].node_id for c in legal})
        if len(node_ids) > MAX_BATCH:
            return fallback("more than six currently legal nodes")
        # B2 enumerates candidates in its admitted demand order, including owner share.
        demand_ids = list(dict.fromkeys(c.demand_id for c in legal))[:len(node_ids)]
        by_demand = {d.demand_id: d for d in snapshot.demands}
        by_node = {n.node_id: n for n in snapshot.inventory}
        tasks, devices = [], []
        try:
            for ident in demand_ids:
                d = by_demand[ident]
                tasks.append({name: _profile(d.known_data, name, ident, {"declared_profile", "history"},
                                             snapshot.now, default=default, positive=positive)
                              for name, default, positive in (
                                  ("estimated_compute_seconds", None, True),
                                  ("checkpoint_seconds", None, False),
                                  ("restart_seconds", .5, False), ("stop_seconds", .1, False))})
            for node_id in node_ids:
                device_id = by_node[node_id].devices[0].physical_gpu_id
                samples = [item for item in snapshot.history
                           if item.name == "availability_window_seconds" and item.source_id == device_id
                           and item.source in {"history", "observed_event"} and _recent(item, snapshot.now)]
                # Repeated copies of one observation cannot manufacture confidence.
                windows = {}
                for item in sorted(samples, key=lambda x: (x.observed_at, x.available_at)):
                    value = _number(item.value, positive=True)
                    if item.observed_at in windows and windows[item.observed_at] != value:
                        raise ValueError("Conflicting availability-window observations")
                    windows[item.observed_at] = value
                if len(windows) < MIN_SAMPLES:
                    raise ValueError("Fewer than four recent independent window observations")
                devices.append({"windows": list(windows.values())[-MAX_SAMPLES:],
                                "notice_seconds": _profile(snapshot.metadata, "notice_seconds", device_id,
                                    {"declared_profile", "public_metadata"}, snapshot.now)})
            estimated = cost_matrix(tasks, devices)
            if any(value > MAX_SAFE_INTEGER for row in estimated for value in row):
                raise ValueError("Estimated interruption cost exceeds the score contract")
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
        restricted = ((tuple(value if j == original_column else None for j, value in enumerate(matrix[0]))), *matrix[1:])
        baseline_assignment = batch_assign(restricted)
        if baseline_assignment is not None:
            baseline_total = sum(matrix[i][j] for i, j in enumerate(baseline_assignment))
            if math.isclose(total, baseline_total, rel_tol=1e-12, abs_tol=1e-12):
                assignment = baseline_assignment
                total = baseline_total
        chosen = edges[demand_ids[0], node_ids[assignment[0]]]
        edge_values = {edges[d, n].candidate_id: matrix[i][j]
                       for i, d in enumerate(demand_ids) for j, n in enumerate(node_ids)
                       if (d, n) in edges}
        scored = tuple(c.model_copy(update={"scores": (*c.scores, ScoreComponent(
            name="expected_interrupt_compute_seconds", value=edge_values[c.candidate_id],
            explanation="Mean single-interruption overhead under completed past windows and declared profiles; not future guarantee.",
        ))}) if c.candidate_id in edge_values else c for c in base.candidates)
        return result(f"Opt-in historical-window matching across {len(demand_ids)} B2-admitted demands; "
                      f"estimated cohort interruption overhead {total:.6g} compute seconds. "
                      "Only the first demand is proposed; no reservations, admission reordering, or production promotion.",
                      selected=chosen.candidate_id, candidates=scored)
