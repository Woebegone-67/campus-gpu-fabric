"""Analytic renewal cases and unchanged bounded authority/causality guards."""

from __future__ import annotations

import math

import pytest

from campus_fabric.policies.baselines import BaselinePolicy
from campus_fabric.policies.renewal import (
    RenewalReliabilityPolicy,
    budgeted_completion_cost,
    budgeted_completion_estimate,
    budgeted_cost_matrix,
    renewal_completion_cost,
    renewal_cost_matrix,
)
from tests.policy.test_baselines import selected
from tests.policy.test_reliability import datum, example


def state_with_downtime():
    state = example()
    return state.model_copy(update={"metadata": (*state.metadata,
        *(datum("owner_use_seconds", 10., ident) for ident in ("gpu-a", "gpu-z"))),
        "demands": tuple(d.model_copy(update={"known_data": (*d.known_data,
            datum("remaining_recoveries", 2, d.demand_id, source="public_metadata"),
            datum("execution_window_seconds", 900., d.demand_id, source="public_metadata"))})
            for d in state.demands)})


def test_budgeted_estimate_matches_complete_three_attempt_event_tree():
    result = budgeted_completion_estimate(20., 10., (5., 25.), 1., max_recoveries=2)
    # Complete on attempt1/2/3 with probabilities .5/.25/.125; otherwise fail.
    assert result.completion_probability == .875
    assert result.expected_restricted_seconds == .5*20 + .25*36.5 + .125*52.5 + .125*900
    assert budgeted_completion_cost(20., 10., (5., 25.), 1., max_recoveries=0) == 460.


def test_reporting_horizon_censors_each_path_before_averaging():
    result = budgeted_completion_estimate(20., 10., (5., 25.), 1., max_recoveries=2,
                                         horizon_seconds=50.)
    assert result.expected_restricted_seconds == .5*20 + .25*36.5 + .25*50
    assert result.completion_probability == .75


def test_saved_progress_is_usable_only_while_recovery_budget_remains():
    assert budgeted_completion_estimate(12., 1., (5.,), 3., max_recoveries=2) == (39.5, 1.)
    assert budgeted_completion_estimate(12., 1., (5.,), 3., max_recoveries=1) == (900., 0.)
    # Initial-fit probability survives even though a post-interruption restart
    # cannot fit the same10second window. The unlimited expectation is infinite.
    assert budgeted_completion_estimate(10., 10., (5., 10.), 1.) == (455., .5)


def test_restart_cut_by_notice_cannot_finish_startup_during_notice():
    result = budgeted_completion_estimate(20., 1., (1., 25.), 3., restart_seconds=30.)
    # Half complete on the initial interval. After the short initial window,
    # restart is interrupted by every subsequent notice before any new compute.
    assert result == (460., .5)


@pytest.mark.parametrize("budget", [-1, 3, 2.5, True])
def test_budget_outside_supported_contract_is_rejected_not_clamped(budget):
    with pytest.raises(ValueError):
        budgeted_completion_cost(20., 1., (5.,), 3., max_recoveries=budget)


def test_restart_trap_is_capped_without_claiming_impossibility():
    assert renewal_completion_cost(20., 10., (5., 10.), 1.) == 900.
    assert renewal_completion_cost(20., 10., (5., 10.), 1., horizon_seconds=300.) == 300.


def test_restart_geometric_expectation_matches_analytic_equation():
    # Resumed T=20.5+16=36.5. First interval has no restart: .5*20+.5*(16+T).
    assert renewal_completion_cost(20., 10., (5., 25.), 1.) == 36.25
    assert renewal_completion_cost(20., 10., (5., 5., 5., 25.), 1.) == 68.375


def test_checkpoint_progress_avoids_restart_trap_and_includes_owner_downtime():
    # First window saves5; resumed windows save floor(5-.5)=4. After another
    # three intervals, remaining3 completes with.5restart: 4*18+3.5=75.5.
    assert renewal_completion_cost(20., 1., (5.,), 3.) == 75.5
    assert renewal_completion_cost(20., 1., (5.,), 3., owner_use_seconds=20.) == 115.5
    assert renewal_completion_cost(20., 10., (5.,), 3.) == 900.


def test_initial_fit_does_not_imply_fit_after_restart_consumes_window():
    assert renewal_completion_cost(10., 10., (10.,), 1.) == 10.
    # A5second first window can force a restart; thereafter even the10second
    # window cannot fit10seconds of compute plus the.5second startup.
    assert renewal_completion_cost(10., 10., (5., 10.), 1.) == 900.
    assert renewal_completion_cost(10., 10., (5., 11.), 1.) == 26.25


def test_zero_progress_resume_branches_solve_self_loop_without_hanging():
    # Restart consumes an entire1second window; longer windows still permit
    # progress or completion. The short-window branch must recur to itself.
    assert math.isfinite(renewal_completion_cost(12., .1, (1., 5.), 1., restart_seconds=1.))
    assert renewal_completion_cost(12., .1, (1.,), 1., restart_seconds=1.) == 900.


def test_notice_finish_preserves_fallback_margin_and_quantization_is_conservative():
    assert renewal_completion_cost(6., 1., (5.,), 3.) == 6.
    assert renewal_completion_cost(6., 1., (5.,), 2.1) == 5.+2.1+10.+.5+1.
    assert renewal_completion_cost(5.1, 10., (5.9,), 1.) == 900.
    assert renewal_completion_cost(5.1, 10., (6.,), 1.) == 6.


