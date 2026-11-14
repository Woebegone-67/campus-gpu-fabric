#!/usr/bin/env python3
"""CP3 causal-prefix attacks and independent complete committed-state replay.

Public events retain their bounded resource projection. A private, atomic state
journal supplies missing payloads; its independent reducer imports no production
code. Repeated Kernel runs are only causal attacks, never independent replay.
"""
from __future__ import annotations

import argparse
import copy
import sqlite3
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from cp3_b_evidence import finish, receipt, retain  # noqa: E402
from cp3_event_fold import claims_from_final_snapshot, fold_events  # noqa: E402
from cp3_state_replay import checksum, read_sql_state, replay  # noqa: E402

from campus_fabric.storage.sqlite import SQLiteStore  # noqa: E402
from campus_fabric.workflows import experiment_runner as runner  # noqa: E402

POLICIES = ("B0", "B1", "B2", "P")
CUTOFF = 5
REPLAY_GAPS = []


def attack_inputs():
    base = runner.scenario_input(ROOT, "cp2-recall-v1", 7)
    base.update(scenario_id="cp3-prefix-attack-v1", scope="Dedicated causal-prefix attack, not policy performance evidence", horizon=25)
    base["works"] = [
        {"intent_id": "prefix-visible", "owner_id": "alice", "arrived_at": 0., "gpu_model": "A10", "timeout_seconds": 100},
        {"intent_id": "future-work", "owner_id": "bob", "arrived_at": 8., "gpu_model": "A10", "timeout_seconds": 100}]
    base["events"] = [{"at": 9, "kind": "recall", "device_ids": ["sim-a10-gpu0"], "notice_seconds": 2, "duration_seconds": 4}]
    variants = {"baseline": base}
    for name in ("changed-future-arrival", "deleted-future-arrival", "changed-future-recall", "deleted-future-recall", "changed-hidden-duration"):
        variants[name] = copy.deepcopy(base)
    variants["changed-future-arrival"]["works"][1].update(arrived_at=12., gpu_model="A100")
    variants["deleted-future-arrival"]["works"] = variants["deleted-future-arrival"]["works"][:1]
    variants["changed-future-recall"]["events"][0].update(at=11, notice_seconds=1, duration_seconds=7)
    variants["deleted-future-recall"]["events"] = []
    variants["changed-hidden-duration"]["model"]["duration_seconds"] = 40
    return variants


def causal_record(result, cutoff):
    snapshot = result["snapshot"].model_dump(mode="json")
    # Wall-clock profiling is deliberately outside deterministic causal output.
    plans = [{"snapshot": row["snapshot"], "plan": row["plan"]}
             for row in result["decisions"] if row["snapshot"]["now"] <= cutoff]
    return {"events": [e for e in snapshot["events"] if e["logical_time"] <= cutoff], "decisions": plans}


def execute(inputs, policy, path):
    observations = {}
    original = SQLiteStore.transaction

    @contextmanager
    def observe_commits(store):
        # Observe the actual SQL state in each transaction; retain it only after
        # COMMIT succeeds. No production serializer or reducer is the oracle.
        observed = None
        with original(store) as connection:
            yield connection
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "fabric_journal" in tables:
                head = connection.execute("SELECT MAX(sequence) FROM fabric_journal").fetchone()[0]
                if head is not None:
                    actual = read_sql_state(connection)
                    if not isinstance(actual, dict):
                        raise ValueError("Committed journal has no authoritative SQL state")
                    observed = (head, checksum(actual))
        if observed is not None:
            observations[observed[0]] = observed[1]

    with patch.object(SQLiteStore, "transaction", observe_commits):
        result = runner.execute_scenario(copy.deepcopy(inputs), policy, db_path=path)
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN")
        actual = read_sql_state(connection)
    store = SQLiteStore(path)
    committed = store.export_journal()
    store.close()
    rebuilt = replay(committed, observed_prefixes=observations)
    assert checksum(rebuilt) == checksum(actual), "Complete persisted state differs"
    assert set(observations) == set(range(1, committed["head_sequence"] + 1)), "A write prefix was not independently observed"
    result["state_replay"] = {"journal": committed, "observed_commit_prefixes": observations,
                              "final_persisted_state": actual,
                              "rebuilt_sha256": checksum(rebuilt),
                              "scope": "Complete committed JSON from initialization; private journal, not public event-only replay."}
    assert not result["failures"], result["failures"]
    for row in result["decisions"]:
        s = row["snapshot"]
        assert all(d["arrived_at"] <= s["now"] for d in s["demands"])
        assert all(n["issued_at"] <= s["now"] for n in s["notices"])
        assert not ({"completion_at", "duration_seconds", "future_arrivals", "future_recalls"} & s.keys())
    return result


