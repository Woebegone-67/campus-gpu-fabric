"""Causal boundaries, failure denominators and sealed-output protections."""
import copy
import json

import pytest

from experiments.research import reliability_comparison as study


@pytest.fixture
def task():
    return {"task_id": "task-0", "estimated_compute_seconds": 40.,
            "checkpoint_seconds": 2., "restart_seconds": .5, "stop_seconds": .1}


@pytest.fixture
def device():
    return {"device_id": "device-0", "windows": [10.] * 16, "notice_seconds": 12.}


def actual(task, *, runtime=None, checkpoint=None, failed=False):
    return {"compute_seconds": runtime if runtime is not None else task["estimated_compute_seconds"],
            "checkpoint_seconds": checkpoint if checkpoint is not None else task["checkpoint_seconds"],
            "checkpoint_fails": failed}


def future(*, window=10., notice=12., stop=.1):
    return {"window_seconds": window, "notice_seconds": notice, "stop_seconds": stop}


def test_actual_future_mutation_cannot_change_any_online_assignment():
    case = study.generate_case(4, "predictable_mixed")
    changed = copy.deepcopy(case)
    for item in changed["actual"]["devices"]:
        item.update(window_seconds=.0001, notice_seconds=1000.)
    for item in changed["actual"]["tasks"]:
        item.update(compute_seconds=10000., checkpoint_fails=True)
    for policy in study.POLICIES:
        assert study.select(policy, case["observed"]) == study.select(policy, changed["observed"])


@pytest.mark.parametrize("regime", study.REGIMES)
def test_frozen_generator_is_deterministic_and_same_cohort_is_admitted(regime):
    case = study.generate_case(7, regime)
    assert case == study.generate_case(7, regime)
    for policy in study.POLICIES:
        assert sorted(study.select(policy, case["observed"])) == [0, 1, 2, 3]
    assert all(len(d["windows"]) == 16 for d in case["observed"]["devices"])


def test_runtime_errors_and_reversed_history_do_not_change_online_information():
    regular = study.generate_case(8, "predictable_mixed")
    for regime in ("runtime_estimate_error", "reversed_history"):
        changed = study.generate_case(8, regime)
        assert changed["observed"] == regular["observed"]
        assert changed["actual"] != regular["actual"]


def test_loss_baseline_ranks_observed_interruption_cost_not_runtime(monkeypatch):
    observed = study.generate_case(0, "baseline_controls")["observed"]
    # Histories are tied, so target devices are in lexical/index order.
    monkeypatch.setattr(study.reliability, "cost_matrix", lambda *args: (
        (1., 0., 0., 0.), (4., 0., 0., 0.), (2., 0., 0., 0.), (3., 0., 0., 0.)))
    assert study.select("loss_sort", observed) == (3, 0, 2, 1)


def test_no_recall_control_has_no_interruption_cost_for_every_arm():
    case = study.generate_case(0, "baseline_controls")
    results = [study.run_case(case, policy) for policy in study.POLICIES]
    assert all(r["completed"] == r["admitted"] == 4 for r in results)
    assert all(r["failed"] == r["recalls"] == r["waste_and_overhead_seconds"] == 0 for r in results)
    assert {r["mean_completion_seconds"] for r in results} == {59.}


def test_saving_blocks_compute_and_owner_use_is_a_fixed_return_window(task, device):
    result = study.execute_task(task, device, actual(task), future())
    assert result["action"] == "save_now"
    assert result["completion_seconds"] == pytest.approx(10 + 12 + 10 + .5 + 30)
    assert result["checkpoint_seconds"] == 2.
    assert result["owner_wait_seconds"] == pytest.approx(2.1)
    assert result["compute_waste_seconds"] == 0.


def test_failed_checkpoint_cold_restarts_and_reports_all_wasted_compute(task, device):
    result = study.execute_task(task, device, actual(task, failed=True), future())
    assert result["completed"]
    assert result["checkpoint_failed"]
    assert result["compute_waste_seconds"] == 10.
    assert result["completion_seconds"] == pytest.approx(10 + 12 + 10 + .5 + 40)
    assert result["overhead_seconds"] == 2.5


