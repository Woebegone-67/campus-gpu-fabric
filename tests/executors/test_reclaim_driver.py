"""The planner must control actual CPU dispatch, not only a replay model."""
import os
import signal
import time

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.executors.cpu_reference import CpuReferenceConfig, CpuReferenceExecutor
from campus_fabric.executors.driver import CpuDriver

USER = m.AuthenticatedActor(actor_id="u", role="member", owner_id="u")
OWNER = m.AuthenticatedActor(actor_id="o", role="member", owner_id="o")


def commit(kernel, parameters, actor=USER):
    receipt = kernel.commit(m.CommandEnvelope(request_id="reclaim-test", actor=actor,
        operation=parameters.kind, parameters=parameters))
    assert receipt.outcome != "rejected", receipt
    return receipt


def spin(driver, predicate, timeout=8):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        driver.tick()
        if predicate():
            return
        time.sleep(.02)
    pytest.fail("Real CPU recall did not reach expected transition")


@pytest.mark.parametrize(("action", "policy", "total_steps", "notice"), [
    ("finish_first", "preserve_work", 300000, 10),
    ("save_at", "preserve_work", 2000000, 4),
    ("save_now", "return_first", 2000000, 4),
])
def test_real_signed_progress_drives_adaptive_recall(tmp_path, action, policy, total_steps, notice):
    capacity = m.ResourceAmounts(cpu_millicores=2000, memory_mib=1024, gpu_count=1, gpu_memory_mib=8192)
    node = m.NodeInventory(node_id="n", contributor_owner_id="o", state="Ready",
        capacity=capacity, available=capacity, observed_at=0., devices=(m.GPUDevice(
            device_id="g", physical_gpu_id="g", model="reference", memory_mib=8192, state="available"),))
    kernel = Kernel(tmp_path / "state.db", inventory=(node,), execution_mode="cpu_reference")
    executor = CpuReferenceExecutor(tmp_path / "cpu", CpuReferenceConfig(
        total_steps=total_steps, batch_steps=1000, batch_pause_seconds=.01 if action == "finish_first" else .003))
    driver = CpuDriver(kernel, executor, lock_path=tmp_path / "driver.lock")
    driver.start()
    try:
        receipt = commit(kernel, m.AllocationCreateParameters(kind="allocation.create", owner_id="u",
            ttl_seconds=300, preemption_mode="checkpoint", checkpoint_timeout_seconds=10,
            request=m.ResourceRequest(cpu_millicores=1000, memory_mib=256, gpu=m.GPURequest(count=1))))
        aid = next(ref.entity_id for ref in receipt.objects if ref.kind == "allocation")
        receipt = commit(kernel, m.JobCreateParameters(kind="job.create", allocation_id=aid,
            image="unused", command=("unused",), timeout_seconds=100))
        jid = next(ref.entity_id for ref in receipt.objects if ref.kind == "job")
        spin(driver, lambda: (kernel.store.read()["jobs"][jid].get("observed_progress") or {}).get("rate_lower") is not None)
        commit(kernel, m.RecallParameters(kind="contribution.recall", device_ids=("g",),
            notice_seconds=notice, duration_seconds=2, return_policy=policy), OWNER)
        allocation = kernel.store.read()["allocations"][aid]
        plan = allocation["reclaim_plan"]
        assert plan["action"] == action
        assert plan["evidence_digest"] and plan["inputs"]["trusted"]
        assert plan["stop_at"] <= allocation["preemption"]["deadline"] - 2
        assert allocation["preemption"]["checkpoint_due"] == allocation["preemption"]["deadline"]
        if action == "save_at":
            commit(kernel, m.RecallParameters(kind="contribution.recall", device_ids=("g",),
                notice_seconds=10, duration_seconds=2, return_policy=policy), OWNER)
            revised = kernel.store.read()["allocations"][aid]
            assert revised["reclaim_plan"]["stop_at"] <= plan["stop_at"]
            assert revised["reclaim_plan"]["dispatch_at"] <= plan["dispatch_at"]
            assert revised["preemption"]["checkpoint_due"] == allocation["preemption"]["checkpoint_due"]
            assert len(revised["reclaim_history"]) == 1
        spin(driver, lambda: kernel.store.read()["allocations"][aid]["placement"] is None)
        final = kernel.store.read()
        audit = final["allocations"][aid]["reclaim_plan"]
        assert audit["stop_confirmed_at"] <= allocation["preemption"]["deadline"]
        assert all(handle.poll() is not None for handle in executor._handles.values())
        if action == "finish_first":
            assert final["jobs"][jid]["state"] == "Succeeded"
            assert "checkpoint_dispatched_at" not in audit
        else:
            assert final["jobs"][jid]["checkpoint"]["verification_status"] == "verified"
            assert audit["checkpoint_dispatched_at"] <= audit["stop_dispatched_at"] <= audit["stop_confirmed_at"]
            if action == "save_at":
                assert audit["checkpoint_dispatched_at"] >= audit["dispatch_at"] > audit["planned_at"]
    finally:
        for handle in executor._handles.values():
            if handle.poll() is None:
                os.killpg(handle.pid, signal.SIGKILL)
            handle.wait(timeout=5)
        driver.close()