def exercise(directory, policies=POLICIES, *, artifacts=None, checks=None):
    variants = attack_inputs()
    artifacts = {} if artifacts is None else artifacts
    checks = [] if checks is None else checks
    for policy in policies:
        reference = None
        reference_state = None
        for name, inputs in variants.items():
            result = execute(inputs, policy, directory / f"{policy}-{name}.sqlite")
            prefix = causal_record(result, CUTOFF)
            prefix_inputs = {**copy.deepcopy(inputs), "horizon": CUTOFF}
            state_run = execute(prefix_inputs, policy, directory / f"{policy}-{name}-prefix.sqlite")
            prefix_state = state_run["snapshot"].model_dump(mode="json")
            # Retain actual inputs/output before comparisons so a failing
            # counterexample survives instead of becoming an empty receipt.
            snapshot = result["snapshot"].model_dump(mode="json")
            record = {"inputs": inputs, "receipts": result["receipts"],
                "decisions": result["decisions"], "final_snapshot": snapshot,
                "cutoff_snapshot": prefix_state, "state_replay": result["state_replay"],
                "cutoff_state_replay": state_run["state_replay"]}
            artifacts[f"{policy}-{name}"] = record
            if reference is None:
                reference, reference_state = prefix, prefix_state
            else:
                assert prefix == reference, (policy, name, "Future input changed the common event/decision prefix")
                assert prefix_state == reference_state, (policy, name, "Future input changed the cutoff state")
            folded = fold_events(inputs["inventory"], snapshot["events"])
            record["fold"] = folded
            assert folded["claims"] == claims_from_final_snapshot(snapshot), (policy, name, "Independent claim fold differs")
            # Also check an active, nonempty occupancy prefix so an all-zero
            # terminal fold cannot accidentally pass every projection check.
            active = fold_events(inputs["inventory"], prefix_state["events"])
            record["cutoff_fold"] = active
            assert active["claims"] == claims_from_final_snapshot(prefix_state)
            assert active["claims"], "Prefix fixture must hold a real allocation"
        # Negative control: altering an already visible request MUST be observed.
        changed = copy.deepcopy(variants["baseline"])
        changed["works"][0]["gpu_model"] = "A100"
        negative = execute(changed, policy, directory / f"{policy}-visible-control.sqlite")
        assert causal_record(negative, CUTOFF) != reference, "Attack comparator failed its visible-input control"
        artifacts[f"{policy}-visible-control"] = {
            "inputs": changed, "prefix": causal_record(negative, CUTOFF),
            "decisions": negative["decisions"],
            "final_snapshot": negative["snapshot"].model_dump(mode="json"),
            "state_replay": negative["state_replay"],
        }
        checks.append({"id": f"{policy}-online-prefix", "status": "PASS", "observed":
            "Five future/hidden-input mutations preserve events, plans and cutoff state through t=5; visible-input negative control differs."})
    checks.append({"id": "independent-resource-event-fold", "status": "PASS", "observed":
        "Frozen initial inventory plus committed deltas independently reconstruct nonempty cutoff claims and final claims, checking every intermediate capacity and device ownership."})
    checks.append({"id": "full-final-state-replay", "status": "PASS", "observed":
        "Independent standard-library reducer reconstructs every observed committed write prefix and final persisted JSON, including workflow stages/results, requests, saved responses, decisions, budgets and bookkeeping. Public events alone remain insufficient; atomic private journal supplies lossless changes."})
    return checks, artifacts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/cp3/prefix_replay_receipt.json")
    args = parser.parse_args(argv)
    result = receipt("CP3-PREFIX-REPLAY", "independent_history", {
        "online": "Actual B0/B1/B2/P plans submitted to isolated A kernels; only the current DecisionSnapshot reaches policy.plan.",
        "prefix": "Repeated production runs establish nonanticipation under the declared attacks, not independent state replay.",
        "replay": "Independent standard-library committed-state journal reducer reconstructs all persisted JSON and compares each write prefix against observed SQL; separate public-event fold checks resource claims.",
        "execution": "Logical simulation with fixed instance-wide hidden duration; no real CPU/GPU claim."})
    artifacts = {}
    try:
        with tempfile.TemporaryDirectory(prefix="fabric-cp3-prefix-") as temp:
            exercise(Path(temp), artifacts=artifacts, checks=result["checks"])
        result["status"] = "PASS"
        result["unresolved_issues"] = list(REPLAY_GAPS)
    except Exception:
        result["status"] = "FAIL"
        result["checks"].append({"id": "prefix-replay-execution", "status": "FAIL", "observed": traceback.format_exc()})
    evidence_dir = args.output.resolve().parent / (args.output.stem + "_histories")
    result["evidence"] = [retain(evidence_dir, name, value) for name, value in artifacts.items()]
    return finish(args.output, result)


if __name__ == "__main__":
    raise SystemExit(main())
