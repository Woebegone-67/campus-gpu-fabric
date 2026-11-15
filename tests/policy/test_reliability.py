"""Causal data boundary, assignment counterexamples and legal B2 fallback."""

from __future__ import annotations

import itertools
import math

import pytest
from pydantic import ValidationError

from campus_fabric.contracts import models as m
from campus_fabric.policies.baselines import BaselinePolicy
from campus_fabric.policies.reliability import (
    MAX_AGE,
    ReliabilityPolicy,
    batch_assign,
    cost_matrix,
    expected_interruption_cost,
)
from tests.policy.test_baselines import demand, device, node, selected, snapshot


def datum(name, value, source_id, when=9., source="declared_profile"):
    return m.ObservedDatum(name=name, value=value, source=source, source_id=source_id,
                           observed_at=when, available_at=when)


def profiled_demand(name, duration, checkpoint, **changes):
    return demand(name, known_data=(datum("estimated_compute_seconds", duration, name),
                                   datum("checkpoint_seconds", checkpoint, name)), **changes)


def example(*, windows=(100., 15.)):
    return snapshot(
        demands=(profiled_demand("first", 10., .1),
                 profiled_demand("second", 30., 10., arrived=1.)),
        inventory=(node("node-a", (device("gpu-a"),)),
                   node("node-z", (device("gpu-z"),))),
        history=tuple(datum("availability_window_seconds", window, ident, when, "history")
                      for ident, window in zip(("gpu-a", "gpu-z"), windows, strict=True)
                      for when in (6., 7., 8., 9.)),
        metadata=tuple(datum("notice_seconds", 1., ident) for ident in ("gpu-a", "gpu-z")),
    )


def assert_fallback(state, **kwargs):
    base = BaselinePolicy("B2", **kwargs).plan(state)
    plan = ReliabilityPolicy(**kwargs).plan(state)
    assert "B2 fallback" in plan.rationale
    assert plan.selected_candidate_id == base.selected_candidate_id
    assert plan.candidates == base.candidates
    return plan


def test_batch_recovers_opportunity_missed_by_first_row_greedy():
    matrix = ((1., 2.), (1., 100.))
    assert batch_assign(matrix) == (1, 0)
    assert sum(matrix[i][j] for i, j in enumerate(batch_assign(matrix))) == 3


def test_assignment_matches_exhaustive_legal_minimum_and_ties():
    matrix = ((0., None, 2., 2.), (2., 0., None, 1.), (0., 1., 5., None))
    candidates = [(sum(matrix[i][j] for i, j in enumerate(p)), p)
                  for p in itertools.permutations(range(4), 3)
                  if all(matrix[i][j] is not None for i, j in enumerate(p))]
    assert batch_assign(matrix) == min(candidates)[1]
    assert batch_assign(((0., 0.), (0., 0.))) == (0, 1)
    assert batch_assign(((1., None), (1., None))) is None
    assert batch_assign(((1.,), (1.,))) is None
    assert batch_assign(()) == ()


@pytest.mark.parametrize("matrix", [((math.nan,),), ((True,),), ((-1.,),),
                                    ((1.,), (1., 2.)), (tuple(range(7)),)])
def test_assignment_rejects_invalid_costs_or_unbounded_problem(matrix):
    with pytest.raises(ValueError):
        batch_assign(matrix)


def test_edge_estimate_distinguishes_finish_save_and_lost_work():
    assert expected_interruption_cost(10., 3., (10.,), 1.) == 0
    assert expected_interruption_cost(10., 1., (9.,), 3.) == 0
    assert expected_interruption_cost(30., 2., (10.,), 4.) == 2.5
    assert expected_interruption_cost(30., 2., (10.,), 1.) == 10.5
    assert expected_interruption_cost(30., 2., (10., 40.), 1.) == 5.25
    # Exactly at the safety limit is not advertised as a successful save.
    assert expected_interruption_cost(30., 2., (10.,), 2.2) == 10.5


def test_window_duration_interaction_cannot_be_replaced_by_single_device_rank():
    costs = cost_matrix(
        ({"estimated_compute_seconds": 10., "checkpoint_seconds": .1},
         {"estimated_compute_seconds": 30., "checkpoint_seconds": 10.}),
        ({"windows": (100.,), "notice_seconds": 1.},
         {"windows": (15.,), "notice_seconds": 1.}),
    )
    assert costs == ((0., 0.), (0., 15.5))
    assert batch_assign(costs) == (1, 0)


def test_matching_changes_placement_without_changing_admission_or_legality():
    state = example()
    before = state.model_dump_json()
    baseline = BaselinePolicy("B2").plan(state)
    policy = ReliabilityPolicy()
    plan = policy.plan(state)
    assert "inactive" not in plan.rationale
    assert selected(plan).demand_id == selected(baseline).demand_id == "first"
    assert selected(baseline).placement.slices[0].node_id == "node-a"
    assert selected(plan).placement.slices[0].node_id == "node-z"
    original = next(c for c in baseline.candidates if c.candidate_id == plan.selected_candidate_id)
    assert selected(plan).placement == original.placement
    assert selected(plan).preconditions == original.preconditions
    assert selected(plan).feasibility == "legal"
    assert policy.plan(state) == plan
    assert state.model_dump_json() == before
    assert not plan.asserts_pool_infeasible


