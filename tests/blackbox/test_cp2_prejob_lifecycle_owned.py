"""Leases recalled or ended between allocation and the first job binding."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from campus_fabric.contracts import models as m
from campus_fabric.executors.cpu_reference import CpuReferenceConfig
from campus_fabric.workflows.devhost import create_app


def application(directory, mode="logical_simulation"):
    actors = {name: m.AuthenticatedActor(actor_id=name, owner_id=name, role=role)
              for name, role in [("alice", "researcher"), ("bob", "researcher"),
                                 ("supplier", "contributor")]}
    actors["admin"] = m.AuthenticatedActor(actor_id="admin", role="administrator")
    resources = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                                 gpu_count=1, gpu_memory_mib=8192)
    node = m.NodeInventory(node_id="prejob-node", contributor_owner_id="supplier", state="Ready",
        observed_at=0., capacity=resources, available=resources,
        devices=(m.GPUDevice(device_id="gpu", physical_gpu_id="gpu", model="reference",
                             memory_mib=8192, state="available"),))
    return create_app(data_dir=directory, inventory=(node,), auth_tokens=actors,
        execution_mode=mode, worker_enabled=False,
        cpu_config=CpuReferenceConfig(total_steps=5000, batch_steps=1000, batch_pause_seconds=.005))


def headers(owner="alice"):
    return {"Authorization": "Bearer " + owner,
            **({"X-Owner": owner} if owner != "admin" else {})}


def read(client, name="prejob"):
    response = client.get("/x/v1/work-intents/" + name, headers=headers())
    assert response.status_code == 200, response.text
    return response.json()


def act(client, action, *, stage=None, owner="alice", name="prejob"):
    for _ in range(8):
        payload = {"action": action, "expected_version": read(client, name)["version"]}
        if stage:
            payload["stage_id"] = stage
        result = client.post(f"/x/v1/work-intents/{name}/actions", headers=headers(owner), json=payload)
        if result.status_code != 409 or result.json().get("error") != "stale_version":
            return result
    raise AssertionError("Fresh isolated command starved on CPU clock revisions")


def register_and_allocate(client, mode="logical_simulation", name="prejob"):
    registration = m.WorkIntentRegistration(intent_id=name, owner_id="alice", execution_mode=mode,
        settlement_action="release_on_success", specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id="alice",
                ttl_seconds=30, preemption_mode="checkpoint", checkpoint_timeout_seconds=1,
                max_restarts=0, request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                                        gpu=m.GPURequest(count=1))),
            job=m.JobSpecification(image="not-executed", command=("not-executed",))))
    for _ in range(8):
        result = client.post("/x/v1/work-intents", headers=headers(),
                             json=registration.model_dump(mode="json"))
        if result.status_code == 202:
            break
        assert result.status_code == 409 and result.json().get("error") == "stale_version"
    assert result.status_code == 202, result.text
    assert act(client, "allocate", stage="allocate", name=name).status_code == 200
    value = read(client, name)
    assert value["allocations"][0]["state"] == "Allocated"
    assert not value["item"]["job_ids"] and not value["item"]["attempts"]
    return value


def recall(client):
    result = client.post("/x/v1/contributions/recall", headers=headers("supplier"),
        json={"gpu_ids": ["gpu"], "notice_seconds": 1, "duration_seconds": 1})
    assert result.status_code == 201, result.text


def advance(client, seconds):
    result = client.post("/x/v1/admin/clock", headers=headers("admin"),
                         json={"advance_seconds": seconds})
    assert result.status_code == 200, result.text


def until(client, predicate, *, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read(client)
        if predicate(value):
            return value
        time.sleep(.03)
    raise AssertionError(value)


@pytest.mark.parametrize("report", ["timeout", "failed"])
def test_prejob_recall_resumes_without_checkpoint_or_restart_cost(tmp_path, report):
    app = application(tmp_path)
    with TestClient(app) as client:
        initial = register_and_allocate(client)
        aid, expiry = initial["allocations"][0]["id"], initial["allocations"][0]["expires_at"]
        recall(client)
        # New job creation cannot cross a live contributor notice.
        blocked = act(client, "create_job", stage="job")
        assert blocked.status_code == 409 and blocked.json()["error"] == "lease_unavailable"
        if report == "failed":
            assert client.post(f"/v1/allocations/{aid}/checkpoint", headers=headers(),
                               json={"status": "failed"}).status_code == 200
        else:
            advance(client, 1)
        stopped = read(client)
        assert stopped["allocations"][0]["state"] == "Preempted"
        assert stopped["item"]["state"] == "waiting" and stopped["item"]["result"] is None
        assert any(a["operation"] == "allocation.resume" for a in stopped["legal_actions"])
        assert act(client, "resume", owner="bob").status_code == 404
        assert act(client, "resume").status_code == 200
        assert read(client)["allocations"][0]["state"] == "Pending"
        advance(client, 2)
        granted = read(client)
        assert granted["allocations"][0]["state"] == "Allocated"
        assert granted["allocations"][0]["expires_at"] == expiry
        assert granted["allocations"][0]["restart_count"] == 0
        assert act(client, "create_job", stage="job").status_code == 200
        assert act(client, "create_job", stage="job").status_code == 200
        active = read(client)
        assert len(active["item"]["job_ids"]) == len(active["item"]["attempts"]) == 1
        assert active["item"]["attempts"][0]["ref"]["generation"] == 1
        assert active["allocations"][0]["id"] == aid
        assert active["allocations"][0]["restart_count"] == active["jobs"][0]["retry_count"] == 0
        advance(client, 10)
        assert read(client)["item"]["state"] == "completed"


@pytest.mark.parametrize("ending", ["expiry", "node_failure", "release"])
def test_terminal_prejob_lease_has_final_result_and_cannot_be_revived(tmp_path, ending):
    app = application(tmp_path)
    with TestClient(app) as client:
        initial = register_and_allocate(client)
        aid = initial["allocations"][0]["id"]
        if ending == "expiry":
            advance(client, 31)
        elif ending == "release":
            assert act(client, "release").status_code == 200
        else:
            receipt = app.state.kernel.commit(m.CommandEnvelope(
                request_id="isolated-node-failure", actor=m.AuthenticatedActor(actor_id="test", role="test_driver"),
                operation="test.node_state", parameters=m.NodeStateParameters(
                    kind="test.node_state", node_id="prejob-node", state="NotReady")))
            assert receipt.outcome == "committed", receipt.error
        value = read(client)
        expected_state = "cancelled" if ending == "release" else "failed"
        assert value["item"]["state"] == expected_state
        assert value["item"]["result"]["kind"] == ("cancelled" if ending == "release" else "failure")
        assert not value["item"]["job_ids"] and not value["item"]["attempts"]
        assert any(e["event_type"] == "intent." + expected_state for e in value["events"])
        assert value["allocations"][0]["placement"] is None
        assert value["legal_actions"] == []
        for action, stage in [("allocate", "new-allocation"), ("create_job", "new-job"), ("resume", None)]:
            assert act(client, action, stage=stage).status_code == 409
        final = read(client)
        assert final["item"]["allocation_ids"] == [aid]
        assert final["item"]["result"] == value["item"]["result"]


def test_real_cpu_starts_exactly_once_after_prejob_recall_and_resume(tmp_path):
    app = application(tmp_path, "cpu_reference")
    with TestClient(app) as client:
        initial = register_and_allocate(client, "cpu_reference")
        expiry = initial["allocations"][0]["expires_at"]
        recall(client)
        stopped = until(client, lambda v: v["allocations"][0]["state"] == "Preempted")
        assert not stopped["item"]["attempts"] and not stopped["item"]["job_ids"]
        assert stopped["allocations"][0]["checkpoint"] is None
        assert act(client, "resume").status_code == 200
        until(client, lambda v: v["allocations"][0]["state"] == "Allocated")
        assert act(client, "create_job", stage="job").status_code == 200
        assert act(client, "create_job", stage="job").status_code == 200
        completed = until(client, lambda v: v["item"]["state"] == "completed")
        assert len(completed["item"]["job_ids"]) == len(completed["item"]["attempts"]) == 1
        assert completed["allocations"][0]["expires_at"] == expiry
        assert completed["allocations"][0]["restart_count"] == completed["jobs"][0]["retry_count"] == 0
        uri = completed["jobs"][0]["result_uri"]
        result = client.get("/x/v1/artifacts", headers=headers(), params={"uri": uri})
        assert result.status_code == 200 and result.json()["state"] == app.state.cpu_driver.executor.reference_result()
        until(client, lambda v: v["allocations"][0]["state"] == "Released")
