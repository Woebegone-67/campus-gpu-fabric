"""Adapter and evidence runner for the standalone CP3 reference specification."""

from __future__ import annotations

import copy
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from cp3_reference import ACTIVE, CAPACITY, Mismatch, Reference, linearize

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel

ADMIN = m.AuthenticatedActor(actor_id="cp3-model-audit", role="administrator")
TEST = m.AuthenticatedActor(actor_id="cp3-model-control", role="test_driver")
DIMENSIONS = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")
SHAPES = [(1, 2, 1, 1, "A10", 4), (2, 2, 3, 1, "any", 8),
          (1, 6, 1, 0, "A10", 4), (3, 1, 1, 0, "any", 0),
          (1, 1, 1, 2, "A10", 8), (5, 1, 1, 1, "A10", 4),
          (1, 1, 7, 0, "any", 0), (1, 1, 1, 1, "H800", 4),
          (1, 1, 1, 1, "A10", 9)]


def actor(owner):
    return m.AuthenticatedActor(actor_id=owner, owner_id=owner, role="researcher")


def fixture():
    capacity = m.ResourceAmounts(cpu_millicores=4, memory_mib=8, ephemeral_storage_mib=6,
                                gpu_count=2, gpu_memory_mib=16)
    return m.NodeInventory(node_id="model-node", contributor_owner_id="supplier", state="Ready",
                           capacity=capacity, available=capacity, observed_at=0.0,
                           devices=tuple(m.GPUDevice(device_id=f"gpu-{i}", physical_gpu_id=f"gpu-{i}",
                                                     model="A10", memory_mib=8, state="available")
                                         for i in range(2)))


def new_kernel(path):
    return Kernel(path, inventory=(fixture(),))


def seconds(stamp):
    if stamp is None:
        return None
    return (datetime.fromisoformat(stamp.replace("Z", "+00:00")) -
            datetime(2026, 1, 1, tzinfo=timezone.utc)).total_seconds()


def state_summary(state):
    node = state["nodes"]["model-node"]
    return {
        "now": state["now"], "node": node["state"],
        "free": [node["available"][d] for d in (*DIMENSIONS, "gpu_count")],
        "allocations": {k: {"owner": a["owner_id"], "state": a["state"],
                            "granted": a["granted_at"], "expires": a["expires_at"]}
                        for k, a in state["allocations"].items()},
        "jobs": {k: {"owner": j["owner_id"], "state": j["state"],
                     "allocation": j["allocation_id"], "finished": j["finished_at"]}
                 for k, j in state["jobs"].items()},
    }


def invoke(kernel, op):
    kind = op["kind"]
    if kind == "read":
        owner = op.get("owner")
        state = kernel.export_state(actor(owner) if owner else ADMIN)
        if owner:
            assert all(e["owner_id"] == owner for e in state["events"])
        return state_summary(state)
    acting = actor(op["owner"]) if "owner" in op else TEST
    if kind == "create":
        cpu, memory, disk, count, model, vram = op["shape"]
        p = m.AllocationCreateParameters(kind="allocation.create", owner_id=op["owner"],
            ttl_seconds=op.get("ttl", 30), priority=op.get("priority", 0),
            request=m.ResourceRequest(cpu_millicores=cpu, memory_mib=memory,
                ephemeral_storage_mib=disk, gpu=m.GPURequest(count=count, model=model,
                                                            memory_mib=vram)))
    elif kind == "release":
        p = m.AllocationTargetParameters(kind="allocation.release", allocation_id=op["target"])
    elif kind == "renew":
        p = m.AllocationRenewParameters(kind="allocation.renew", allocation_id=op["target"],
                                       ttl_seconds=op.get("ttl", 30))
    elif kind == "job":
        p = m.JobCreateParameters(kind="job.create", allocation_id=op["target"],
                                 image="independent-model", command=("work",),
                                 timeout_seconds=op.get("timeout", 50))
    elif kind == "cancel":
        p = m.JobCancelParameters(kind="job.cancel", job_id=op["target"])
    elif kind == "clock":
        p = m.ClockAdvanceParameters(kind="clock.advance", advance_seconds=float(op["delta"]),
                                    source="test_control")
    elif kind == "node":
        p = m.NodeStateParameters(kind="test.node_state", node_id="model-node", state=op["state"])
    else:
        raise ValueError(kind)
    receipt, payload = kernel.commit_with_payload(m.CommandEnvelope(
        request_id=op["request_id"], actor=acting, operation=p.kind, parameters=p,
        idempotency_key=op.get("key")))
    if receipt.outcome == "rejected":
        return {"error": receipt.error.code, "time": receipt.logical_time}
    answer = {"time": receipt.logical_time, "status": payload["status_code"]}
    body = payload["body"]
    if kind in {"create", "renew", "job"}:
        answer.update(id=body["id"], state=body["state"])
    if kind in {"create", "renew"}:
        answer["expires"] = seconds(body["expires_at"])
    if kind == "node":
        answer.update(state=body["state"], generation=body["generation"])
    return answer


