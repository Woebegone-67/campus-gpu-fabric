"""Port-double tests only: no production ledger or durable backend is provided here."""

from __future__ import annotations

from dataclasses import replace

import pytest

from campus_fabric.contracts.models import (
    AllocationCreateParameters,
    AllocationView,
    AuthenticatedActor,
    CommitReceipt,
    EntityRef,
    ErrorDetail,
    GPURequest,
    JobSpecification,
    JobView,
    ReadSnapshot,
    ResourceRequest,
    StageReceipt,
    VersionStamp,
    WorkIntent,
    WorkIntentRegistration,
    WorkResult,
    WorkSpecification,
)
from campus_fabric.workflows import WorkflowOrchestrator, WorkflowReadError

ACTOR = AuthenticatedActor(actor_id="actor-a", owner_id="owner-a", role="researcher")
RESOURCES = ResourceRequest(cpu_millicores=1000, memory_mib=1024, gpu=GPURequest(count=1))
REGISTRATION = WorkIntentRegistration(
    intent_id="work-a", owner_id="owner-a", execution_mode="logical_simulation",
    settlement_action="keep_lease",
    specification=WorkSpecification(
        allocation=AllocationCreateParameters(kind="allocation.create", owner_id="owner-a",
                                               request=RESOURCES),
        job=JobSpecification(image="example:local", command=("task",)),
    ),
)


def changed(model, **updates):
    """Revalidate every fixture update, as required by the shared DTO contract."""
    return type(model).model_validate({**model.model_dump(), **updates})


class PortDouble:
    """An isolated deterministic double, not a substitute for A's persistent kernel."""

    def __init__(self):
        self.version = VersionStamp(epoch=1, revision=0)
        self.intent = None
        self.allocations = ()
        self.jobs = ()
        self.registration_receipt = None
        self.calls = []
        self.queued = False
        self.fail_job = False
        self.stale_allocation = False
        self.lose_job_response = False
        self.lose_allocation_response = False

    def committed(self, request_id, *objects):
        self.version = VersionStamp(epoch=self.version.epoch, revision=self.version.revision + 1)
        return CommitReceipt(request_id=request_id, outcome="committed", version=self.version,
            logical_time=10.0, objects=objects, event_ids=(f"event-{self.version.revision}",))

    def rejected(self, request_id, code="lease_unavailable"):
        return CommitReceipt(request_id=request_id, outcome="rejected", version=self.version,
            logical_time=10.0, error=ErrorDetail(code=code, detail="Injected port rejection"))

    def read_snapshot(self, query):
        return ReadSnapshot(version=self.version, now=10.0, viewer=query.actor,
            intents=(self.intent,) if self.intent else (), allocations=self.allocations,
            jobs=self.jobs)

    def register_intent(self, registration, expected_version, *, actor, request_id):
        self.calls.append(("register", registration.intent_id))
        if self.intent is None:
            self.intent = WorkIntent(
                intent_id=registration.intent_id, owner_id=registration.owner_id,
                first_arrival_at=10.0, execution_mode=registration.execution_mode,
                settlement_action=registration.settlement_action,
                specification=registration.specification, state="pending",
            )
            self.registration_receipt = self.committed(
                request_id, EntityRef(kind="intent", entity_id=registration.intent_id))
        return self.registration_receipt

    def create_allocation_and_bind(self, intent_id, stage_id, expected_version, *, actor, request_id):
        self.calls.append(("allocate", stage_id))
        if self.stale_allocation:
            return self.rejected(request_id, "stale_version")
        receipt = self.committed(request_id, EntityRef(kind="allocation", entity_id="lease-a"))
        self.allocations = (AllocationView(allocation_id="lease-a", owner_id="owner-a",
            state="Pending" if self.queued else "Allocated", request=RESOURCES,
            created_at=10.0, expires_at=None if self.queued else 100.0),)
        self.intent = changed(self.intent, allocation_ids=("lease-a",),
            stages=(*self.intent.stages, StageReceipt(stage_id=stage_id, operation="allocate",
                                                     state="committed", receipt=receipt)))
        if self.lose_allocation_response:
            raise TimeoutError("Committed before response loss")
        return receipt

    def create_job_and_bind(self, intent_id, stage_id, expected_version, *, actor, request_id):
        self.calls.append(("job", stage_id))
        if self.fail_job:
            receipt = self.rejected(request_id)
            self.intent = changed(self.intent, state="manual_action",
                stages=(*self.intent.stages, StageReceipt(stage_id=stage_id,
                    operation="create_job_and_bind", state="failed", receipt=receipt)))
            return receipt
        receipt = self.committed(request_id, EntityRef(kind="job", entity_id="job-a"))
        self.jobs = (JobView(job_id="job-a", allocation_id="lease-a", owner_id="owner-a",
                             state="Pending", created_at=10.0),)
        self.intent = changed(self.intent, state="waiting", job_ids=("job-a",),
            stages=(*self.intent.stages, StageReceipt(stage_id=stage_id,
                operation="create_job_and_bind", state="committed", receipt=receipt)))
        if self.lose_job_response:
            raise TimeoutError("Committed before response loss")
        return receipt


def start(kernel):
    return WorkflowOrchestrator(kernel, kernel).start(
        REGISTRATION, kernel.version, actor=ACTOR, request_id="register-a")


def test_normal_flow_binds_once_and_does_not_claim_pending_job_is_running():
    kernel = PortDouble()
    result = start(kernel)
    assert kernel.calls == [("register", "work-a"), ("allocate", "allocate"), ("job", "create-job")]
    assert result.state == "waiting"
    assert result.intent.job_ids == ("job-a",)
    assert result.allocation_id == "lease-a"
    assert len(result.receipts) == 3
    assert result.to_dict()["intent"]["first_arrival_at"] == 10.0
    WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)
    assert len(kernel.calls) == 3


