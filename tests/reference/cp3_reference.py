"""Small executable lease specification, independent of production implementation.

The reference deliberately has one homogeneous two-GPU node. It uses anonymous
resource tokens and integer time, not the implementation's placement, counters,
IDs, inventory functions or event reducer. IDs returned by creation are opaque
names only: all subsequent legality, ownership, deadlines and claims are derived
here. This is a bounded executable specification, not a universal proof.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

CAPACITY = (4, 8, 6, 2)  # CPU, RAM, disk, whole GPUs in the small test fixture
ACTIVE = {"Allocated", "Running"}
TERMINAL = {"Released", "Expired", "Evicted"}


class Mismatch(AssertionError):
    pass


@dataclass
class Reference:
    now: int = 0
    node: str = "Ready"
    generation: int = 0
    allocations: dict = field(default_factory=dict)
    jobs: dict = field(default_factory=dict)
    dedup: dict = field(default_factory=dict)
    serial: int = 0

    def free(self):
        if self.node != "Ready":
            return (0, 0, 0, 0)
        used = [0, 0, 0, 0]
        for a in self.allocations.values():
            if a["state"] in ACTIVE:
                used = [u + q for u, q in zip(used, a["demand"])]
        return tuple(c - u for c, u in zip(CAPACITY, used))

    def schedule(self):
        pending = sorted((a for a in self.allocations.values() if a["state"] == "Pending"),
                         key=lambda a: (-a["priority"], a["serial"]))
        for a in pending:
            if self.node != "Ready" or not a["compatible"]:
                continue
            if all(q <= f for q, f in zip(a["demand"], self.free())):
                a.update(state="Allocated", granted=self.now, expires=self.now + a["ttl"])

    def finish(self, job, state):
        job["state"] = state
        job["finished"] = self.now
        allocation = self.allocations[job["allocation"]]
        if allocation["state"] == "Running":
            allocation["state"] = "Allocated"

    def release(self, allocation, state):
        if allocation["state"] in TERMINAL:
            return False
        for job in self.jobs.values():
            if job["allocation"] == allocation["id"] and job["state"] == "Running":
                self.finish(job, "Cancelled" if state == "Released" else "Failed")
        allocation["state"] = state
        return True

    def tick(self, target):
        # Small integer model advances by unit ticks, not production event jumps.
        for time in range(self.now + 1, target + 1):
            self.now = time
            for a in self.allocations.values():
                if a["state"] in ACTIVE and a["expires"] <= time:
                    self.release(a, "Expired")
            for j in self.jobs.values():
                if j["state"] != "Running":
                    continue
                if j["timeout"] <= time:
                    self.finish(j, "Failed")
                elif j["complete"] <= time:
                    self.finish(j, "Succeeded")
            self.schedule()
        self.schedule()

    def summary(self, owner=None):
        return {
            "now": self.now,
            "node": self.node,
            "free": list(self.free()),
            "allocations": {k: {f: a[f] for f in ("owner", "state", "granted", "expires")}
                            for k, a in self.allocations.items()
                            if owner is None or a["owner"] == owner},
            "jobs": {k: {f: j[f] for f in ("owner", "state", "allocation", "finished")}
                     for k, j in self.jobs.items() if owner is None or j["owner"] == owner},
        }

    def step(self, op, observed):
        """Apply one command and compare its atomic reply with an observation."""
        if op["kind"] == "read":
            self._equal(self.summary(op.get("owner")), observed, "owner-scoped snapshot")
            return
        key = (op.get("owner"), op["kind"], op.get("key"))
        fingerprint = {k: v for k, v in op.items() if k not in {"request_id", "key"}}
        # The adapter passes these defaults explicitly to the API. Spell them
        # out here independently so omission and an explicit default agree.
        if op["kind"] == "create":
            fingerprint.setdefault("ttl", 30)
            fingerprint.setdefault("priority", 0)
        elif op["kind"] == "renew":
            fingerprint.setdefault("ttl", 30)
        elif op["kind"] == "job":
            fingerprint.setdefault("timeout", 50)
        if op.get("key") is not None and key in self.dedup:
            previous, answer = self.dedup[key]
            if previous == fingerprint:
                self._equal(answer, observed, "idempotent replay")
            else:
                self._equal(self.error("idempotency_conflict"), observed, "key conflict")
            return
        answer = self.apply(op, observed)
        self._equal(answer, observed, op["kind"])
        if op.get("key") is not None and "error" not in answer:
            self.dedup[key] = (copy.deepcopy(fingerprint), copy.deepcopy(answer))

    def error(self, code):
        return {"error": code, "time": self.now}

    def apply(self, op, observed):
        kind = op["kind"]
        answer = {"time": self.now, "status": 200}
        if kind == "create":
            ident = observed.get("id")
            if not ident or ident in self.allocations:
                raise Mismatch("Creation reused an allocation ID without an idempotent replay")
            self.serial += 1
            shape = op["shape"]
            a = dict(id=ident, owner=op["owner"], state="Pending", demand=tuple(shape[:4]),
                     compatible=(shape[3] == 0 or (shape[4] in {"any", "A10"}
                                                   and shape[5] <= 8)),
                     priority=op.get("priority", 0), ttl=op.get("ttl", 30),
                     serial=self.serial, granted=None, expires=None)
            self.allocations[ident] = a
            self.schedule()
            answer.update(id=ident, state=a["state"], expires=a["expires"],
                          status=201 if a["state"] == "Allocated" else 202)
        elif kind in {"release", "renew", "job"}:
            a = self.allocations.get(op["target"])
            if a is None or a["owner"] != op["owner"]:
                return self.error("lease_unavailable" if kind == "renew" else "not_found")
            if kind == "release":
                self.release(a, "Released")
                answer.update(status=202)
            elif kind == "renew":
                if a["state"] not in ACTIVE or a["expires"] <= self.now:
                    return self.error("lease_unavailable")
                a["expires"] = self.now + op.get("ttl", 30)
                answer.update(id=a["id"], state=a["state"], expires=a["expires"])
            else:
                if a["state"] != "Allocated" or a["expires"] <= self.now:
                    return self.error("lease_unavailable")
                ident = observed.get("id")
                if not ident or ident in self.jobs:
                    raise Mismatch("Creation reused job ID without idempotency")
                self.jobs[ident] = dict(owner=op["owner"], state="Running", allocation=a["id"],
                                       finished=None, complete=self.now + 10,
                                       timeout=self.now + op.get("timeout", 50))
                a["state"] = "Running"
                answer.update(id=ident, state="Running", status=201)
        elif kind == "cancel":
            j = self.jobs.get(op["target"])
            if j is None or j["owner"] != op["owner"]:
                return self.error("not_found")
            if j["state"] == "Running":
                self.finish(j, "Cancelled")
            answer.update(status=202)
        elif kind == "clock":
            self.tick(self.now + op["delta"])
            answer["time"] = self.now
        elif kind == "node":
            old, self.node = self.node, op["state"]
            if self.node == "NotReady" and old != self.node:
                self.generation += 1
                for a in self.allocations.values():
                    if a["state"] in ACTIVE:
                        self.release(a, "Evicted")
            answer.update(state=self.node, generation=self.generation)
        else:
            raise ValueError(kind)
        self.schedule()
        return answer

    @staticmethod
    def _equal(expected, actual, context):
        if expected != actual:
            raise Mismatch(f"{context}: expected {expected!r}; actual {actual!r}")


def linearize(initial, history, final, *, max_nodes=100000):
    """Find a legal serialization preserving every response-before-call edge.

    Searches all eligible permutations; never orders calls by implementation
    revision/event counters. Inconclusive exhaustion is an error, never PASS.
    """
    count = len(history)
    predecessors = []
    for event in history:
        mask = 0
        for i, other in enumerate(history):
            if other["end_ns"] < event["begin_ns"]:
                mask |= 1 << i
        predecessors.append(mask)
    visited = 0

    def visit(model, done, path):
        nonlocal visited
        visited += 1
        if visited > max_nodes:
            raise RuntimeError("Linearizability search budget exhausted; result inconclusive")
        if done == (1 << count) - 1:
            return path if model.summary() == final else None
        for i, event in enumerate(history):
            bit = 1 << i
            if done & bit or predecessors[i] & done != predecessors[i]:
                continue
            candidate = copy.deepcopy(model)
            try:
                candidate.step(event["operation"], event["result"])
            except Mismatch:
                continue
            result = visit(candidate, done | bit, path + [i])
            if result is not None:
                return result
        return None

    ordering = visit(copy.deepcopy(initial), 0, [])
    return {"linearizable": ordering is not None, "serialization": ordering,
            "search_nodes": visited,
            "real_time_edges": sum(p.bit_count() for p in predecessors),
            "overlapping_pairs": sum(
                left["begin_ns"] <= right["end_ns"] and right["begin_ns"] <= left["end_ns"]
                for i, left in enumerate(history) for right in history[i + 1:])}
