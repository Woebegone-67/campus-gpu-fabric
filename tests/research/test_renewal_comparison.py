"""Independent calendar/progress semantics and before-outcome sealing guards."""
import copy
import json

import pytest

from experiments.research import renewal_comparison as study


def task(runtime=20., checkpoint=1.):
    return {"task_id": "task-0", "estimated_compute_seconds": runtime,
            "checkpoint_seconds": checkpoint, "restart_seconds": .5,
            "stop_seconds": .1, "remaining_recoveries": 2}


def device():
    return {"device_id": "device-0", "windows": [5.] * 16,
            "notice_seconds": 3., "owner_use_seconds": 10.}


def actual(spec, **changes):
    return {"compute_seconds": spec["estimated_compute_seconds"],
            "checkpoint_seconds": spec["checkpoint_seconds"], "checkpoint_fails": False, **changes}


def calendar(windows, *, notice=3., stop=.1):
    return {"windows": windows, "notice_seconds": notice, "stop_seconds": stop,
            "owner_use_seconds": 10.}


def source_cases():
    return json.loads((study.ROOT / "evidence/reliability-placement-20261115-run01/inputs.json").read_text())["cases"]


def test_all_original_cases_retained_and_independent_calendars_cover_horizon():
    cases = source_cases()
    assert len(cases) == 180
    for source in cases:
        generated = study.generate_case(source)
        assert generated == study.generate_case(source)
        for old, new in zip(source["observed"]["tasks"], generated["observed"]["tasks"], strict=True):
            assert all(new[key] == value for key, value in old.items())
            assert new["remaining_recoveries"] == 2
        assert source["actual"]["tasks"] == generated["actual"]["tasks"]
        for old, observed, future in zip(source["observed"]["devices"],
                                         generated["observed"]["devices"],
                                         generated["actual"]["devices"], strict=True):
            assert observed["windows"] == old["windows"]
            assert len(future["windows"]) == 64
            assert sum(future["windows"]) + 64 * (future["notice_seconds"] + 10) > 900


def test_old_six_selectors_are_identical_and_new_selectors_cannot_read_future():
    source = source_cases()[20]
    case = study.generate_case(source)
    changed = copy.deepcopy(case)
    for future in changed["actual"]["devices"]:
        future["windows"] = [.01] * 64
    for physical in changed["actual"]["tasks"]:
        physical.update(compute_seconds=10000., checkpoint_fails=True)
    for policy in study.POLICIES:
        chosen = study.select(policy, case["observed"])
        assert sorted(chosen) == [0, 1, 2, 3]
        assert chosen == study.select(policy, changed["observed"])
        if policy in study.first.POLICIES:
            assert chosen == study.first.select(policy, source["observed"])


def test_completion_before_first_notice_has_no_restart_or_recall():
    spec = task(runtime=4.)
    result = study.execute_task(spec, device(), actual(spec), calendar([5.]), horizon=100.)
    assert result["completion_seconds"] == 4.
    assert result["restart_seconds"] == result["recalls"] == result["owner_wait_seconds"] == 0


def test_fixed_calendar_does_not_advance_after_early_return():
    spec = task()
    result = study.execute_task(spec, device(), actual(spec), calendar([5., 30.]), horizon=100.)
    assert result["completion_seconds"] == pytest.approx(5 + 3 + 10 + .5 + 15)
    assert result["owner_wait_seconds"] == pytest.approx(1.1)
    assert result["checkpoint_seconds"] == 1.
    assert result["restart_seconds"] == .5
    cheaper = task(checkpoint=.5)
    other = study.execute_task(cheaper, device(), actual(cheaper), calendar([5., 30.]), horizon=100.)
    assert other["completion_seconds"] == result["completion_seconds"]
    assert other["owner_wait_seconds"] == pytest.approx(.6)


def test_failed_second_checkpoint_preserves_first_durable_progress_even_in_strict_mode():
    spec = task()
    result = study.execute_task(spec, device(), actual(spec, checkpoint_failure_windows=[1]),
                                calendar([5., 5., 20.]), horizon=100., allow_cold_restart=False)
    assert result["completed"]
    assert result["completion_seconds"] == pytest.approx(51.5)
    assert result["checkpoint_failures"] == 1
    assert result["compute_waste_seconds"] == pytest.approx(4.5)
    assert result["durable_progress_seconds"] == 5.
    assert result["physical_compute_seconds"] == pytest.approx(24.5)
    assert result["recoveries"] == 2


def test_restart_consumes_available_window_and_budget_cannot_be_bypassed():
    spec = task(runtime=10., checkpoint=5.)
    result = study.execute_task(spec, device(), actual(spec), calendar([5., 10., 10., 100.], notice=2.),
                                horizon=100., max_recoveries=2)
    assert not result["completed"]
    assert result["budget_exhausted"] and not result["censored"]
    assert result["noncompletion_reason"] == "budget_exhausted"
    assert result["completion_seconds"] is None
    assert result["restricted_completion_seconds"] == 100.
    assert result["compute_waste_seconds"] == pytest.approx(5 + 9.5 + 9.5)
    assert result["restart_seconds"] == 1.
    assert result["recoveries"] == 2
    assert result["recalls"] == 3