def audit_snapshot(kernel, reference):
    """Independently check observable states, placements AND signed event history."""
    state = kernel.export_state(ADMIN)
    Reference._equal(reference.summary(), state_summary(state), "complete snapshot")
    actual_claims, used = {}, [0, 0, 0]
    for ident, a in state["allocations"].items():
        placement = a["placement"]
        if a["state"] not in ACTIVE:
            assert placement is None, "Terminal or pending lease retains a placement"
            continue
        assert placement is not None and len(placement["slices"]) == 1
        part = placement["slices"][0]
        assert part["node_id"] == "model-node"
        expected = reference.allocations[ident]["demand"]
        assert tuple(part["resources"][d] for d in DIMENSIONS) == expected[:3]
        assert len(part["device_ids"]) == expected[3]
        assert part["resources"]["gpu_count"] == expected[3]
        assert part["resources"]["gpu_memory_mib"] == expected[3] * 8
        used = [u + q for u, q in zip(used, expected[:3])]
        for device in part["device_ids"]:
            assert device in {"gpu-0", "gpu-1"} and device not in actual_claims
            actual_claims[device] = ident
    assert all(u <= c for u, c in zip(used, CAPACITY))
    event_claims, event_used, ids = {}, [0, 0, 0], set()
    for sequence, event in enumerate(state["events"], start=1):
        assert event["sequence"] == sequence and event["event_id"] not in ids
        ids.add(event["event_id"])
        for delta in event["resource_deltas"]:
            assert delta["node_id"] == "model-node"
            event_used = [u + delta[d] for u, d in zip(event_used, DIMENSIONS)]
            assert all(0 <= u <= c for u, c in zip(event_used, CAPACITY))
            for device in delta["device_ids_added"]:
                assert device not in event_claims, "Duplicate event claim"
                event_claims[device] = delta["allocation_id"]
            for device in delta["device_ids_removed"]:
                assert event_claims.pop(device) == delta["allocation_id"], "Wrong/double return"
    assert event_claims == actual_claims and event_used == used
    node = state["nodes"]["model-node"]
    assert node["available"]["gpu_memory_mib"] == (
        8 * (2 - len(actual_claims)) if reference.node == "Ready" else 0)
    for device in node["devices"]:
        expected = ("occupied" if device["device_id"] in actual_claims else
                    "available" if reference.node == "Ready" else "unavailable")
        assert device["state"] == expected
    for owner in ("alice", "bob"):
        Reference._equal(reference.summary(owner), invoke(kernel, {"kind": "read", "owner": owner}),
                         "owner projection")
    return state_summary(state)


def run_sequential(path, seed, steps):
    rng, model, kernel = random.Random(seed), Reference(), new_kernel(path)
    history, creations = [], []
    for index in range(steps):
        owner, roll = rng.choice(("alice", "bob")), rng.random()
        if not creations or roll < 0.30:
            op = dict(kind="create", owner=owner, shape=list(rng.choice(SHAPES)),
                      ttl=rng.choice((30, 35, 60)), priority=rng.randrange(3),
                      key=f"create-{rng.randrange(max(2, index // 3 + 1))}")
            creations.append(copy.deepcopy(op))
        elif roll < 0.43:
            op = copy.deepcopy(rng.choice(creations))
            if rng.random() < 0.5:
                op["shape"][0] = 1 + op["shape"][0] % 4
        elif roll < 0.66:
            target = rng.choice(list(model.allocations))
            allocation = model.allocations[target]
            op = dict(kind=rng.choice(("release", "renew", "job")), target=target,
                      owner=allocation["owner"] if rng.random() < 0.8 else owner)
            if op["kind"] == "renew":
                op["ttl"] = rng.choice((30, 50))
            elif op["kind"] == "job":
                op["timeout"] = rng.choice((5, 10, 40))
            if rng.random() < 0.5:
                op["key"] = f"mutation-{rng.randrange(4)}"
        elif roll < 0.74 and model.jobs:
            target = rng.choice(list(model.jobs))
            op = dict(kind="cancel", target=target, owner=model.jobs[target]["owner"])
        elif roll < 0.90:
            expiries = [a["expires"] for a in model.allocations.values()
                        if a["state"] in ACTIVE]
            delta = max(0, min(expiries) - model.now) if expiries and rng.random() < 0.5 else rng.choice((0, 1, 5, 10, 30, 60))
            op = dict(kind="clock", delta=delta)
        else:
            op = dict(kind="node", state=rng.choice(("Ready", "Ready", "Draining", "NotReady")))
        op["request_id"] = f"seed-{seed}-step-{index}"
        event = {"operation": op}
        history.append(event)
        try:
            event["result"] = invoke(kernel, op)
            model.step(op, event["result"])
            event["snapshot"] = audit_snapshot(kernel, model)
            if index % 13 == 0:  # Lost process handle/reopen must not lose state or replay keys.
                kernel = new_kernel(path)
                audit_snapshot(kernel, model)
        except Exception:
            path.with_suffix(".failure.json").write_text(json.dumps(history, indent=2) + "\n")
            raise
    return {"seed": seed, "steps": steps, "history": history, "final": model.summary()}


