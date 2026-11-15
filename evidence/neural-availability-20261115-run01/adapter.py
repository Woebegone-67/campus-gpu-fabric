"""Pinned neural availability distributions driving legal task/GPU assignments.

This opt-in adapter consumes only the fixed FEATURE_NAMES in per-physical-GPU
snapshot.metadata. Values must be recent, finite scalars from history,
observed_event, or public_metadata, with the physical GPU as source_id. Neither
future windows nor actual task runtimes enter prediction. Training cutoff must
precede the planning snapshot. The artifact loader verifies a caller-pinned
SHA256 and schema; predict additionally checks all feature bounds.

The existing renewal guard still requires four completed historical windows,
supported independent whole-GPU nodes, explicit task estimates, remaining
recovery budget0..2, and an authoritative execution window of at least900s.
Because the model predicts newly opened windows, a public/observed age datum
availability_window_age_seconds must be zero and observed at snapshot.now.
Missing/stale/OOD inputs fall back to historical renewal matching or B2. No
model-only cold start or production-default registration is installed here.

Eight predicted window masses become32 deterministic midpoint quantiles for
the frozen finite-budget completion estimator. This approximates their CDF to
1/32; it does not assert calibrated field probabilities. Task duration and
checkpoint cost turn each GPU distribution into a different placement cost.
The bounded cohort assignment changes one existing legal CandidateAction,
preserves B2 admission, and never grants an extension to a contributor deadline.
Actual reclaim, checkpoint, recovery and version checks stay in the kernel.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Sequence

from campus_fabric.contracts.models import (
    MAX_SAFE_INTEGER,
    DecisionPlan,
    DecisionSnapshot,
    ScoreComponent,
    VersionedArtifact,
)
from campus_fabric.policies.availability_model import FEATURE_NAMES, load_predictor
from campus_fabric.policies.baselines import _digest
from campus_fabric.policies.reliability import _profile, _recent, batch_assign
from campus_fabric.policies.renewal import RenewalReliabilityPolicy, budgeted_cost_matrix

QUANTILE_SAMPLES = 32
_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_FEATURE_SOURCES = {"history", "observed_event", "public_metadata"}


def probability_windows(probabilities: Sequence[float], bins: Sequence[float]) -> tuple[float, ...]:
    """Bounded deterministic equal-mass approximation of an eight-bin CDF."""
    if len(probabilities) != 8 or len(bins) != 8:
        raise ValueError("The learned window distribution must contain exactly eight bins")
    if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in probabilities):
        raise ValueError("Window probabilities must be finite and nonnegative")
    total = sum(probabilities)
    if not math.isclose(total, 1., rel_tol=0., abs_tol=1e-9):
        raise ValueError("Window probabilities must sum to one")
    if any(type(x) not in (int, float) or not math.isfinite(x) or not 1 <= x <= 900 for x in bins):
        raise ValueError("Window bin representatives must be between one and900seconds")
    if any(left >= right for left, right in zip(bins, bins[1:])):
        raise ValueError("Window bin representatives must be strictly increasing")
    samples = []
    for index in range(QUANTILE_SAMPLES):
        quantile = (index + .5) / QUANTILE_SAMPLES
        cumulative = 0.
        for probability, window in zip(probabilities, bins, strict=True):
            cumulative += probability / total
            if cumulative >= quantile:
                samples.append(float(window))
                break
        else:
            raise ValueError("Invalid learned cumulative distribution")
    return tuple(samples)


def predicted_cost_matrix(tasks: Sequence[dict], devices: Sequence[dict], predictor):
    """Observed-only helper for the same predictor-to-assignment path.

    Each device supplies features, notice_seconds and owner_use_seconds; tasks
    follow budgeted_cost_matrix's explicit profile/budget schema. Artifact
    verification and snapshot provenance belong to LearnedAvailabilityPolicy.
    """
    distributions = tuple({
        "windows": probability_windows(predictor.predict(device["features"]), predictor.window_bins_seconds),
        "notice_seconds": device["notice_seconds"],
        "owner_use_seconds": device["owner_use_seconds"],
    } for device in devices)
    return budgeted_cost_matrix(tasks, distributions)


def task_gpu_rankings(tasks: Sequence[dict], devices: Sequence[dict], predictor) -> tuple[dict, ...]:
    """Explicit per-task GPU ranks; never a universal GPU reliability league.

    Inputs are the same observed profiles as predicted_cost_matrix plus stable
    task_id/device_id. Ties share a rank and are ordered by device ID. The batch
    assignment can choose among equal or competing per-task options to preserve
    the admitted cohort; this table itself neither allocates nor reserves.
    """
    task_ids = tuple(task["task_id"] for task in tasks)
    device_ids = tuple(device["device_id"] for device in devices)
    if (any(not isinstance(ident, str) or not ident for ident in (*task_ids, *device_ids))
            or len(set(task_ids)) != len(task_ids) or len(set(device_ids)) != len(device_ids)):
        raise ValueError("Rankings require distinct nonempty task and device IDs")
    matrix = predicted_cost_matrix(tasks, devices, predictor)
    records = []
    for task_id, row in zip(task_ids, matrix, strict=True):
        previous, rank = None, 0
        for position, column in enumerate(sorted(range(len(device_ids)), key=lambda col: (row[col], device_ids[col]))):
            cost = row[column]
            if previous is None or not math.isclose(previous, cost, rel_tol=1e-12, abs_tol=1e-12):
                rank = position + 1
            records.append({"task_id": task_id, "device_id": device_ids[column], "rank": rank,
                            "predicted_restricted_seconds": cost, "model_sha256": predictor.sha256,
                            "scope": "task-conditioned; newly opened window; not a contributor promise"})
            previous = cost
    return tuple(records)


def _feature(items, name, physical_gpu_id, now):
    facts = [item for item in items if item.name == name and item.source_id == physical_gpu_id
             and item.source in _FEATURE_SOURCES and _recent(item, now)]
    if not facts:
        raise ValueError("Missing, untrusted or stale learned feature: " + name)
    latest_time = max(item.observed_at for item in facts)
    latest = [item.value for item in facts if item.observed_at == latest_time]
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or abs(value) > MAX_SAFE_INTEGER for value in latest):
        raise ValueError("Learned features must be finite numeric scalars")
    if len({float(value) for value in latest}) != 1:
        raise ValueError("Conflicting simultaneous learned feature: " + name)
    return float(latest[0])


class LearnedAvailabilityPolicy:
    """Executable bounded assignment from an explicitly pinned neural artifact."""

    def __init__(self, artifact_path, *, expected_sha256: str, max_candidates=512):
        self.history = RenewalReliabilityPolicy(max_candidates=max_candidates)
        self.predictor = None
        self.load_error = None
        self.expected_sha256 = expected_sha256
        try:
            self.predictor = load_predictor(artifact_path, expected_sha256=expected_sha256)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.load_error = str(error)

    def plan(self, snapshot: DecisionSnapshot) -> DecisionPlan:
        historical = self.history.plan(snapshot)
        model = self.predictor
        fingerprint = _digest({"source": _SOURCE_SHA, "history": historical.policy.sha256,
                               "model": model.sha256 if model else self.expected_sha256,
                               "quantile_samples": QUANTILE_SAMPLES})

        def result(reason, *, chosen=None, candidates=None):
            active = chosen is not None
            return historical.model_copy(update={
                "decision_id": "decision-" + _digest([fingerprint, snapshot.model_dump(mode="json")])[:32],
                "policy": VersionedArtifact(name="P-learned-availability", version="0.1.0", sha256=fingerprint),
                "model": (VersionedArtifact(name="neural-availability-window", version=model.model_version,
                                             sha256=model.sha256) if active else historical.model),
                "selected_candidate_id": chosen if active else historical.selected_candidate_id,
                "candidates": candidates if candidates is not None else historical.candidates,
                "rationale": reason,
                "tie_break_rule": ("B2 admission; learned task-conditioned bounded completion matching; "
                                   "equal totals preserve B2 first choice, then node/device IDs; kernel revalidates."),
            })

        def fallback(reason):
            return result("Learned matching inactive; history/B2 fallback: " + reason + "; " + historical.rationale)

        if historical.rationale.startswith("Renewal matching inactive;"):
            return fallback("historical safety/data guard rejected the snapshot")
        if model is None:
            return fallback("pinned model unavailable: " + (self.load_error or "unknown artifact error"))
        if (type(model.trained_through) not in (int, float) or not math.isfinite(model.trained_through)
                or not 0 <= model.trained_through <= snapshot.now):
            return fallback("model training cutoff is later than the planning snapshot")
        if tuple(model.feature_names) != FEATURE_NAMES:
            return fallback("model feature contract does not match the fixed observed-only schema")
        base = self.history.guard.baseline.plan(snapshot)
        legal = [candidate for candidate in base.candidates if candidate.feasibility == "legal"]
        node_ids = sorted({candidate.placement.slices[0].node_id for candidate in legal})
        demand_ids = list(dict.fromkeys(candidate.demand_id for candidate in legal))[:len(node_ids)]
        by_demand = {demand.demand_id: demand for demand in snapshot.demands}
        by_node = {node.node_id: node for node in snapshot.inventory}
        try:
            tasks = []
            for ident in demand_ids:
                task = {name: _profile(by_demand[ident].known_data, name, ident,
                        {"declared_profile", "history"}, snapshot.now, default=default, positive=positive)
                        for name, default, positive in (("estimated_compute_seconds", None, True),
                            ("checkpoint_seconds", None, False), ("restart_seconds", .5, False),
                            ("stop_seconds", .1, False))}
                task["remaining_recoveries"] = int(_profile(by_demand[ident].known_data,
                    "remaining_recoveries", ident, {"public_metadata", "observed_event"}, snapshot.now))
                tasks.append(task)
            devices = []
            for ident in node_ids:
                physical = by_node[ident].devices[0].physical_gpu_id
                age = _profile(snapshot.metadata, "availability_window_age_seconds", physical,
                               {"public_metadata", "observed_event"}, snapshot.now)
                if age != 0 or not any(item.name == "availability_window_age_seconds"
                        and item.source_id == physical and item.source in {"public_metadata", "observed_event"}
                        and item.observed_at == snapshot.now and _recent(item, snapshot.now)
                        for item in snapshot.metadata):
                    raise ValueError("Neural model requires an authoritative newly opened window at snapshot time")
                devices.append({"features": {name: _feature(snapshot.metadata, name, physical, snapshot.now)
                                             for name in FEATURE_NAMES},
                                **{name: _profile(snapshot.metadata, name, physical,
                                    {"declared_profile", "public_metadata"}, snapshot.now)
                                   for name in ("notice_seconds", "owner_use_seconds")}})
            estimated = predicted_cost_matrix(tasks, devices, model)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            return fallback(str(error))
        edges = {(candidate.demand_id, candidate.placement.slices[0].node_id): candidate for candidate in legal}
        matrix = tuple(tuple(estimated[i][j] if (demand, node) in edges else None
            for j, node in enumerate(node_ids)) for i, demand in enumerate(demand_ids))
        assignment = batch_assign(matrix)
        if assignment is None:
            return fallback("no complete legal matching for the B2-admitted cohort")
        total = sum(matrix[i][j] for i, j in enumerate(assignment))
        original = next(candidate for candidate in legal if candidate.candidate_id == base.selected_candidate_id)
        original_column = node_ids.index(original.placement.slices[0].node_id)
        restricted = (tuple(value if j == original_column else None for j, value in enumerate(matrix[0])),
                      *matrix[1:])
        original_match = batch_assign(restricted)
        if original_match is not None:
            original_total = sum(matrix[i][j] for i, j in enumerate(original_match))
            if math.isclose(total, original_total, rel_tol=1e-12, abs_tol=1e-12):
                assignment, total = original_match, original_total
        chosen = edges[demand_ids[0], node_ids[assignment[0]]]
        values = {edges[demand, node].candidate_id: matrix[i][j]
                  for i, demand in enumerate(demand_ids) for j, node in enumerate(node_ids)
                  if (demand, node) in edges}
        candidates = tuple(candidate.model_copy(update={"scores": (*candidate.scores, ScoreComponent(
            name="learned_budgeted_expected_restricted_seconds", value=values[candidate.candidate_id],
            explanation="Neural window distribution plus task/checkpoint/budget; discretized expected restricted completion, not a guarantee.",
        ))}) if candidate.candidate_id in values else candidate for candidate in base.candidates)
        return result(f"Pinned neural availability model selects legal GPU assignment for {len(demand_ids)} "
                      f"B2-admitted demands; task-conditioned cohort cost {total:.6g} seconds. "
                      "One legal action; contributor deadlines and downstream recovery checks remain authoritative.",
                      chosen=chosen.candidate_id, candidates=candidates)
