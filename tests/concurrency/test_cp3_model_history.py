"""Independent bounded resource model and real concurrent-history checks."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))

from cp3_history import (  # noqa: E402
    SHAPES,
    boundary_scenarios,
    invoke,
    new_kernel,
    oracle_controls,
    run_concurrent,
    run_sequential,
)
from cp3_reference import Reference  # noqa: E402


def test_oracle_rejects_impossible_observations_and_real_time_reordering():
    controls = oracle_controls()
    assert controls["real_time_violation_rejected"]
    assert controls["overlap_reverse_order_accepted"]
    assert controls["wrong_state_deadline_status_rejected"] == 3


def test_exact_deadline_and_return_boundaries(tmp_path):
    evidence = boundary_scenarios(tmp_path / "boundary.sqlite")
    assert len(evidence["history"]) == 17
    assert evidence["final"]["free"] == [4, 8, 6, 2]


def test_explicit_defaults_and_omission_have_same_replay_identity(tmp_path):
    kernel, model = new_kernel(tmp_path / "defaults.sqlite"), Reference()
    op = dict(kind="create", owner="alice", shape=list(SHAPES[0]), key="same", request_id="first")
    first = invoke(kernel, op)
    model.step(op, first)
    repeated = {**op, "ttl": 30, "priority": 0, "request_id": "repeat"}
    second = invoke(kernel, repeated)
    model.step(repeated, second)
    assert second == first


@pytest.mark.parametrize("seed", range(8))
def test_generated_interleavings_match_independent_model(tmp_path, seed):
    evidence = run_sequential(tmp_path / "history.sqlite", seed, 64)
    assert len(evidence["history"]) == 64


@pytest.mark.parametrize("seed", range(10))
def test_real_concurrency_has_legal_real_time_preserving_order(tmp_path, seed):
    evidence = run_concurrent(tmp_path / "concurrent.sqlite", seed)
    assert len(evidence["history"]) == 6
    assert evidence["search"]["linearizable"]
    # Every first-wave reply precedes every second-wave invocation. Sorting
    # entirely by completion time or ignoring the order would not suffice.
    assert evidence["search"]["real_time_edges"] >= 9
