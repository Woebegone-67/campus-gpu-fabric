"""Owner-scoped explanations of committed policy decisions.

Stored plans are immutable execution evidence. This read projection is deliberately
partial: a policy may inspect competing work even when its candidate budget leaves
only one owner's candidates in the plan. Free text and scores therefore cannot be
treated as owner-private merely because a candidate carries that owner's label.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from campus_fabric.contracts import models as m


def owner_decision_view(
    plan: m.DecisionPlan,
    owner_id: str | None,
    allocations: Mapping[str, Mapping[str, Any]],
) -> m.DecisionPlan | None:
    """Return only the caller's recorded candidates, never global ranking inputs.

    Ownership comes from the authoritative allocation, not an unselected policy
    candidate's owner label. Admin callers bypass this helper and retain the original
    plan. The returned DTO is a read-only explanation, not a new executable plan.
    """
    if owner_id is None:
        return None
    candidates = []
    for candidate in plan.candidates:
        allocation = allocations.get(candidate.allocation_id)
        if (
            candidate.owner_id != owner_id
            or allocation is None
            or allocation["owner_id"] != owner_id
            or candidate.demand_id != allocation["allocation_id"]
            or candidate.intent_id not in (None, allocation["intent_id"])
        ):
            continue
        candidates.append(candidate.model_copy(update={
            # Structured candidate identity, action, placement and feasibility
            # remain from the original decision. Context-sensitive prose, refs,
            # preconditions and scores may describe other tenants and are omitted.
            "attempt": None,
            "preconditions": (),
            "scores": (),
            "violations": tuple(m.ConstraintViolation(
                constraint=violation.constraint,
                detail=f"Recorded candidate violates {violation.constraint}; private details omitted.",
            ) for violation in candidate.violations),
        }))
    if not candidates:
        return None
    ids = {candidate.candidate_id for candidate in candidates}
    return m.DecisionPlan(
        decision_id=plan.decision_id,
        snapshot_version=plan.snapshot_version,
        decision_at=plan.decision_at,
        policy=plan.policy.model_copy(update={"uri": None}),
        model=plan.model.model_copy(update={"uri": None}),
        candidates=tuple(candidates),
        coverage=m.SearchCoverage(
            scope="partial",
            domain_description="Recorded candidates for your own authorized demands only.",
            examined_count=sum(c.feasibility != "unexamined" for c in candidates),
            domain_size=None,
            truncation_reason=(
                "Owner-scoped explanation: other demands, global comparison inputs, "
                "scores and private free text are not disclosed. This view cannot "
                "establish complete search coverage or global ranking."
            ),
        ),
        selected_candidate_id=(
            plan.selected_candidate_id if plan.selected_candidate_id in ids else None
        ),
        rationale=(
            "The recorded selection belongs to your demand; inspect authorized "
            "committed events for its actual outcome."
            if plan.selected_candidate_id in ids else
            "No selection for your demand is disclosed in this owner-scoped view; "
            "this does not imply your demand is infeasible."
        ),
        tie_break_rule="Global tie-break comparison details are outside this owner-scoped view.",
        asserts_pool_infeasible=False,
    )
