"""Reviewed synthetic exports exercise the live DTO adapter, not field accuracy."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.policies.learned import LearnedAvailabilityPolicy
from campus_fabric.workflows.availability_inputs import (
    AvailabilityInputProducer,
    execution_limits,
    specification_sha256,
)
from tests.policy.test_learned import artifact
from tests.policy.test_reliability_kernel import setup_kernel

ADMIN = m.AuthenticatedActor(actor_id="input-auditor", role="administrator")


def inputs(tmp_path):
    kernel = setup_kernel(tmp_path)
    decision = kernel.decision_snapshot()
    read = kernel.read_snapshot(m.ReadQuery(actor=ADMIN, global_scope=True))
    model_path, model_sha = artifact(tmp_path)
    registry = {
        "schema_version": 1, "namespace": "reviewed-local-test", "epoch": 0,
        "execution_mode": "logical_simulation", "calendar_origin": "logical_day0_monday",
        "utc_offset_hours": 0., "model_sha256": model_sha,
        "devices": {node.devices[0].physical_gpu_id: {
            "contributor_owner_id": node.contributor_owner_id,
            "completed_windows": [{"started_at": end - duration, "ended_at": float(end),
                "available_at": float(end), "duration_seconds": duration}
                for end in (200, 400, 600, 800)],
            "current_window_started_at": decision.now, "observed_at": decision.now,
            "available_at": decision.now, "notice_seconds": 12., "owner_use_seconds": 10.,
        } for node, duration in zip(decision.inventory, (160., 16.), strict=True)},
        "tasks": {intent.intent_id: {
            "owner_id": intent.owner_id,
            "specification_sha256": specification_sha256(intent.specification),
            "observed_at": decision.now, "available_at": decision.now,
            "estimated_compute_seconds": 24. if intent.intent_id == "work-a" else 110.,
            "checkpoint_seconds": .5 if intent.intent_id == "work-a" else 8.,
        } for intent in read.intents},
    }
    return decision, read, registry, model_path


def producer(tmp_path, registry, **overrides):
    path = tmp_path / "reviewed-inputs.json"
    path.write_text(json.dumps(registry, allow_nan=False), encoding="utf-8")
    options = {"expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "model_sha256": registry["model_sha256"], "namespace": registry["namespace"]}
    return AvailabilityInputProducer(path, **{**options, **overrides})


def test_enrichment_uses_real_read_bindings_preserves_snapshot_and_reaches_learned_policy(tmp_path):
    decision, read, registry, model_path = inputs(tmp_path)
    before = decision.model_dump_json(), read.model_dump_json()
    adapter = producer(tmp_path, registry)
    enriched = adapter.enrich(decision, read)
    assert enriched is not decision, adapter.last_diagnostics
    assert decision.model_dump_json() == before[0] and read.model_dump_json() == before[1]
    assert enriched.model_dump(exclude={"demands", "history", "metadata"}) == decision.model_dump(
        exclude={"demands", "history", "metadata"})
    for demand, original in zip(enriched.demands, decision.demands, strict=True):
        assert demand.model_dump(exclude={"known_data"}) == original.model_dump(exclude={"known_data"})
        values = {datum.name: datum.value for datum in demand.known_data}
        assert values["remaining_recoveries"] == 2
        assert values["execution_window_seconds"] == 900
        assert values["estimated_compute_seconds"] in {24., 110.}  # Not lease TTL.
    assert len(enriched.history) == 8
    assert {row.observed_at for row in enriched.history} == {200., 400., 600., 800.}
    assert adapter.enrich(enriched, read) == enriched
    plan = LearnedAvailabilityPolicy(model_path, expected_sha256=registry["model_sha256"]).plan(enriched)
    assert "inactive" not in plan.rationale
    assert plan.snapshot_version == decision.version
    assert "operator-reviewed external export" in adapter.last_diagnostics[0]


@pytest.mark.parametrize("change,reason", [
    (lambda r: r.update(epoch=1), "epoch"),
    (lambda r: r.update(execution_mode="cpu_reference"), "execution mode"),
    (lambda r: r.update(calendar_origin="2026-01-01T00:00:00Z"), "schema"),
    (lambda r: r.update(utc_offset_hours=True), "schema"),
    (lambda r: r.update(schema_version=True), "integer 1"),
    (lambda r: r["tasks"]["work-a"].update(owner_id="bob"), "task owner"),
    (lambda r: r["tasks"]["work-a"].update(specification_sha256="0" * 64), "SHA256"),
    (lambda r: r["tasks"].pop("work-a"), "missing explicitly"),
    (lambda r: r["tasks"]["work-a"].update(available_at=1001.), "cutoff"),
    (lambda r: r["tasks"]["work-a"].update(estimated_compute_seconds=True), "schema"),
    (lambda r: r["devices"]["gpu-a"].update(contributor_owner_id="intruder"), "ownership"),
    (lambda r: r["devices"]["gpu-a"].update(current_window_started_at=999.), "not newly"),
    (lambda r: r["devices"]["gpu-a"].update(current_window_started_at=1001.), "starts after"),
    (lambda r: r["devices"]["gpu-a"].update(observed_at=999.), "starts after"),
    (lambda r: r["devices"]["gpu-a"].update(available_at=1001.), "cutoff"),
    (lambda r: r["devices"]["gpu-a"]["completed_windows"][0].update(duration_seconds=159.),
     "end minus start"),
    (lambda r: r["devices"]["gpu-a"]["completed_windows"][1].update(
        started_at=199., duration_seconds=201.), "overlapping"),
    (lambda r: r["devices"]["gpu-a"]["completed_windows"][0].update(available_at=1001.),
     "unavailable"),
    (lambda r: r["devices"]["gpu-a"]["completed_windows"].pop(), "schema"),
    (lambda r: r["devices"]["gpu-a"].update(hidden_next_recall=1100.), "schema"),
])
def test_unreviewable_or_out_of_scope_inputs_keep_original_snapshot(tmp_path, change, reason):
    decision, read, registry, _ = inputs(tmp_path)
    change(registry)
    adapter = producer(tmp_path, registry)
    assert adapter.enrich(decision, read) is decision
    assert reason in adapter.last_diagnostics[0]


@pytest.mark.parametrize("options,reason", [
    ({"expected_sha256": "0" * 64}, "reviewed pin"),
    ({"model_sha256": "0" * 64}, "binding differs"),
    ({"namespace": "another-instance"}, "binding differs"),
    ({"expected_sha256": ""}, "pins"),
])
def test_registry_pins_bind_bytes_instance_and_model(tmp_path, options, reason):
    decision, read, registry, _ = inputs(tmp_path)
    adapter = producer(tmp_path, registry, **options)
    assert adapter.enrich(decision, read) is decision
    assert reason in adapter.last_diagnostics[0]


def test_missing_file_or_duplicate_json_keys_fail_closed(tmp_path):
    decision, read, registry, _ = inputs(tmp_path)
    path = tmp_path / "missing.json"
    adapter = AvailabilityInputProducer(path, expected_sha256="0" * 64,
        model_sha256=registry["model_sha256"], namespace=registry["namespace"])
    assert adapter.enrich(decision, read) is decision
    raw = json.dumps(registry)[:-1] + ',"epoch":0}'
    path.write_text(raw)
    adapter = AvailabilityInputProducer(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        model_sha256=registry["model_sha256"], namespace=registry["namespace"])
    assert adapter.enrich(decision, read) is decision
    assert "duplicate" in adapter.last_diagnostics[0]


def test_separate_snapshots_and_non_global_reads_cannot_supply_profiles(tmp_path):
    decision, read, registry, _ = inputs(tmp_path)
    adapter = producer(tmp_path, registry)
    for other in (
        read.model_copy(update={"version": m.VersionStamp(epoch=0, revision=read.version.revision + 1)}),
        read.model_copy(update={"now": read.now + 1}),
        read.model_copy(update={"global_scope": False,
            "viewer": m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="researcher")}),
        read.model_copy(update={"intents": ()}),
    ):
        assert adapter.enrich(decision, other) is decision
        assert "inactive" in adapter.last_diagnostics[0]


def test_existing_facts_are_never_overwritten_or_freshened(tmp_path):
    decision, read, registry, _ = inputs(tmp_path)
    adapter = producer(tmp_path, registry)
    enriched = adapter.enrich(decision, read)
    first = enriched.demands[0]
    wrong = first.known_data[0].model_copy(update={"value": 999.})
    conflict = decision.model_copy(update={"demands": (
        decision.demands[0].model_copy(update={"known_data": (wrong,)}), *decision.demands[1:])})
    assert adapter.enrich(conflict, read) is conflict
    assert "conflicting existing" in adapter.last_diagnostics[0]
    conflict = decision.model_copy(update={"metadata": (
        enriched.metadata[0].model_copy(update={"observed_at": 999., "available_at": 999.}),)})
    assert adapter.enrich(conflict, read) is conflict
    assert "conflicting existing" in adapter.last_diagnostics[0]


def test_profile_observation_expiry_and_short_lease_do_not_change_compute_estimate(tmp_path):
    decision, read, registry, _ = inputs(tmp_path)
    now = 90000.
    later = decision.model_copy(update={"now": now})
    later_read = read.model_copy(update={"now": now})
    adapter = producer(tmp_path, registry)
    assert adapter.enrich(later, later_read) is later
    assert "stale" in adapter.last_diagnostics[0]
    for device in registry["devices"].values():
        device.update(observed_at=now, available_at=now, current_window_started_at=now)
    for task in registry["tasks"].values():
        task.update(observed_at=now, available_at=now)
    adapter = producer(tmp_path, registry)
    assert adapter.enrich(later, later_read) is later
    assert "stale" in adapter.last_diagnostics[0]  # The historical ends were not refreshed.

    intent = read.intents[0]
    spec = intent.specification.model_copy(update={"allocation":
        intent.specification.allocation.model_copy(update={"ttl_seconds": 300})})
    changed = intent.model_copy(update={"specification": spec})
    registry["tasks"][intent.intent_id].update(specification_sha256=specification_sha256(spec),
        observed_at=1000., available_at=1000.)
    adapter = producer(tmp_path, registry)
    short_read = read.model_copy(update={"intents": (changed, *read.intents[1:])})
    assert adapter.enrich(decision, short_read) is decision
    assert "horizon >=900" in adapter.last_diagnostics[0]


def test_active_recall_and_owner_mismatch_never_relabel_a_window_as_open(tmp_path):
    decision, read, registry, _ = inputs(tmp_path)
    adapter = producer(tmp_path, registry)
    recalled = decision.model_copy(update={"notices": (m.ContributorNotice(
        notice_id="notice-a", contributor_owner_id=decision.inventory[0].contributor_owner_id,
        device_ids=("gpu-a",), issued_at=1000., deadline=1012., unavailable_until=1022.),)})
    assert adapter.enrich(recalled, read) is recalled
    assert "active authoritative recall" in adapter.last_diagnostics[0]
    wrong = decision.model_copy(update={"demands": (decision.demands[0].model_copy(
        update={"owner_id": "bob"}), *decision.demands[1:])})
    assert adapter.enrich(wrong, read) is wrong
    assert "ownership" in adapter.last_diagnostics[0]


def test_resume_limits_preserve_original_expiry_timeout_and_charge_pending_restart(tmp_path):
    decision, read, registry, _ = inputs(tmp_path)
    intent, allocation = read.intents[0], read.allocations[0]
    intent = intent.model_copy(update={"job_ids": ("job-a",)})
    job = m.JobView(job_id="job-a", allocation_id=allocation.allocation_id,
                   owner_id=intent.owner_id, state="Preempted", created_at=1000.)
    resumed = allocation.model_copy(update={"expires_at": 1800., "restart_count": 0})
    # Original timeout=900 from job creation, old expiry=1800; no reset on resume.
    assert execution_limits(intent, resumed, (job,), 1100.) == (1, 700.)
    assert execution_limits(intent, resumed.model_copy(update={"restart_count": 1}),
                            (job,), 1100.) == (0, 700.)
    with pytest.raises(ValueError, match="budget"):
        execution_limits(intent, resumed.model_copy(update={"restart_count": 2}), (job,), 1100.)
    no_job = intent.model_copy(update={"job_ids": ()})
    assert execution_limits(no_job, resumed, (), 1100.) == (2, 700.)
    adapter = producer(tmp_path, registry)
    other = read.model_copy(update={"allocations": (resumed, *read.allocations[1:]),
                                    "intents": (intent, *read.intents[1:]), "jobs": (job,)})
    assert adapter.enrich(decision, other) is decision
    assert "remaining-compute profile" in adapter.last_diagnostics[0]


def test_loaded_export_is_fixed_and_no_extra_registry_entry_enters_snapshot(tmp_path):
    decision, read, registry, _ = inputs(tmp_path)
    registry["tasks"]["not-yet-submitted"] = copy.deepcopy(registry["tasks"]["work-a"])
    registry["tasks"]["not-yet-submitted"]["observed_at"] = 90000.
    registry["tasks"]["not-yet-submitted"]["available_at"] = 90000.
    adapter = producer(tmp_path, registry)
    first = adapter.enrich(decision, read)
    assert first is not decision, adapter.last_diagnostics
    (tmp_path / "reviewed-inputs.json").write_text("unreviewed replacement")
    assert adapter.enrich(decision, read) == first
    assert "not-yet-submitted" not in first.model_dump_json()