def concurrent_wave(path, operations):
    barrier = Barrier(len(operations))

    def run(op):
        kernel = new_kernel(path)
        barrier.wait(timeout=20)
        begin = time.monotonic_ns()
        answer = invoke(kernel, op)
        end = time.monotonic_ns()
        return {"operation": op, "result": answer, "begin_ns": begin, "end_ns": end}

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def run_concurrent(path, seed):
    kernel, model = new_kernel(path), Reference()
    setup = dict(kind="create", owner="alice", shape=list(SHAPES[0]), ttl=30,
                 request_id=f"setup-{seed}")
    reply = invoke(kernel, setup)
    model.step(setup, reply)
    aid = reply["id"]
    initial = copy.deepcopy(model)
    create = dict(kind="create", owner="bob", shape=list(SHAPES[0]), key="shared", ttl=30)
    variant = copy.deepcopy(create)
    variant["shape"][0] = 2
    mode = seed % 5
    if mode == 0:
        first = [create, copy.deepcopy(create), variant]
        second = [dict(kind="release", owner="alice", target=aid), dict(kind="clock", delta=30),
                  dict(kind="read", owner="bob")]
    elif mode == 1:
        first = [dict(kind="renew", owner="alice", target=aid, ttl=60),
                 dict(kind="clock", delta=30), dict(kind="release", owner="alice", target=aid)]
        second = [create, variant, dict(kind="read", owner="alice")]
    elif mode == 2:
        first = [dict(kind="job", owner="alice", target=aid, timeout=10, key="job"),
                 dict(kind="job", owner="alice", target=aid, timeout=10, key="job"),
                 dict(kind="job", owner="bob", target=aid, timeout=10)]
        second = [dict(kind="clock", delta=10), dict(kind="release", owner="alice", target=aid),
                  dict(kind="read", owner="alice")]
    elif mode == 3:
        first = [dict(kind="node", state="NotReady"), dict(kind="node", state="Ready"), create]
        second = [dict(kind="node", state="Draining"),
                  dict(kind="renew", owner="alice", target=aid, ttl=30), dict(kind="read")]
    else:
        first = [dict(kind="job", owner="alice", target=aid, timeout=50),
                 dict(kind="create", owner="bob", shape=list(SHAPES[4]), priority=2),
                 dict(kind="create", owner="alice", shape=list(SHAPES[2]))]
        second = [dict(kind="clock", delta=30), dict(kind="node", state="NotReady"),
                  dict(kind="read", owner="bob")]
    for i, op in enumerate(first + second):
        op["request_id"] = f"concurrent-{seed}-{i}"
    history = []
    try:
        history.extend(concurrent_wave(path, first))
        history.extend(concurrent_wave(path, second))
        final = state_summary(kernel.export_state(ADMIN))
        result = linearize(initial, history, final)
    except Exception as exc:
        path.with_suffix(".failure.json").write_text(json.dumps({
            "seed": seed, "planned_operations": first + second,
            "completed_history": history, "error": repr(exc)}, indent=2) + "\n")
        raise
    evidence = {"seed": seed, "setup": {"operation": setup, "result": reply},
                "history": history, "final": final, "search": result}
    if not result["linearizable"]:
        path.with_suffix(".failure.json").write_text(json.dumps(evidence, indent=2) + "\n")
        raise Mismatch(f"Nonlinearizable real concurrent history seed={seed}")
    for i in result["serialization"]:
        model.step(history[i]["operation"], history[i]["result"])
    audit_snapshot(kernel, model)
    return evidence