def test_equal_cost_keeps_b2_choice_and_input_order_does_not_change_choice():
    homogeneous = example(windows=(100., 100.))
    assert selected(ReliabilityPolicy().plan(homogeneous)).candidate_id == selected(
        BaselinePolicy("B2").plan(homogeneous)).candidate_id
    state = example()
    reversed_state = state.model_copy(update={"inventory": tuple(reversed(state.inventory)),
                                              "demands": tuple(reversed(state.demands))})
    a, b = selected(ReliabilityPolicy().plan(state)), selected(ReliabilityPolicy().plan(reversed_state))
    assert (a.demand_id, a.placement) == (b.demand_id, b.placement)


def test_missing_insufficient_duplicate_and_stale_history_fall_back():
    state = example()
    assert_fallback(state.model_copy(update={"history": ()}))
    assert_fallback(state.model_copy(update={"history": state.history[:3] + state.history[4:]}))
    assert_fallback(state.model_copy(update={"history": (state.history[0],) * 4 + state.history[4:]}))
    fresh_profiles = tuple(d.model_copy(update={"known_data": tuple(
        x.model_copy(update={"observed_at": MAX_AGE + 20, "available_at": MAX_AGE + 20})
        for x in d.known_data)}) for d in state.demands)
    assert_fallback(state.model_copy(update={"now": MAX_AGE + 20, "demands": fresh_profiles}))


def test_missing_wrong_owner_wrong_source_and_conflicting_profiles_fall_back():
    state = example()
    first = state.demands[0]
    assert_fallback(state.model_copy(update={"metadata": ()}))
    for bad in ((), (datum("estimated_compute_seconds", 10., "other"), first.known_data[1]),
                (datum("estimated_compute_seconds", 10., "first", source="request"), first.known_data[1]),
                (*first.known_data, datum("estimated_compute_seconds", 99., "first"))):
        assert_fallback(state.model_copy(update={"demands": (first.model_copy(update={"known_data": bad}),
                                                            state.demands[1])}))


def test_future_data_is_rejected_and_copy_bypass_is_filtered():
    state = example()
    future = datum("notice_seconds", 500., "gpu-z", when=11.)
    with pytest.raises(ValidationError):
        m.DecisionSnapshot.model_validate({**state.model_dump(), "metadata": (future,)})
    assert_fallback(state.model_copy(update={"metadata": (state.metadata[0], future)}))
    # Irrelevant actual/future annotations cannot influence an otherwise supported plan.
    extra = datum("actual_next_window_seconds", 100000., "gpu-z", when=11.)
    contaminated = state.model_copy(update={"metadata": (*state.metadata, extra)})
    clean_choice = selected(ReliabilityPolicy().plan(state))
    other_choice = selected(ReliabilityPolicy().plan(contaminated))
    assert (clean_choice.demand_id, clean_choice.placement) == (other_choice.demand_id, other_choice.placement)


def test_valid_individual_profiles_with_out_of_range_total_fall_back():
    state = example()
    second = state.demands[1]
    enormous_restart = datum("restart_seconds", m.MAX_SAFE_INTEGER, second.demand_id)
    second = second.model_copy(update={"known_data": (*second.known_data, enormous_restart)})
    plan = assert_fallback(state.model_copy(update={"demands": (state.demands[0], second)}))
    assert "score contract" in plan.rationale


def test_compatibility_masks_cannot_be_overridden_by_risk_estimate():
    state = example()
    second = profiled_demand("second", 30., 10., arrived=1., model="H100")
    inventory = (state.inventory[0], node("node-z", (device("gpu-z", model="H100"),)))
    plan = ReliabilityPolicy().plan(state.model_copy(update={"demands": (state.demands[0], second),
                                                            "inventory": inventory}))
    assert selected(plan).placement.slices[0].node_id == "node-a"
    assert selected(plan).feasibility == "legal"
    assert "inactive" not in plan.rationale


def test_incomplete_candidate_search_and_unsupported_shapes_fall_back():
    state = example()
    assert_fallback(state, max_candidates=1)
    for request in (m.GPURequest(count=0), m.GPURequest(count=2),
                    m.GPURequest(count=1, profile="mig-2g.20gb")):
        changed = state.demands[0].model_copy(update={"request": m.ResourceRequest(
            cpu_millicores=1000, memory_mib=2048, gpu=request)})
        assert_fallback(state.model_copy(update={"demands": (changed, state.demands[1])}))
    multi = node("multi", (device("x"), device("y")))
    assert_fallback(state.model_copy(update={"inventory": (*state.inventory, multi)}))
    too_many = tuple(node(f"node-{i}", (device(f"gpu-{i}"),)) for i in range(7))
    assert_fallback(state.model_copy(update={"inventory": too_many}))


def test_no_full_legal_cohort_matching_falls_back_without_skipping_demand():
    state = example()
    specific = tuple(profiled_demand(name, 20., 2., model="A100", arrived=i)
                     for i, name in enumerate(("first", "second", "third")))
    inventory = (state.inventory[0], node("node-z", (device("gpu-z", model="H100"),)))
    # A flexible third task creates a second legal node, but the admitted first two
    # cannot both use the single A100. The policy must not swap in the third task.
    flexible = profiled_demand("third", 10., .1, arrived=2.)
    assert_fallback(state.model_copy(update={"demands": (*specific[:2], flexible),
                                            "inventory": inventory}))
