"""Actual isolated kernel runs; these regression fixtures are not gate receipts."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.workflows import experiment_runner as runner
from experiments.compare_runs import compare_runs, summarize, validate_run

ROOT = Path(__file__).resolve().parents[2]
ADMIN = m.AuthenticatedActor(actor_id="test-admin", role="administrator")
OWNER = m.AuthenticatedActor(actor_id="test-owner", role="researcher", owner_id="sample-owner-0")


@pytest.fixture
def source(tmp_path):
    # Freeze real source metadata while parallel developer agents edit the main checkout.
    root = tmp_path / "source"
    for name in ("configs/cp1_inventory.json", "configs/semantics.json", "scripts/check_checkpoint.py", "experiments/compare_runs.py"):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    return root


@pytest.fixture
def service(source, tmp_path):
    value = runner.ExperimentService(source, tmp_path / "runs")
    yield value
    value.close()


def run(service, scenario="cp2-contention-v1", policy="P", seed=0):
    initial = service.submit(actor=ADMIN, scenario_id=scenario, policy=policy, seed=seed)
    assert initial.status == "running" and not initial.outputs
    return service.wait(initial.run_id, timeout=30)


def artifact(service, record, name):
    raw, media = service.artifact(ADMIN, record.run_id, name)
    assert media == "application/json"
    return json.loads(raw)


@pytest.mark.parametrize("policy", runner.POLICIES)
def test_registered_recall_runs_preserve_work_job_and_lease_identity(service, policy):
    record = run(service, "cp2-recall-v1", policy)
    assert record.status == "completed", record.exceptions
    summary = artifact(service, record, "summary")
    final = artifact(service, record, "final-snapshot")
    events = artifact(service, record, "events")
    assert len(summary["works"]) == 3
    assert all(w["status"] == "completed" for w in summary["works"])
    restarted = [i for i in final["intents"] if len(i["attempts"]) > 1]
    assert restarted
    for intent in restarted:
        assert len(intent["allocation_ids"]) == len(intent["job_ids"]) == 1
        assert {a["ref"]["job_id"] for a in intent["attempts"]} == set(intent["job_ids"])
    assert {"checkpoint.reported", "allocation.preempted", "job.restarted"} <= {e["event_type"] for e in events}
    assert not record.checks  # Runner execution does not assert CP2/CP3/T-REAL acceptance.
    assert record.environment.execution_mode == "logical_simulation"


def test_failed_and_unfinished_work_stay_in_paired_denominator(service):
    left = run(service, "cp2-negative-control-v1", "B2")
    right = run(service, "cp2-negative-control-v1", "P")
    assert left.status == right.status == "completed"
    b, p = artifact(service, left, "summary"), artifact(service, right, "summary")
    pair = compare_runs(b, p)
    assert pair["baseline"]["arrived"] == 4
    assert pair["baseline"]["failed"] >= 1
    assert pair["baseline"]["not_completed_at_horizon"] >= 2
    assert any(w["status"] == "unfinished" for w in b["works"])
    assert len(pair["per_work"]) == 4
    assert b["input_sha256"] == p["input_sha256"]
    assert pair["baseline"]["unfulfilled_demand_area"] >= 80


def test_real_node_fault_remains_recorded_and_service_has_no_operating_kernel(service, tmp_path):
    operating = Kernel(tmp_path / "operating.sqlite")
    before = operating.export_state(ADMIN)
    record = run(service, "cp2-node-loss-v1")
    assert record.status == "completed"
    data = artifact(service, record, "summary")
    assert summarize(validate_run(data), data["horizon"])["failed"] > 0
    assert operating.export_state(ADMIN) == before
    assert not list(service.output_root.rglob("kernel.sqlite"))
    assert not hasattr(service, "kernel")


def test_repeat_seed_is_reproducible_except_ids_and_measured_policy_time(service):
    a, b = run(service), run(service)
    sa, sb = artifact(service, a, "summary"), artifact(service, b, "summary")
    sa.pop("run_id")
    sb.pop("run_id")
    assert sa == sb
    assert artifact(service, a, "events") == artifact(service, b, "events")
    da, db = artifact(service, a, "decisions"), artifact(service, b, "decisions")
    for item in (*da, *db):
        item.pop("elapsed_ns")
        item.pop("same_snapshot_diagnostic_ns")
    assert da == db


def test_changed_future_arrival_and_notice_cannot_change_earlier_prefix(source, tmp_path):
    first = runner.scenario_input(source, "cp2-contention-v1", 1)
    first["works"][-1]["arrived_at"] = 25.
    second = copy.deepcopy(first)
    second["works"][-1]["gpu_model"] = "H100"
    second["events"] = [{"at": 25, "kind": "recall", "device_ids": ["sim-a10-gpu0"], "notice_seconds": 4, "duration_seconds": 5}]
    outputs = [runner.execute_scenario(s, "P", db_path=tmp_path / f"{index}.sqlite") for index, s in enumerate((first, second))]
    plans = [[r["plan"] for r in output["decisions"] if r["snapshot"]["now"] < 25] for output in outputs]
    events = [[e for e in output["snapshot"].events if e.logical_time < 25] for output in outputs]
    assert plans[0] == plans[1]
    assert events[0] == events[1]


def test_auth_bounds_registered_download_and_hash_integrity(service):
    with pytest.raises(PermissionError):
        service.submit(actor=OWNER, scenario_id="cp2-contention-v1", policy="P")
    with pytest.raises(PermissionError):
        service.list_runs(OWNER)
    for seed in (-1, 10000, True, 1.5):
        with pytest.raises(ValueError):
            service.submit(actor=ADMIN, scenario_id="cp2-contention-v1", policy="P", seed=seed)
    with pytest.raises(ValueError):
        service.submit(actor=ADMIN, scenario_id="../../db", policy="P")
    record = run(service)
    with pytest.raises(PermissionError):
        service.artifact(OWNER, record.run_id, "input")
    with pytest.raises(KeyError):
        service.artifact(ADMIN, record.run_id, "../../input")
    path = service.output_root / record.run_id / "summary.json"
    path.write_text('{}')
    with pytest.raises(ValueError, match="integrity"):
        service.artifact(ADMIN, record.run_id, "summary")


def test_restart_discovers_completed_and_marks_interrupted_run_failed(service, source, tmp_path):
    record = run(service)
    service.close()
    reopened = runner.ExperimentService(source, service.output_root)
    try:
        assert reopened.list_runs(ADMIN)[0].run == record
        assert artifact(reopened, record, "summary")["works"]
    finally:
        reopened.close()
    path = service.output_root / record.run_id / "evidence.json"
    value = record.model_copy(update={"status": "running", "outputs": ()})
    path.write_text(value.model_dump_json())
    resumed = runner.ExperimentService(source, service.output_root)
    try:
        failed = resumed.list_runs(ADMIN)[0].run
        assert failed.status == "failed" and failed.exceptions
        assert not failed.outputs
    finally:
        resumed.close()


def test_execution_failure_and_changed_build_are_not_completed(service, monkeypatch, source):
    original = runner.execute_scenario
    def fail_after_real_run(*args, **kwargs):
        result = original(*args, **kwargs)
        result["failures"].append({"kind": "fixture_failure", "detail": "regression-only"})
        (source / "configs/semantics.json").write_text('{}')
        return result
    monkeypatch.setattr(runner, "execute_scenario", fail_after_real_run)
    record = run(service)
    assert record.status == "failed" and len(record.exceptions) == 2
    assert artifact(service, record, "summary")["runner_failures"]
    assert len(artifact(service, record, "summary")["works"]) == 8


def test_service_queue_is_bounded_and_directory_has_one_writer(service, source, monkeypatch):
    import threading
    release = threading.Event()
    original = runner.execute_scenario
    def delayed(*args, **kwargs):
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(runner, "execute_scenario", delayed)
    records = []
    try:
        with pytest.raises(ValueError, match="owns this output"):
            runner.ExperimentService(source, service.output_root)
        for _ in range(4):
            records.append(service.submit(actor=ADMIN, scenario_id="cp2-negative-control-v1", policy="B0"))
        with pytest.raises(ValueError, match="four"):
            service.submit(actor=ADMIN, scenario_id="cp2-negative-control-v1", policy="B0")
    finally:
        release.set()
        for record in records:
            service.wait(record.run_id, timeout=30)
    assert all(r.run.status == "completed" for r in service.list_runs(ADMIN))
