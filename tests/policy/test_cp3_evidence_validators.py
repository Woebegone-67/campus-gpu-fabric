"""Development checks for CP3 evidence instruments; no build-bound gate receipt."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

VALIDATORS = Path(__file__).resolve().parents[1] / "acceptance/cp3"
sys.path.insert(0, str(VALIDATORS))
from cp3_decision_coverage import CASES, POLICIES  # noqa: E402
from cp3_decision_coverage import exercise as http_exercise  # noqa: E402
from cp3_event_fold import fold_events  # noqa: E402
from cp3_prefix_replay import REPLAY_GAPS  # noqa: E402
from cp3_prefix_replay import exercise as prefix_exercise  # noqa: E402


@pytest.mark.parametrize("policy", POLICIES)
def test_all_online_policies_preserve_future_input_prefixes_and_complete_state_replay(tmp_path, policy):
    checks, artifacts = prefix_exercise(tmp_path, policies=(policy,))
    assert len(artifacts) == 7
    assert next(c for c in checks if c["id"] == "full-final-state-replay")["status"] == "PASS"
    assert not REPLAY_GAPS
    assert all(c["status"] == "PASS" for c in checks)
    for record in list(artifacts.values())[:6]:
        audit = record["state_replay"]
        assert len(audit["observed_commit_prefixes"]) == audit["journal"]["head_sequence"]
        assert audit["final_persisted_state"]["intents"]
        assert audit["final_persisted_state"]["dedup"]
        assert audit["final_persisted_state"]["payloads"]


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("case", CASES)
def test_actual_http_explanations_acceptance_rejection_and_candidate_budget(tmp_path, policy, case):
    check, trace, history = http_exercise(tmp_path / "instance", policy, case)
    assert check["status"] == "PASS", check["observed"]
    assert trace["submissions"]
    assert any(row.get("kind") == "http" for row in history)
    assert any(row.get("kind") == "process_stopped" for row in history)


def delta_fixture():
    inventory = [{"node_id": "n", "capacity": {"cpu_millicores": 4, "memory_mib": 8,
        "ephemeral_storage_mib": 16}, "devices": [{"device_id": "g"}]}]
    event = {"event_id": "ev1", "sequence": 1, "version": {"epoch": 0, "revision": 1},
        "logical_time": 0, "event_type": "allocation.granted", "state_changes": [],
        "resource_deltas": [{"node_id": "n", "allocation_id": "a", "cpu_millicores": 1,
            "memory_mib": 2, "ephemeral_storage_mib": 3, "device_ids_added": ["g"], "device_ids_removed": []}]}
    return inventory, event


def test_independent_fold_negative_controls_catch_corruption():
    inventory, event = delta_fixture()
    expected = {"n/a": {"cpu_millicores": 1, "memory_mib": 2, "ephemeral_storage_mib": 3, "devices": ["g"]}}
    assert fold_events(inventory, [event])["claims"] == expected
    for mutation in ("sequence", "unknown-device", "excess-resource", "unowned-release", "duplicate-device"):
        bad = copy.deepcopy(event)
        events = [bad]
        if mutation == "sequence":
            bad["sequence"] = 2
        elif mutation == "unknown-device":
            bad["resource_deltas"][0]["device_ids_added"] = ["other"]
        elif mutation == "excess-resource":
            bad["resource_deltas"][0]["cpu_millicores"] = 99
        elif mutation == "unowned-release":
            bad["resource_deltas"][0].update(device_ids_added=[], device_ids_removed=["g"])
        else:
            bad.update(sequence=2, event_id="ev2")
            bad["resource_deltas"][0]["allocation_id"] = "second-allocation"
            events = [event, bad]
        with pytest.raises(AssertionError):
            fold_events(inventory, events)


def test_event_fold_has_no_production_oracle_dependency():
    # The fold must remain importable as a plain standalone file. A producer
    # bug must not be reproduced by calling its own reducer/placement helper.
    import ast
    tree = ast.parse((VALIDATORS / "cp3_event_fold.py").read_text())
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert imports == ["__future__"]
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