def test_zero_recovery_budget_never_starts_second_attempt():
    spec = task()
    result = study.execute_task(spec, device(), actual(spec), calendar([5., 100.]),
                                horizon=100., max_recoveries=0)
    assert result["budget_exhausted"]
    assert result["restart_seconds"] == result["recoveries"] == 0
    assert result["recalls"] == 1
    assert result["durable_progress_seconds"] == 5.


def test_restart_in_progress_at_horizon_records_only_partial_overhead():
    spec = task()
    result = study.execute_task(spec, device(), actual(spec), calendar([5., 30.]), horizon=18.25)
    assert result["censored"] and not result["budget_exhausted"]
    assert result["noncompletion_reason"] == "horizon"
    assert result["restart_seconds"] == .25
    assert result["physical_compute_seconds"] == 5.
    assert result["completion_seconds"] is None


def test_checkpoint_in_progress_at_horizon_counts_partial_save_cost_without_saved_claim():
    spec = task(runtime=40., checkpoint=4.)
    result = study.execute_task(spec, device(), actual(spec), calendar([10.], notice=12.), horizon=11.)
    assert result["checkpoint_seconds"] == 1.
    assert result["durable_progress_seconds"] == 0.
    assert result["progress_seconds"] == 10.
    assert result["compute_waste_seconds"] == 0.
    assert result["censored"] and result["pending_returns"] == 1
    assert result["owner_wait_seconds"] == 1.


def test_wrong_runtime_estimate_can_trigger_fallback_but_not_oracle_action():
    spec = task(runtime=12., checkpoint=2.)
    result = study.execute_task(spec, device(), actual(spec, compute_seconds=40.),
                                calendar([10.], notice=12.), horizon=12.)
    assert result["episodes"][0]["action"] == "finish_or_save"
    assert result["physical_compute_seconds"] == 12.
    assert result["checkpoint_seconds"] == 0.
    assert result["censored"] and result["pending_returns"] == 1


def test_lease_end_is_not_reported_as_success_or_budget_exhaustion():
    spec = task(runtime=1000.)
    result = study.execute_task(spec, device(), actual(spec), calendar([1000.]), horizon=900.)
    assert not result["completed"] and result["censored"]
    assert not result["budget_exhausted"]
    assert result["completion_seconds"] is None
    assert result["noncompletion_reason"] == "lease_end_at_horizon"


def test_strict_first_checkpoint_failure_retains_failed_job_in_horizon_metric():
    spec = task()
    result = study.execute_task(spec, device(), actual(spec, checkpoint_fails=True),
                                calendar([5., 100.]), horizon=100., allow_cold_restart=False)
    assert result["terminal_failed"]
    assert not result["completed"] and not result["censored"]
    assert result["restricted_completion_seconds"] == 100.
    assert result["compute_waste_seconds"] == 5.


def test_actual_notice_is_public_at_recall_and_late_stop_is_not_hidden():
    spec = task(checkpoint=8.)
    result = study.execute_task(spec, device(), actual(spec), calendar([5., 100.], notice=.5, stop=1.),
                                horizon=100., max_recoveries=0)
    assert result["episodes"][0]["action"] == "stop"
    assert result["deadline_misses"] == 1
    assert result["max_return_lateness_seconds"] == pytest.approx(.5)


def test_calendar_exhaustion_is_an_error_instead_of_infinite_stable_supply():
    spec = task(runtime=100., checkpoint=8.)
    with pytest.raises(ValueError, match="calendar_exhausted"):
        study.execute_task(spec, device(), actual(spec), calendar([5.]), horizon=100., max_recoveries=None)


def test_freeze_copies_exact_old_inputs_and_refuses_reuse(tmp_path):
    output = tmp_path / "sealed"
    protocol_path = study.ROOT / "experiments/research/renewal_protocol.json"
    study.freeze(protocol_path, output)
    protocol = json.loads(protocol_path.read_text())
    assert study.sha256(output / "source-inputs.json") == protocol["source_inputs_sha256"]
    assert not (output / "RUN_STARTED").exists()
    assert len(json.loads((output / "inputs.json").read_text())["cases"]) == 180
    with pytest.raises(FileExistsError):
        study.freeze(protocol_path, output)


def test_tampered_calendar_is_rejected_before_execution(tmp_path):
    output = tmp_path / "sealed"
    study.freeze(study.ROOT / "experiments/research/renewal_protocol.json", output)
    (output / "inputs.json").write_text("{}")
    with pytest.raises(ValueError, match="Frozen artifact changed"):
        study.run_frozen(output)
    assert not (output / "RUN_STARTED").exists()