def test_pending_lease_waits_without_job_submission_or_reallocation():
    kernel = PortDouble()
    kernel.queued = True
    result = start(kernel)
    assert result.state == "waiting"
    assert not kernel.jobs
    WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)
    assert kernel.calls == [("register", "work-a"), ("allocate", "allocate")]
    kernel.allocations = (changed(kernel.allocations[0], state="Allocated", expires_at=100.0),)
    WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)
    assert kernel.calls[-1] == ("job", "create-job")


def test_job_failure_preserves_lease_and_requires_deliberate_new_stage():
    kernel = PortDouble()
    kernel.fail_job = True
    result = start(kernel)
    assert result.state == "partial_success"
    assert result.allocation_id == "lease-a"
    result = WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)
    assert result.state == "partial_success"
    assert len(kernel.calls) == 3
    kernel.fail_job = False
    result = WorkflowOrchestrator(kernel, kernel).advance(
        "work-a", actor=ACTOR, job_stage_id="job-retry-1")
    assert result.intent.job_ids == ("job-a",)
    assert [c[0] for c in kernel.calls].count("allocate") == 1
    assert kernel.calls[-1] == ("job", "job-retry-1")


@pytest.mark.parametrize("lost_stage", ["job", "allocation"])
def test_commit_before_response_loss_is_recovered_from_authoritative_read(lost_stage):
    kernel = PortDouble()
    setattr(kernel, f"lose_{lost_stage}_response", True)
    result = start(kernel)
    assert result.state == "unknown"
    # Simulate replacement of the orchestrator after restart: no local memory is copied.
    result = WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)
    assert result.intent.job_ids == ("job-a",)
    assert [c[0] for c in kernel.calls].count("allocate") == 1
    assert [c[0] for c in kernel.calls].count("job") == 1


def test_stale_version_returns_without_retry_or_job_submission():
    kernel = PortDouble()
    kernel.stale_allocation = True
    result = start(kernel)
    assert result.state == "blocked"
    assert result.receipts[-1].error.code == "stale_version"
    assert not kernel.allocations and not kernel.jobs
    assert len(kernel.calls) == 2


@pytest.mark.parametrize("state", ["PreemptRequested", "Preempted", "Running"])
def test_unavailable_lease_does_not_accept_a_new_job(state):
    kernel = PortDouble()
    kernel.queued = True
    start(kernel)
    kernel.allocations = (changed(kernel.allocations[0], state=state),)
    result = WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)
    assert result.state == "blocked"
    assert not kernel.jobs


def test_exact_expiry_blocks_job_creation():
    kernel = PortDouble()
    kernel.queued = True
    start(kernel)
    kernel.allocations = (changed(kernel.allocations[0], state="Allocated", expires_at=10.0),)
    assert WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR).state == "blocked"
    assert not kernel.jobs


def test_owner_mismatch_and_missing_linked_objects_fail_closed():
    kernel = PortDouble()
    with pytest.raises(PermissionError):
        WorkflowOrchestrator(kernel, kernel).start(REGISTRATION, kernel.version,
            actor=AuthenticatedActor(actor_id="other", owner_id="owner-b", role="researcher"),
            request_id="forbidden")
    assert not kernel.calls
    kernel.queued = True
    start(kernel)
    kernel.allocations = ()
    with pytest.raises(WorkflowReadError, match="omitted"):
        WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)


def test_auto_settlement_is_explicitly_not_faked_without_a_durable_port():
    kernel = PortDouble()
    start(kernel)
    kernel.intent = changed(kernel.intent, state="completed", settlement_action="release_on_success",
        result=WorkResult(kind="success", recorded_at=10.0))
    result = WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR)
    assert result.state == "terminal"
    assert "settlement is kernel-owned" in result.reason
    assert kernel.allocations[0].state == "Allocated"
    assert len(kernel.calls) == 3


def test_progress_is_frozen_and_stages_cannot_alias():
    kernel = PortDouble()
    result = start(kernel)
    assert replace(result, reason="new explanation").intent is result.intent
    with pytest.raises(ValueError):
        WorkflowOrchestrator(kernel, kernel).advance("work-a", actor=ACTOR,
            allocation_stage_id="same", job_stage_id="same")


def test_discovered_work_item_cannot_cross_reset_into_reused_intent_id():
    kernel = PortDouble()
    kernel.register_intent(REGISTRATION, kernel.version, actor=ACTOR, request_id="register")
    discovered_epoch = kernel.version.epoch
    # An administrative reset followed by an identically named new work intent.
    kernel.version = VersionStamp(epoch=discovered_epoch + 1, revision=1)
    result = WorkflowOrchestrator(kernel, kernel).advance(
        REGISTRATION.intent_id, actor=ACTOR, expected_epoch=discovered_epoch)
    assert result.state == "blocked"
    assert "Epoch changed" in result.reason
    assert not kernel.allocations and not kernel.jobs
    assert len(kernel.calls) == 1


def test_start_does_not_advance_a_registration_receipt_from_previous_epoch():
    class ResetAfterRegister(PortDouble):
        def register_intent(self, *args, **kwargs):
            receipt = super().register_intent(*args, **kwargs)
            self.version = VersionStamp(epoch=receipt.version.epoch + 1, revision=1)
            return receipt

    kernel = ResetAfterRegister()
    result = start(kernel)
    assert result.state == "blocked"
    assert len(result.receipts) == 1
    assert not kernel.allocations and not kernel.jobs
    assert len(kernel.calls) == 1
