"""Operational reads stay bounded without hiding history or changing authority."""

import copy

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain import kernel as kernel_module
from campus_fabric.domain.errors import Rejected
from campus_fabric.domain.kernel import Kernel
from campus_fabric.domain.state_views import touched_values, values_where
from campus_fabric.projections.read_models import legal_actions_json
from campus_fabric.workflows.devhost import WorkflowWorker
from campus_fabric.workflows.orchestrator import WorkflowOrchestrator

ALICE = m.AuthenticatedActor(actor_id="alice", role="member", owner_id="alice")
BOB = m.AuthenticatedActor(actor_id="bob", role="member", owner_id="bob")
ADMIN = m.AuthenticatedActor(actor_id="admin", role="administrator")
TEST = m.AuthenticatedActor(actor_id="clock", role="test_driver")


def registration(ident="work", mode="logical_simulation"):
    return m.WorkIntentRegistration(
        intent_id=ident, owner_id="alice", execution_mode=mode,
        settlement_action="keep_lease",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(
                kind="allocation.create", owner_id="alice", ttl_seconds=30,
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                          gpu=m.GPURequest(count=0)),
            ),
            job=m.JobSpecification(image="reference", command=("run",)),
        ),
    )


def clock(kernel, seconds=1):
    return kernel.commit(m.CommandEnvelope(
        request_id="tick", actor=TEST, operation="clock.advance",
        parameters=m.ClockAdvanceParameters(
            kind="clock.advance", advance_seconds=float(seconds), source="test_control",
        ),
    ))


def add_history(kernel, count=80):
    with kernel.store.transaction() as conn:
        state = kernel.store.load(conn)
        for index in range(count):
            source = registration(f"old-{index}")
            old = m.WorkIntent(
                **source.model_dump(), first_arrival_at=0.0, state="cancelled",
                result=m.WorkResult(kind="cancelled", recorded_at=0.0),
            )
            state["intents"][old.intent_id] = old.model_dump(mode="json")
        kernel.store.save(conn, state)


def test_clock_does_not_revalidate_untouched_terminal_history(tmp_path, monkeypatch):
    kernel = Kernel(tmp_path / "history.sqlite")
    add_history(kernel)
    original = kernel_module.parse

    def reject_historical_validation(model, raw):
        if model is m.WorkIntent and raw["intent_id"].startswith("old-"):
            pytest.fail("A clock-only commit loaded terminal work history")
        return original(model, raw)

    with monkeypatch.context() as patch:
        patch.setattr(kernel_module, "parse", reject_historical_validation)
        assert clock(kernel).outcome == "committed"
    archived = kernel.read_snapshot(m.ReadQuery(actor=ALICE))
    assert len(archived.intents) == 80
    assert all(intent.state == "cancelled" for intent in archived.intents)


def test_rejection_discards_nested_changes_and_uses_original_clock_version(tmp_path, monkeypatch):
    kernel = Kernel(tmp_path / "reject.sqlite")
    kernel.register_current_intent(registration(), actor=ALICE, request_id="register")
    before = kernel.store.read()

    def reject_after_mutation(state, command):
        state["now"] = 99.0
        state["intents"]["work"]["state"] = "running"
        state["events"].append({"uncommitted": True})
        raise Rejected("lease_unavailable", "Injected after local mutation")

    monkeypatch.setattr(kernel, "_apply", reject_after_mutation)
    receipt = clock(kernel)
    assert receipt.outcome == "rejected"
    assert receipt.logical_time == before["now"]
    assert receipt.version == receipt.error.current_version == m.VersionStamp(
        epoch=before["epoch"], revision=before["revision"],
    )
    assert kernel.store.read() == before


def test_private_work_views_keep_owner_scope_and_do_not_call_full_state_read(tmp_path, monkeypatch):
    kernel = Kernel(tmp_path / "private-read.sqlite")
    add_history(kernel)
    kernel.register_current_intent(registration(), actor=ALICE, request_id="register")

    def no_full_read():
        pytest.fail("Operational read reconstructed the entire state")

    monkeypatch.setattr(kernel.store, "read", no_full_read)
    own = kernel.work_snapshot("work", ALICE)
    assert [intent.intent_id for intent in own.intents] == ["work"]
    assert own.viewer == ALICE and not own.global_scope
    assert not own.events and not own.decisions
    assert not kernel.work_snapshot("work", BOB).intents
    assert [intent.intent_id for intent in kernel.workflow_pending_snapshot(ADMIN).intents] == ["work"]
    with pytest.raises(PermissionError):
        kernel.workflow_pending_snapshot(ALICE)
    assert kernel.runtime_metadata()["revision"] == own.version.revision
    assert kernel.pending_dispatches()[1] == ()
    assert kernel.decision_snapshot().version == own.version
    assert kernel.inventory_state(ALICE) == {**kernel.runtime_metadata(), "nodes": {}}
    # DTOs remain usable after the underlying read connection has closed.
    assert m.ReadSnapshot.model_validate_json(own.model_dump_json()) == own


