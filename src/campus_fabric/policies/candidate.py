"""P: current observed placement opportunity loss, with B1's admission order.

The opportunity component only examines legal candidates already enumerated by
B2 under the same budget. It measures lost recorded alternatives, not predicted
future work or a proof of feasibility. Recovery costs are intentionally inactive:
the current DecisionSnapshot does not contain a verified recovery cost model.
A validates resumes and budgets before putting their leases back into Pending.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from campus_fabric.contracts.models import (
    DecisionPlan,
    DecisionSnapshot,
    ScoreComponent,
    VersionedArtifact,
)
from campus_fabric.policies.baselines import BaselinePolicy, _digest, _shape

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _can_coexist(left, right, snapshot):
    """Resource compatibility of two recorded single-node legal placements."""
    a, b = left.placement.slices[0], right.placement.slices[0]
    if a.node_id != b.node_id:
        return True
    node = next(n for n in snapshot.inventory if n.node_id == a.node_id)
    for field in ("cpu_millicores", "memory_mib", "ephemeral_storage_mib", "gpu_memory_mib"):
        if getattr(a.resources, field) + getattr(b.resources, field) > getattr(node.available, field):
            return False
    devices = {d.device_id: d for d in node.devices}
    whole_count = sum(devices[i].profile is None for i in (*a.device_ids, *b.device_ids))
    if whole_count > node.available.gpu_count:
        return False
    for aid in a.device_ids:
        for bid in b.device_ids:
            aa, bb = devices[aid], devices[bid]
            if aid == bid or (aa.physical_gpu_id == bb.physical_gpu_id
                             and (aa.profile is None or bb.profile is None)):
                return False
    return True


class CandidatePolicy:
    def __init__(self, *, max_candidates: int = 512, opportunity_enabled: bool = True):
        self.baseline = BaselinePolicy("B2", max_candidates=max_candidates)
        self.name = "P"
        self.max_candidates = max_candidates
        self.opportunity_enabled = opportunity_enabled

    def plan(self, snapshot: DecisionSnapshot) -> DecisionPlan:
        baseline = self.baseline.plan(snapshot)
        legal = [c for c in baseline.candidates if c.feasibility == "legal"]
        groups = defaultdict(dict)
        demands = {d.demand_id: d for d in snapshot.demands}
        for c in legal:
            groups[c.owner_id].setdefault(_shape(demands[c.demand_id]), c.demand_id)
        alternatives = defaultdict(list)
        for c in legal:
            alternatives[c.demand_id].append(c)
        scores = {}
        for c in legal:
            loss = 0.0
            for owner, shapes in groups.items():
                if owner == c.owner_id:
                    continue
                for demand_id in shapes.values():
                    options = alternatives[demand_id]
                    loss += sum(not _can_coexist(c, o, snapshot) for o in options) / len(options) / len(shapes)
            scores[c.candidate_id] = loss
        candidates = tuple(c.model_copy(update={"scores": (*c.scores, ScoreComponent(
            name="observed_opportunity_loss", value=scores.get(c.candidate_id, 0.0),
            explanation="Fraction of recorded legal placement alternatives consumed, owner/shape balanced; no future prediction.",
        ))}) for c in baseline.candidates)
        selected = next((c for c in candidates if c.candidate_id == baseline.selected_candidate_id), None)
        if selected is not None and self.opportunity_enabled:
            admitted = [c for c in candidates if c.demand_id == selected.demand_id
                        and c.feasibility == "legal"]
            def key(c):
                s = {score.name: score.value for score in c.scores}
                return (s["observed_opportunity_loss"], s["compatibility_pressure"],
                        s["remaining_gpu_count"], s["excess_gpu_memory_mib"],
                        s["remaining_cpu_millicores"], s["remaining_memory_mib"],
                        c.placement.slices[0].node_id, c.placement.slices[0].device_ids)
            selected = min(admitted, key=key)
        fingerprint = _digest({"source": _LOADED_SOURCE_SHA256, "baseline": baseline.policy.sha256,
                               "opportunity_enabled": self.opportunity_enabled})
        return baseline.model_copy(update={
            "decision_id": f"decision-{_digest([fingerprint, snapshot.model_dump(mode='json')])[:32]}",
            "policy": VersionedArtifact(name="P", version="0.2.0", sha256=fingerprint),
            "candidates": candidates,
            "selected_candidate_id": selected.candidate_id if selected else None,
            "rationale": "P uses B1 admission and bounded observed opportunity loss before B2 tie keys. "
                         f"Opportunity component enabled={self.opportunity_enabled}; recovery-cost component inactive. "
                         "Kernel revalidates all placements and recovery eligibility.",
            "tie_break_rule": "B1 owner-share/priority/arrival admission; observed opportunity loss; "
                              "B2 compatibility pressure and best-fit keys; node/device IDs.",
        })
