"""Real process integration: authority, driver, private artifacts and reset fence."""
import time

import pytest

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.executors.cpu_reference import CpuReferenceConfig, CpuReferenceExecutor
from campus_fabric.executors.driver import CpuDriver

ALICE = m.AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice")
TEST = m.AuthenticatedActor(actor_id="test", role="test_driver")


def node(now=0.):
    capacity = m.ResourceAmounts(cpu_millicores=2000, memory_mib=1024,
                                gpu_count=1, gpu_memory_mib=8192)
    return m.NodeInventory(node_id="cpu-node", contributor_owner_id="supplier", state="Ready",
        capacity=capacity, available=capacity, observed_at=now,
        devices=(m.GPUDevice(device_id="g1", physical_gpu_id="g1", model="ref",
                            memory_mib=8192, state="available"),))


def command(k, p, actor=ALICE):
    receipt = k.commit(m.CommandEnvelope(request_id="test-request", actor=actor,
                                         operation=p.kind, parameters=p))
    assert receipt.outcome != "rejected", receipt
    return receipt


def spin(driver, predicate, timeout=7):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        driver.tick(advance_wall=False)
        if predicate():
            return
        time.sleep(.02)
    pytest.fail("Physical transition did not complete")


def advance(k, seconds):
    command(k, m.ClockAdvanceParameters(kind="clock.advance", advance_seconds=float(seconds),
                                        source="test_control"), TEST)


@pytest.fixture
def system(tmp_path):
    k = Kernel(tmp_path / "state.db", inventory=(node(),), execution_mode="cpu_reference")
    executor = CpuReferenceExecutor(tmp_path / "cpu", CpuReferenceConfig(
        total_steps=100000, batch_steps=1000, batch_pause_seconds=.01))
    driver = CpuDriver(k, executor, lock_path=tmp_path / "driver.lock")
    driver.start()
    yield k, executor, driver
    # Release all current executions, and settle any old-epoch processes without assuming stop.
    for a in list(k.store.read()["allocations"].values()):
        command(k, m.AllocationTargetParameters(kind="allocation.release",
                                               allocation_id=a["allocation_id"]))
    spin(driver, lambda: not k.store.read().get("retired") and
         not any(j.get("physical_live") for j in k.store.read()["jobs"].values()))
    driver.close()


def start(k):
    receipt = command(k, m.AllocationCreateParameters(kind="allocation.create", owner_id="alice",
        ttl_seconds=300, preemption_mode="checkpoint", checkpoint_timeout_seconds=10,
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=256,
                                  gpu=m.GPURequest(count=1))))
    aid = next(o.entity_id for o in receipt.objects if o.kind == "allocation")
    receipt = command(k, m.JobCreateParameters(kind="job.create", allocation_id=aid,
        image="not-executed", command=("not-executed",), timeout_seconds=200))
    jid = next(o.entity_id for o in receipt.objects if o.kind == "job")
    return aid, jid


def test_real_checkpoint_stop_resume_and_result_match(system):
    k, executor, driver = system
    aid, jid = start(k)
    driver.tick(advance_wall=False)
    time.sleep(.12)
    command(k, m.RecallParameters(kind="test.contributor_demand", device_ids=("g1",),
                                  notice_seconds=5, duration_seconds=1), TEST)
    spin(driver, lambda: k.store.read()["allocations"][aid]["state"] == "Preempted")
    state = k.store.read()
    cp = state["jobs"][jid]["checkpoint"]
    assert cp["verification_status"] == "verified"
    assert cp["continuation_status"] == "not_attempted"
    saved = executor.read_artifact(cp["artifact_uri"], owner_id="alice", epoch=0)
    assert 0 < saved["state"]["step"] < 100000
    expiry = state["allocations"][aid]["expires_at"]
    command(k, m.AllocationTargetParameters(kind="allocation.resume", allocation_id=aid))
    assert k.store.read()["allocations"][aid]["state"] == "Pending"
    assert k.store.read()["allocations"][aid]["restart_count"] == 0
    advance(k, 6)
    spin(driver, lambda: k.store.read()["jobs"][jid]["state"] == "Succeeded")
    state = k.store.read()
    assert state["allocations"][aid]["expires_at"] == expiry
    assert state["allocations"][aid]["restart_count"] == 1
    assert state["jobs"][jid]["retry_count"] == 0
    assert state["jobs"][jid]["checkpoint"]["continuation_status"] == "applied"
    result = executor.read_artifact(state["jobs"][jid]["result_uri"], owner_id="alice", epoch=0)
    assert result["state"] == executor.reference_result()