def test_worker_and_orchestrator_use_private_reads_but_history_page_stays_complete(
    tmp_path, monkeypatch,
):
    kernel = Kernel(tmp_path / "worker.sqlite")
    add_history(kernel)
    kernel.register_current_intent(registration(), actor=ALICE, request_id="register")
    original_read = kernel.read_snapshot

    def no_full_snapshot(query):
        pytest.fail("Workflow reconciliation requested a full historical snapshot")

    monkeypatch.setattr(kernel, "read_snapshot", no_full_snapshot)
    worker = WorkflowWorker(kernel)
    worker.tick()
    own = kernel.work_snapshot("work", ALICE)
    assert len(own.allocations) == 1 and own.allocations[0].state == "Pending"
    assert not own.jobs
    assert len(original_read(m.ReadQuery(actor=ALICE)).intents) == 81

    class LegacyReader:
        def read_snapshot(self, query):
            return original_read(query)

    _, intent = WorkflowOrchestrator(kernel, LegacyReader())._read("work", ALICE)
    assert intent.intent_id == "work"


def test_delivered_dispatch_still_deduplicates_without_scanning_other_attempts(tmp_path):
    kernel = Kernel(tmp_path / "dispatch.sqlite")
    attempt = m.AttemptRef(intent_id="intent", attempt_id="e0-attempt1", generation=1,
                          allocation_id="e0-a1", job_id="e0-j1")
    command = m.ExecutorCommand(dispatch_id="e0-dispatch2", version=m.VersionStamp(epoch=0, revision=1),
                                attempt=attempt, action="stop", mode="cpu_reference")
    entry = {"command": command.model_dump(mode="json"), "status": "delivered",
             "acknowledged_at": 1.0}

    class IndexedOutbox(dict):
        def values(self):
            pytest.fail("Dispatch lookup scanned the historical outbox")

        def values_where(self, **filters):
            assert filters == {"attempt_id": "e0-attempt1", "action": "stop"}
            return [self[command.dispatch_id]]

    state = {"outbox": IndexedOutbox({command.dispatch_id: entry})}
    assert kernel._dispatch(state, {"attempt": attempt.model_dump(mode="json")}, "stop") == command.dispatch_id
    assert len(state["outbox"]) == 1 and entry["status"] == "delivered"


@pytest.mark.parametrize("mode", ["logical_simulation", "cpu_reference"])
@pytest.mark.parametrize("kind", ["allocation", "job"])
def test_record_detail_keeps_terminal_lease_and_stopping_generation_hints(
    tmp_path, monkeypatch, mode, kind,
):
    capacity = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192)
    node = m.NodeInventory(node_id="node", contributor_owner_id="supplier", state="Ready",
                           capacity=capacity, available=capacity, observed_at=0.0)
    kernel = Kernel(tmp_path / "detail.sqlite", inventory=(node,), execution_mode=mode)
    result = WorkflowOrchestrator(kernel, kernel).start(
        registration(mode=mode), kernel.decision_snapshot().version,
        actor=ALICE, request_id="start",
    )
    assert result.state == "running"
    current = kernel.work_snapshot("work", ALICE)
    job_id, allocation_id = current.jobs[0].job_id, current.allocations[0].allocation_id
    if mode == "cpu_reference":
        receipt = kernel.commit(m.CommandEnvelope(
            request_id="cancel", actor=ALICE, operation="job.cancel",
            parameters=m.JobCancelParameters(kind="job.cancel", job_id=job_id),
        ))
        assert receipt.outcome == "committed"
    else:
        clock(kernel, 10)
    complete = kernel.read_snapshot(m.ReadQuery(actor=ALICE))
    ident = allocation_id if kind == "allocation" else job_id
    related = {(kind, ident)}
    expected = legal_actions_json(complete, related, include_cp1=True)

    def no_full_snapshot(query):
        pytest.fail("Record detail reconstructed complete owner history")

    monkeypatch.setattr(kernel, "read_snapshot", no_full_snapshot)
    query = m.ReadQuery(actor=ALICE, entities=(m.EntityRef(kind=kind, entity_id=ident),))
    selected = kernel.record_snapshot(query)
    assert legal_actions_json(selected, related, include_cp1=True) == expected
    assert not selected.events and not selected.decisions
    assert selected.version == complete.version
    hidden = kernel.record_snapshot(query.model_copy(update={"actor": BOB}))
    assert not hidden.allocations and not hidden.jobs and not hidden.intents
    if mode == "logical_simulation":
        assert selected.allocations[0].state == "Allocated"
        if kind == "job":
            assert selected.jobs[0].state == "Succeeded"
    else:
        assert selected.intents[0].attempts[-1].state == "stopping"


def test_plain_snapshot_helpers_preserve_selection_and_conservative_validation():
    records = {
        "one": {"state": "Pending", "owner_id": "alice", "unavailable_until": 2.0},
        "two": {"state": "Preempted", "owner_id": "alice", "unavailable_until": 3.0},
        "three": {"state": "Released", "owner_id": "bob", "unavailable_until": 4.0},
    }
    before = copy.deepcopy(records)
    assert list(values_where(records, state=("Pending", "Preempted"), owner_id="alice")) == [
        records["one"], records["two"],
    ]
    assert list(values_where(records, unavailable_until__gt=3.0)) == [records["three"]]
    assert list(touched_values(records)) == list(records.values())
    assert records == before