def test_nonrestartable_checkpoint_failure_is_not_dropped(task, device):
    result = study.execute_task(task, device, actual(task, failed=True), future(),
                                allow_cold_restart=False)
    assert not result["completed"]
    assert result["completion_seconds"] is None
    assert result["truncated_completion_seconds"] == 400.
    assert result["compute_waste_seconds"] == 10.
    assert result["checkpoint_seconds"] == 2.
    assert result["restart_seconds"] == 0.


def test_actual_runtime_cannot_enable_a_more_favorable_action(task, device):
    task["estimated_compute_seconds"] = 15.
    result = study.execute_task(task, device, actual(task, runtime=40.), future())
    assert result["action"] == "finish_or_save"
    assert result["checkpoint_seconds"] == 2.
    assert result["completion_seconds"] == pytest.approx(10 + 22 + .5 + (40 - 19.8))
    assert result["compute_waste_seconds"] == 0.


def test_notice_deadline_becomes_public_only_to_recall_responder(task, device):
    result = study.execute_task(task, device, actual(task), future(notice=1.))
    assert result["action"] == "stop"
    assert result["owner_wait_seconds"] == pytest.approx(.1)
    assert result["compute_waste_seconds"] == 10.


def test_late_return_is_reported_separately(task, device):
    task["checkpoint_seconds"] = 20.
    result = study.execute_task(task, device, actual(task), future(notice=.05, stop=.2))
    assert result["action"] == "stop"
    assert result["return_lateness_seconds"] == pytest.approx(.15)


def test_checkpoint_finishing_at_stop_boundary_does_not_count_as_saved(task, device):
    result = study.execute_task(task, device, actual(task, checkpoint=11.9), future(),
                                allow_cold_restart=False)
    assert result["checkpoint_failed"]
    assert not result["completed"]
    assert result["compute_waste_seconds"] == 10.


def test_strict_failures_stay_in_admitted_count_and_completion_denominator():
    case = study.generate_case(0, "short_notice")
    for task in case["observed"]["tasks"]:
        task.update(estimated_compute_seconds=40., checkpoint_seconds=8.)
    for task in case["actual"]["tasks"]:
        task.update(compute_seconds=40., checkpoint_seconds=8.)
    for item in case["actual"]["devices"]:
        item["window_seconds"] = 10.
    result = study.run_case(case, "id_order", allow_cold_restart=False)
    assert result["admitted"] == result["failed"] == 4
    assert result["completed"] == result["completed_useful_compute_seconds"] == 0
    assert result["mean_completion_seconds"] == 400.


def test_freeze_has_protocol_and_source_hashes_and_refuses_overwrite(tmp_path):
    protocol = study.ROOT / "experiments/research/reliability_protocol.json"
    output = tmp_path / "new"
    study.freeze(protocol, output)
    frozen = json.loads((output / "freeze.json").read_text())
    assert frozen["outcomes_started"] is False
    assert set(frozen["sha256"]) == {"protocol.json", "protocol.md", "runner.py", "planner.py", "inputs.json"}
    assert len(json.loads((output / "inputs.json").read_text())["cases"]) == 180
    assert not (output / "report.json").exists()
    with pytest.raises(FileExistsError):
        study.freeze(protocol, output)


def test_frozen_input_tampering_fails_before_any_outcome(tmp_path):
    output = tmp_path / "new"
    study.freeze(study.ROOT / "experiments/research/reliability_protocol.json", output)
    (output / "inputs.json").write_text("{}")
    with pytest.raises(ValueError, match="Frozen file changed"):
        study.run_frozen(output)
    assert not (output / "RUN_STARTED").exists()


def test_partial_or_completed_run_cannot_be_silently_reused(tmp_path):
    output = tmp_path / "new"
    study.freeze(study.ROOT / "experiments/research/reliability_protocol.json", output)
    (output / "RUN_STARTED").touch()
    with pytest.raises(FileExistsError):
        study.run_frozen(output)
