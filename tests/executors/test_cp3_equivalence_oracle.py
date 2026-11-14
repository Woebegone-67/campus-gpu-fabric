"""Challenge the acceptance oracle with a second closed-form calculation."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "cp3_equivalence_oracle_test", ROOT / "tests/acceptance/cp3/cp3_cpu_equivalence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("seed", [0, 17, 2**64 - 1])
@pytest.mark.parametrize("steps", [0, 1, 2, 7, 31, 257])
def test_matrix_oracle_equals_expanded_polynomial(seed, steps):
    # Unroll the affine recurrence algebraically. No production loop is used,
    # and this polynomial calculation shares no matrix code with the oracle.
    expected = (pow(1664525, steps, 2**64) * seed + sum(
        pow(1664525, steps - 1 - index, 2**64) * (1013904223 + index)
        for index in range(steps)
    )) % 2**64
    assert MODULE.oracle(seed, steps) == {"step": steps, "value": expected}


def test_acceptance_rejects_float_or_rounded_uint64_artifacts():
    state = MODULE.oracle(17, 100_003)
    assert state["value"] > 2**53
    MODULE.verify_state({"state": state}, 17, expected_step=100_003)
    for altered in (float(state["value"]), int(float(state["value"])), state["value"] + 1):
        assert altered != state["value"] or type(altered) is float
        with pytest.raises(AssertionError):
            MODULE.verify_state({"state": {"step": state["step"], "value": altered}}, 17)


@pytest.mark.parametrize("seed,steps", [(True, 1), (-1, 1), (2**64, 1), (0, -1), (0, 1.0)])
def test_oracle_rejects_non_specification_inputs(seed, steps):
    with pytest.raises(ValueError):
        MODULE.oracle(seed, steps)
