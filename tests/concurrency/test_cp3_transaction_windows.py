"""Kill a real writer on either side of COMMIT before the caller sees a reply.

The barriers wrap SQLiteStore only inside the disposable child. No fault endpoint
or production branch is added. A separate connection observes visibility while
the child is alive, then a new Kernel recovers the same on-disk database.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
from contextlib import contextmanager

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.storage.state import materialize

ALICE = m.AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice")
OPERATIONS = ("allocation", "workflow_allocation", "workflow_job")
WINDOWS = ("before_commit", "after_commit")


def inventory():
    capacity = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                                ephemeral_storage_mib=1024, gpu_count=1,
                                gpu_memory_mib=8192)
    return (m.NodeInventory(
        node_id="n", contributor_owner_id="supplier", state="Ready",
        capacity=capacity, available=capacity, observed_at=0,
        devices=(m.GPUDevice(device_id="g", physical_gpu_id="g", model="A10",
                             memory_mib=8192, state="available"),),
    ),)


def allocation():
    return m.AllocationCreateParameters(
        kind="allocation.create", owner_id="alice", ttl_seconds=300,
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                  ephemeral_storage_mib=32,
                                  gpu=m.GPURequest(count=1, model="A10", memory_mib=4096)),
    )


def prepare(path, operation):
    kernel = Kernel(path, inventory=inventory())
    if operation != "allocation":
        registration = m.WorkIntentRegistration(
            intent_id="crash-intent", owner_id="alice", execution_mode="logical_simulation",
            settlement_action="keep_lease",
            specification=m.WorkSpecification(
                allocation=allocation(), job=m.JobSpecification(
                    image="cp3-simulation", command=("work",), timeout_seconds=100,
                    max_retries=0)),
        )
        registered = kernel.register_intent(
            registration, kernel.decision_snapshot().version, actor=ALICE, request_id="register")
        assert registered.outcome != "rejected"
        if operation == "workflow_job":
            allocated = kernel.create_allocation_and_bind(
                "crash-intent", "preallocate", registered.version,
                actor=ALICE, request_id="preallocate")
            assert allocated.outcome != "rejected"
    return kernel


def envelope(operation, version):
    if operation == "allocation":
        parameters = allocation()
        key = "durable-create"
    else:
        suffix = "create_job_and_bind" if operation == "workflow_job" else "create_allocation_and_bind"
        parameters = m.WorkflowStageParameters(
            kind="workflow." + suffix, intent_id="crash-intent", stage_id="durable-stage")
        key = None
    return m.CommandEnvelope(request_id="lost-response", actor=ALICE,
                             operation=parameters.kind, parameters=parameters,
                             idempotency_key=key, expected_version=version)


def killed_writer(path, command, window, pipe):
    kernel = Kernel(path)
    original = kernel.store.transaction

    @contextmanager
    def intercepted():
        with original() as conn:
            yield conn
            # Detach the complete observation while this transaction's lazy
            # containers can still read their connection; never pickle a cursor.
            provisional = materialize(kernel.store.load(conn))
            if window == "before_commit":
                pipe.send(("barrier", provisional))
                pipe.recv()
        if window == "after_commit":
            pipe.send(("barrier", provisional))
            pipe.recv()

    kernel.store.transaction = intercepted
    receipt, payload = kernel.commit_with_payload(
        m.CommandEnvelope.model_validate_json(json.dumps(command)))
    pipe.send(("returned", receipt.model_dump(mode="json"), payload))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def exercise(directory, operation, window):
    path = directory / (operation + "_" + window + ".sqlite3")
    kernel = prepare(path, operation)
    old = kernel.store.read()
    command = envelope(operation, kernel.decision_snapshot().version)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=killed_writer,
                              args=(str(path), command.model_dump(mode="json"), window, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20), f"writer did not reach {window}"
        kind, staged = parent.recv()
        assert kind == "barrier"
        assert staged["revision"] == old["revision"] + 1
        # A reader cannot see any uncommitted resource, binding, event or receipt.
        expected = old if window == "before_commit" else staged
        assert kernel.store.read() == expected
        process.kill()
        process.join(10)
        assert not process.is_alive() and process.exitcode == -9
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()

    recovered = Kernel(path)
    assert recovered.store.read() == expected
    receipt, payload = recovered.commit_with_payload(command)
    assert receipt.outcome != "rejected", receipt
    final = recovered.store.read()
    # Before COMMIT, retry applies exactly the missing transaction. After COMMIT,
    # it returns the original stored result despite the obsolete expected version.
    assert final == staged
    replay_receipt, replay_payload = recovered.commit_with_payload(command)
    assert replay_receipt == receipt and replay_payload == payload
    assert recovered.store.read() == final
    assert len(final["allocations"]) == 1
    assert len(final["jobs"]) == (1 if operation == "workflow_job" else 0)
    assert len({event["event_id"] for event in final["events"]}) == len(final["events"])
    if operation == "allocation":
        altered = command.model_copy(update={"parameters": allocation().model_copy(
            update={"ttl_seconds": 299})})
        conflict = recovered.commit(altered)
        assert conflict.outcome == "rejected" and conflict.error.code == "idempotency_conflict"
        assert recovered.store.read() == final
    return {"operation": operation, "window": window, "killed_pid": process.pid,
            "exit_code": process.exitcode, "caller_received_response": False,
            "old_state_sha256": digest(old), "staged_state_sha256": digest(staged),
            "recovered_state_sha256": digest(expected), "final_state_sha256": digest(final),
            "receipt": receipt.model_dump(mode="json"), "payload": payload,
            "replayed_receipt_equal": True, "replayed_payload_equal": True,
            "allocation_count": len(final["allocations"]), "job_count": len(final["jobs"]),
            "event_count": len(final["events"])}


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("window", WINDOWS)
def test_sigkill_at_sqlite_commit_boundary(tmp_path, operation, window):
    exercise(tmp_path, operation, window)