def test_manual_clock_cannot_fabricate_cpu_stop_or_success(system):
    k, _, driver = system
    aid, jid = start(k)
    driver.tick(advance_wall=False)
    advance(k, 300)
    state = k.store.read()
    assert state["jobs"][jid]["state"] != "Succeeded"
    assert state["allocations"][aid]["placement"] is not None
    assert k.decision_snapshot().inventory[0].available.gpu_count == 0
    assert state["allocations"][aid]["pending_terminal"] == "Expired"
    spin(driver, lambda: k.store.read()["allocations"][aid]["state"] == "Expired")
    assert k.store.read()["jobs"][jid]["state"] == "Failed"
    assert k.decision_snapshot().inventory[0].available.gpu_count == 1


def test_reset_keeps_private_old_process_fence_until_real_stop(system):
    k, _, driver = system
    aid, jid = start(k)
    driver.tick(advance_wall=False)
    old_attempt = k.store.read()["jobs"][jid]["attempt"]
    command(k, m.ResetParameters(kind="test.reset"), TEST)
    state = k.store.read()
    assert not state["jobs"] and not state["allocations"] and not state["nodes"]
    assert old_attempt["attempt_id"] in state["retired"]
    contributor = m.AuthenticatedActor(actor_id="supplier", role="contributor", owner_id="supplier")
    command(k, m.ContributionRegisterParameters(kind="contribution.register", node=node()), contributor)
    assert k.decision_snapshot().inventory[0].available.cpu_millicores == 0
    spin(driver, lambda: not k.store.read()["retired"])
    assert k.decision_snapshot().inventory[0].available.cpu_millicores == 2000
    assert not k.store.read()["jobs"]


def test_only_one_monotonic_driver_owns_an_instance(system):
    k, executor, driver = system
    other = CpuDriver(k, executor, lock_path=driver.lock_path)
    with pytest.raises(RuntimeError, match="Another"):
        other.start()


def test_clock_sample_and_deadlines_rollback_together_on_crash(tmp_path, monkeypatch):
    k = Kernel(tmp_path / "clock.db", inventory=(node(),), execution_mode="cpu_reference")
    k.sync_cpu_clock("same-boot", 1000000000)
    aid, jid = start(k)
    initial = k.store.read()
    real_save = k.store.save

    def crash_after_write(conn, state):
        real_save(conn, state)
        raise OSError("Crash before SQLite commit")

    monkeypatch.setattr(k.store, "save", crash_after_write)
    with pytest.raises(OSError):
        k.sync_cpu_clock("same-boot", 302000000000)
    assert k.store.read() == initial
    monkeypatch.setattr(k.store, "save", real_save)
    k.sync_cpu_clock("same-boot", 302000000000)
    settled = k.store.read()
    assert settled["now"] == 301.
    assert settled["allocations"][aid]["pending_terminal"] == "Expired"
    assert settled["jobs"][jid]["physical_live"]  # Clock is not proof of process exit.
    # Retrying a sample after successful commit cannot double-advance deadlines.
    k.sync_cpu_clock("same-boot", 302000000000)
    assert k.store.read() == settled


def test_clock_reset_preserves_anchor_and_boot_change_refuses_recovery(tmp_path):
    k = Kernel(tmp_path / "clock.db", execution_mode="cpu_reference")
    k.sync_cpu_clock("same-boot", 1000000000)
    k.sync_cpu_clock("same-boot", 2000000000)
    command(k, m.ResetParameters(kind="test.reset"), TEST)
    k.sync_cpu_clock("same-boot", 3000000000)
    saved = k.store.read()
    assert saved["epoch"] == 1 and saved["now"] == 1.
    with pytest.raises(RuntimeError, match="explicit recovery"):
        k.sync_cpu_clock("new-boot", 4000000000)
    assert k.store.read() == saved