def boundary_scenarios(path):
    kernel, model, history = new_kernel(path), Reference(), []

    def step(**op):
        op["request_id"] = f"boundary-{len(history)}"
        answer = invoke(kernel, op)
        history.append({"operation": op, "result": answer})
        model.step(op, answer)
        audit_snapshot(kernel, model)
        return answer

    a = step(kind="create", owner="alice", shape=list(SHAPES[4]), ttl=30, key="same")["id"]
    b = step(kind="create", owner="bob", shape=list(SHAPES[0]), ttl=30, key="same")["id"]
    step(kind="clock", delta=20)
    job = step(kind="job", owner="alice", target=a, timeout=10)["id"]
    step(kind="clock", delta=10)
    assert model.jobs[job]["state"] == "Failed"  # TTL wins simultaneous success/timeout.
    assert model.allocations[b]["expires"] == 60  # Pending time did not consume lease TTL.
    step(kind="create", owner="alice", shape=list(SHAPES[4]), ttl=30, key="same")
    step(kind="renew", owner="alice", target=a, ttl=60)
    step(kind="release", owner="alice", target=b)  # Foreign mutation must fail.
    step(kind="release", owner="bob", target=b)
    step(kind="release", owner="bob", target=b)  # Repeated return must be inert.
    c = step(kind="create", owner="alice", shape=list(SHAPES[0]), ttl=60)["id"]
    j = step(kind="job", owner="alice", target=c, timeout=50)["id"]
    step(kind="node", state="Draining")
    step(kind="clock", delta=10)
    assert model.jobs[j]["state"] == "Succeeded"
    assert model.allocations[c]["state"] == "Allocated"  # Success does not return lease.
    step(kind="node", state="NotReady")
    step(kind="node", state="NotReady")
    step(kind="node", state="Ready")
    assert model.generation == 1 and model.allocations[c]["state"] == "Evicted"
    return {"history": history, "final": model.summary()}


def oracle_controls():
    """Deliberately corrupt histories; false observations must not pass the oracle."""
    setup = dict(kind="create", owner="alice", shape=list(SHAPES[0]), ttl=30)
    good = {"time": 0, "status": 201, "id": "opaque-a", "state": "Allocated", "expires": 30}
    initial = Reference()
    initial.step(setup, good)
    renew = dict(kind="renew", target="opaque-a", owner="alice", ttl=60)
    release = dict(kind="release", target="opaque-a", owner="alice")
    final_model = copy.deepcopy(initial)
    final_model.step(release, {"time": 0, "status": 202})
    # Impossible real-time order: the observed renewal success requires occurring
    # before release, but release has already returned before renewal is invoked.
    illegal = [
        {"operation": release, "result": {"time": 0, "status": 202}, "begin_ns": 1, "end_ns": 2},
        {"operation": renew, "result": {**good, "status": 200, "expires": 60},
         "begin_ns": 3, "end_ns": 4},
    ]
    assert not linearize(initial, illegal, final_model.summary())["linearizable"]
    overlap = copy.deepcopy(illegal)
    overlap[0]["end_ns"] = 4
    overlap[1]["begin_ns"] = 2
    # Legal reverse serialization produces expires=60, retained after release.
    legal_final = copy.deepcopy(initial)
    legal_final.step(renew, overlap[1]["result"])
    legal_final.step(release, overlap[0]["result"])
    assert linearize(initial, overlap, legal_final.summary())["linearizable"]
    rejected = 0
    for field, value in (("state", "Pending"), ("expires", 0), ("status", 202)):
        mutated = {**good, field: value}
        try:
            Reference().step(setup, mutated)
        except Mismatch:
            rejected += 1
    assert rejected == 3
    bad_final = initial.summary()
    bad_final["free"] = list(CAPACITY)  # Ignore the extant lease in the final snapshot.
    single = [{"operation": setup, "result": good, "begin_ns": 1, "end_ns": 2}]
    assert not linearize(Reference(), single, bad_final)["linearizable"]
    return {"real_time_violation_rejected": True, "overlap_reverse_order_accepted": True,
            "wrong_state_deadline_status_rejected": rejected,
            "corrupt_final_capacity_rejected": True}
