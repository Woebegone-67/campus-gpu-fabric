from dataclasses import replace

import pytest

from campus_fabric.policies.reclaim import ReclaimInputs, plan_reclaim


def inputs(**changes):
    base = ReclaimInputs(now=10, checkpoint_due=18, owner_deadline=20,
        progress_steps=5000, total_steps=10000, rate_lower=100, rate_upper=120,
        checkpoint_cost_upper=.4, stop_cost_upper=1.5, restart_cost_upper=.5,
        observed_at=10, trusted=True)
    return replace(base, **changes)


@pytest.mark.parametrize(("changes", "action"), [
    ({"progress_steps": 9900}, "finish_first"),
    ({"owner_wait_weight": .25}, "save_at"),
    ({}, "save_now"),
    ({"progress_steps": 0, "checkpoint_cost_upper": 4}, "stop"),
    ({"progress_steps": 0, "checkpoint_cost_upper": 4, "allow_cold_restart": False}, "save_now"),
    ({"observed_at": 8}, "save_now"),
])
def test_frozen_cost_objective_selects_feasible_candidate(changes, action):
    plan = plan_reclaim(inputs(**changes))
    assert plan["action"] == action
    assert plan["inputs"] == inputs(**changes).__dict__
    assert plan["stop_at"] <= min(18, 20 - 1.5)
    if action in {"finish_first", "save_at"}:
        assert plan["dispatch_at"] + changes.get("checkpoint_cost_upper", .4) < plan["stop_at"]


def test_unavoidable_short_notice_reports_infeasibility_instead_of_pretending_safe():
    plan = plan_reclaim(inputs(owner_deadline=10.5, allow_cold_restart=False))
    assert plan["action"] == "stop" and plan["dispatch_at"] == 10
    assert not plan["deadline_feasible"] and not plan["completion_feasible"]


def test_equal_weight_prefers_earliest_return_despite_float_rounding():
    plan = plan_reclaim(inputs(checkpoint_due=17.813517))
    assert plan["action"] == "save_now"
