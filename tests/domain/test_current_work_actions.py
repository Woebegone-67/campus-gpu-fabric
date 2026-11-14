"""User work actions tolerate unrelated revisions without weakening explicit CAS."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from campus_fabric.app import create_app
from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel

ALICE = {"X-Owner": "alice"}
BOB = {"X-Owner": "bob"}
ACTOR = m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="member")
TEST = {"X-Test-Key": "current-work-test"}
PATH = "/x/v1/work-intents/current-work"


def inventory():
    cap = m.ResourceAmounts(cpu_millicores=2000, memory_mib=4096,
                           gpu_count=1, gpu_memory_mib=4096)
    return m.NodeInventory(
        node_id="current-node", contributor_owner_id="supplier", state="Ready",
        capacity=cap, available=cap, observed_at=0.0,
        devices=(m.GPUDevice(device_id="current-gpu", physical_gpu_id="current-gpu",
                             model="A10", memory_mib=4096, state="available"),),
    )


def registration(mode="logical_simulation"):
    return m.WorkIntentRegistration(
        intent_id="current-work", owner_id="alice", execution_mode=mode,
        settlement_action="keep_lease",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(
                kind="allocation.create", owner_id="alice", ttl_seconds=100,
                preemption_mode="checkpoint", max_restarts=1,
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                           gpu=m.GPURequest(count=1, model="A10")),
            ),
            job=m.JobSpecification(image="logical", command=("run",), timeout_seconds=80),
        ),
    )


def tick(client, seconds):
    response = client.post("/_test/clock", headers=TEST, json={"advance_seconds": seconds})
    assert response.status_code == 200, response.text


def view(client):
    response = client.get(PATH, headers=ALICE)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def interrupted(tmp_path):
    app = create_app(data_dir=tmp_path, inventory=(inventory(),), test_key=TEST["X-Test-Key"])
    with TestClient(app) as client:
        response = client.post("/x/v1/work-intents", headers=ALICE,
                               json=registration().model_dump(mode="json"))
        assert response.status_code == 202
        for action in ("allocate", "create_job"):
            response = client.post(PATH + "/actions", headers=ALICE, json={
                "action": action, "stage_id": action, "expected_version": view(client)["version"],
            })
            assert response.status_code == 200, response.text
        assert client.post("/_test/contributor-demand", headers=TEST, json={
            "gpu_ids": ["current-gpu"], "notice_seconds": 2, "duration_seconds": 2,
        }).status_code == 201
        aid = view(client)["intent"]["allocation_ids"][0]
        assert client.post(f"/v1/allocations/{aid}/checkpoint", headers=ALICE,
                           json={"status": "ready"}).status_code == 200
        yield client, app.state.kernel


@pytest.mark.parametrize("action", ["resume", "cancel", "release"])
def test_current_action_accepts_unrelated_clock_but_explicit_cas_stays_strict(interrupted, action):
    client, _ = interrupted
    old = view(client)["version"]
    tick(client, .1)
    assert view(client)["version"] != old
    rejected = client.post(PATH + "/actions", headers=ALICE,
                           json={"action": action, "expected_version": old})
    assert rejected.status_code == 409 and rejected.json()["error"] == "stale_version"
    accepted = client.post(PATH + "/actions", headers=ALICE,
                           json={"action": action, "expected_epoch": old["epoch"]})
    assert accepted.status_code == 200, accepted.text
    current = view(client)["intent"]
    if action == "resume":
        assert current["state"] == "waiting"
        aid = current["allocation_ids"][0]
        allocation = client.get(f"/v1/allocations/{aid}", headers=ALICE).json()
        assert allocation["state"] == "Pending" and allocation["restart_count"] == 0
        assert len(current["attempts"]) == 1
    else:
        assert current["state"] == "cancelled"


@pytest.mark.parametrize("extra", [{}, {"expected_epoch": None}, {"expected_epoch": True},
                                  {"expected_epoch": "0"}, {"expected_epoch": -1},
                                  {"expected_epoch": 1.5},
                                  {"expected_epoch": m.MAX_SAFE_INTEGER + 1},
                                  {"expected_epoch": 0, "expected_version": {"epoch": 0, "revision": 0}}])
def test_current_action_requires_one_valid_epoch_condition(interrupted, extra):
    client, _ = interrupted
    initial = view(client)
    response = client.post(PATH + "/actions", headers=ALICE, json={"action": "resume", **extra})
    assert response.status_code == 400, response.text
    assert view(client) == initial


@pytest.mark.parametrize("action", ["allocate", "create_job", "unknown"])
def test_epoch_condition_does_not_replace_stage_or_unknown_action_contract(interrupted, action):
    client, _ = interrupted
    response = client.post(PATH + "/actions", headers=ALICE,
                           json={"action": action, "expected_epoch": 0, "stage_id": "new-stage"})
    assert response.status_code == 400, response.text


def test_current_action_preserves_owner_isolation_and_reset_fence(interrupted):
    client, _ = interrupted
    original = view(client)
    assert client.post(PATH + "/actions", headers=BOB,
                       json={"action": "resume", "expected_epoch": 0}).status_code == 404
    assert view(client) == original
    assert client.post("/_test/reset", headers=TEST).status_code == 204
    # Reusing a work ID after reset cannot authorize an old click.
    assert client.post("/x/v1/work-intents", headers=ALICE,
                       json=registration().model_dump(mode="json")).status_code == 202
    new = view(client)
    response = client.post(PATH + "/actions", headers=ALICE,
                           json={"action": "resume", "expected_epoch": 0})
    assert response.status_code == 409 and response.json()["error"] == "stale_version"
    assert view(client) == new


def test_concurrent_current_resume_creates_only_one_generation(interrupted, tmp_path):
    client, _ = interrupted
    tick(client, 4)
    count = 8
    kernels = [Kernel(tmp_path / "fabric.sqlite3") for _ in range(count)]
    barrier = Barrier(count)

    def resume(index):
        barrier.wait(timeout=10)
        return kernels[index].act_on_current_intent(
            "current-work", "resume", 0, actor=ACTOR, request_id=f"concurrent-{index}",
        )

    with ThreadPoolExecutor(max_workers=count) as pool:
        receipts = list(pool.map(resume, range(count)))
    assert sum(receipt.outcome == "committed" for receipt in receipts) == 1
    assert all(receipt.outcome != "rejected" or receipt.error.code == "lease_unavailable"
               for receipt in receipts)
    state = view(client)["intent"]
    assert state["state"] == "running" and len(state["attempts"]) == 2
    aid = state["allocation_ids"][0]
    assert client.get(f"/v1/allocations/{aid}", headers=ALICE).json()["restart_count"] == 1


def test_cpu_clock_revisions_do_not_invalidate_current_release(tmp_path):
    kernel = Kernel(tmp_path / "cpu.sqlite3", inventory=(inventory(),), execution_mode="cpu_reference")
    kernel.register_current_intent(registration("cpu_reference"), actor=ACTOR, request_id="register")
    kernel.create_allocation_and_bind("current-work", "allocate", kernel.decision_snapshot().version,
                                      actor=ACTOR, request_id="allocate")
    epoch = kernel.decision_snapshot().version.epoch
    kernel.sync_cpu_clock("isolated-boot", 1_000_000_000)
    old = kernel.decision_snapshot().version
    kernel.sync_cpu_clock("isolated-boot", 1_050_000_000)
    assert kernel.decision_snapshot().version != old
    result = kernel.act_on_current_intent("current-work", "release", epoch,
                                         actor=ACTOR, request_id="release")
    assert result.outcome == "committed"
    state = kernel.read_snapshot(m.ReadQuery(actor=ACTOR))
    assert state.allocations[0].state == "Released"