def test_expected_completion_cap_is_applied_only_after_recurrence():
    # Strong downtime pushes the expectation above900; intermediate clipping
    # would bias it below900 by averaging a clipped failure branch with success.
    assert renewal_completion_cost(500., .1, (1., 600.), 1., owner_use_seconds=1000.) == 900.


@pytest.mark.parametrize("duration,windows", [(901., (5.,)), (0., (5.,)),
                                              (20., (0.5,)), (20., ()), (math.nan, (5.,))])
def test_unsupported_compute_or_window_inputs_fail_closed(duration, windows):
    with pytest.raises(ValueError):
        renewal_completion_cost(duration, 1., windows, 3.)


def test_matrix_uses_task_device_interaction_without_outcome_inputs():
    tasks = ({"estimated_compute_seconds": 10., "checkpoint_seconds": .1},
             {"estimated_compute_seconds": 30., "checkpoint_seconds": 10.})
    devices = ({"windows": (100.,), "notice_seconds": 1., "owner_use_seconds": 10.},
               {"windows": (15.,), "notice_seconds": 1., "owner_use_seconds": 10.})
    assert renewal_cost_matrix(tasks, devices) == ((10., 10.), (30., 900.))


def test_policy_retains_b2_admission_and_existing_legal_candidate():
    state = state_with_downtime()
    baseline = BaselinePolicy("B2").plan(state)
    plan = RenewalReliabilityPolicy().plan(state)
    choice = selected(plan)
    assert "inactive" not in plan.rationale
    assert choice.demand_id == selected(baseline).demand_id == "first"
    assert choice.placement.slices[0].node_id == "node-z"
    original = next(c for c in baseline.candidates if c.candidate_id == choice.candidate_id)
    assert choice.placement == original.placement
    assert choice.preconditions == original.preconditions
    assert choice.feasibility == "legal"
    assert choice.scores[-1].name == "budgeted_expected_restricted_seconds"
    assert RenewalReliabilityPolicy().plan(state) == plan


def test_required_downtime_is_sourced_causal_and_never_inferred_from_future():
    state = state_with_downtime()
    baseline = BaselinePolicy("B2").plan(state)
    old = example()
    future = datum("owner_use_seconds", 10., "gpu-z", when=11.)
    malformed = state.model_copy(update={"metadata": (*state.metadata[:-1], future)})
    for changed in (old, malformed, state.model_copy(update={"history": ()})):
        result = RenewalReliabilityPolicy().plan(changed)
        assert "B2 fallback" in result.rationale
        assert result.selected_candidate_id == baseline.selected_candidate_id
    unused = datum("actual_next_window_seconds", 100000., "gpu-z", when=11.)
    decorated = state.model_copy(update={"metadata": (*state.metadata, unused)})
    assert selected(RenewalReliabilityPolicy().plan(decorated)).placement == selected(
        RenewalReliabilityPolicy().plan(state)).placement


def test_out_of_model_compute_profile_falls_back_to_b2():
    state = state_with_downtime()
    first = state.demands[0]
    facts = (datum("estimated_compute_seconds", 901., first.demand_id), *first.known_data[1:])
    changed = state.model_copy(update={"demands": (first.model_copy(update={"known_data": facts}),
                                                   state.demands[1])})
    result = RenewalReliabilityPolicy().plan(changed)
    assert "B2 fallback" in result.rationale
    assert result.selected_candidate_id == BaselinePolicy("B2").plan(changed).selected_candidate_id


def test_missing_untrusted_or_excess_recovery_budget_keeps_b2():
    state = state_with_downtime()
    first = state.demands[0]
    for fact in (None, datum("remaining_recoveries", 2, first.demand_id),
                 datum("remaining_recoveries", 3, first.demand_id, source="public_metadata"),
                 datum("remaining_recoveries", 2, first.demand_id, when=11., source="public_metadata")):
        facts = tuple(x for x in first.known_data if x.name != "remaining_recoveries")
        facts += () if fact is None else (fact,)
        changed = state.model_copy(update={"demands": (first.model_copy(update={"known_data": facts}),
                                                       state.demands[1])})
        result = RenewalReliabilityPolicy().plan(changed)
        assert "B2 fallback" in result.rationale
        assert result.selected_candidate_id == BaselinePolicy("B2").plan(changed).selected_candidate_id


def test_real_execution_window_must_cover_the_reporting_horizon():
    state = state_with_downtime()
    first = state.demands[0]
    facts = tuple(x for x in first.known_data if x.name != "execution_window_seconds")
    for fact in (None, datum("execution_window_seconds", 300., first.demand_id, source="public_metadata")):
        updated = facts + (() if fact is None else (fact,))
        changed = state.model_copy(update={"demands": (first.model_copy(update={"known_data": updated}),
                                                       state.demands[1])})
        result = RenewalReliabilityPolicy().plan(changed)
        assert "B2 fallback" in result.rationale


def test_budgeted_matrix_requires_explicit_budget_instead_of_silently_assuming_two():
    tasks = ({"estimated_compute_seconds": 20., "checkpoint_seconds": 1.},)
    devices = ({"windows": (5.,), "notice_seconds": 3.},)
    with pytest.raises(KeyError):
        budgeted_cost_matrix(tasks, devices)
