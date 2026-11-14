#!/usr/bin/env python3
"""Real HTTP regression for atomic administrative overview snapshots.

Two isolated modes each contain Alice's and Bob's completed work. Fifty
consecutive overview reads must succeed without retry while the CPU driver
keeps advancing its real clock. Independent read-only SQLite observations
check that GET creates no business command, event, or state change. This is
supplemental backend evidence, not an additional joint CP3 acceptance gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/acceptance/cp2"))

from accept_cp1_a import inventory_node, object_id, registration  # noqa: E402
from check_checkpoint import current_build  # noqa: E402
from cp2_cpu_reference import CpuHarness, process_cannot_execute  # noqa: E402
from cp3_state_replay import read_sql_state  # noqa: E402

from campus_fabric.executors.cpu_reference import CpuReferenceConfig  # noqa: E402
from campus_fabric.workflows.devhost import create_app  # noqa: E402

OVERVIEW = "/x/v1/admin/overview"
MODES = ("logical_simulation", "cpu_reference")
READS = 50


def create_overview_test_app():
    # The real product query assembly is retained. Explicit stage writes below
    # make the fixture deterministic; only the automatic workflow loop is off.
    return create_app(worker_enabled=False, cpu_config=CpuReferenceConfig(
        total_steps=50_000, batch_steps=1000, batch_pause_seconds=0.03, seed=17))


class OverviewHarness(CpuHarness):
    def __init__(self, directory, mode):
        super().__init__(directory)
        self.mode = mode
        self.app_target = "cp3_overview_snapshot:create_overview_test_app"
        self.env["FABRIC_MODE"] = mode
        self.env["PYTHONPATH"] = os.pathsep.join(
            (str(Path(__file__).resolve().parent), self.env["PYTHONPATH"]))
        nodes = []
        for index in range(2):
            node = inventory_node()
            node["node_id"] = f"overview-node-{index}"
            node["devices"][0]["device_id"] = f"overview-gpu-{index}"
            node["devices"][0]["physical_gpu_id"] = f"overview-gpu-{index}"
            nodes.append(node)
        (self.directory / "inventory.json").write_text(json.dumps({"nodes": nodes}))

    def check(self, name, observed):
        name = f"{self.mode}:{name}"
        self.checks.append({"id": name, "status": "PASS", "observed": observed})
        print(f"PASS {name}", flush=True)

    def make_work(self, actor):
        name = f"overview-{self.mode}-{actor}"
        spec = registration(name, settlement="release_on_success")
        spec["owner_id"] = self.owners[actor]
        spec["execution_mode"] = self.mode
        spec["specification"]["allocation"]["owner_id"] = self.owners[actor]
        version = self.request("POST", "/x/v1/work-intents", body=spec,
                               actor=actor, expected=202)[1]["version"]
        objects = {}
        for action, kind in (("allocate", "allocation"), ("create_job", "job")):
            for _ in range(10):
                status, receipt = self.request(
                    "POST", f"/x/v1/work-intents/{name}/actions", actor=actor,
                    body={"action": action, "stage_id": action, "expected_version": version},
                    expected=(200, 409))
                if status == 200:
                    break
                assert receipt["error"] == "stale_version", receipt
                version = self.get(f"/x/v1/work-intents/{name}", actor=actor)["version"]
            else:
                raise AssertionError("Ten consecutive optimistic writes became stale")
            version = receipt["version"]
            objects[kind] = object_id(receipt, kind)
        return {"actor": actor, "intent_id": name, **objects}

    def persisted(self):
        path = self.directory / "database/fabric.sqlite3"
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as connection:
            connection.execute("BEGIN")
            return read_sql_state(connection)

    def finish_owned_processes(self):
        if self.mode != "cpu_reference" or self.process is None:
            return
        # Both owners are local fixtures. Release through the public command
        # path before stopping the service; never infer worker exit from HTTP.
        if self.process.poll() is not None:
            self.stop()
            self.start()
        for actor in ("a", "b"):
            for allocation in self.get("/v1/allocations", actor=actor)["allocations"]:
                self.request("DELETE", f"/v1/allocations/{allocation['id']}",
                             actor=actor, expected=202)
        deadline = time.monotonic() + 10
        journal = self.directory / "database/cpu-executor/journal.json"
        while journal.exists():
            records = tuple(self.journal()["attempts"].values())
            if all(record["stop_confirmed"] for record in records):
                assert all(record["pid"] is None or process_cannot_execute(record["pid"])
                           for record in records)
                return
            assert time.monotonic() < deadline, "Owned CPU processes did not stop within 10s"
            time.sleep(0.05)


def business_state(state):
    # The real CPU clock legitimately commits clock.advanced events. Everything
    # else, including receipts, dedup keys, dispatch outbox and job records, must
    # remain identical once both workers have completed and settled.
    result = {key: value for key, value in state.items()
              if key not in {"now", "revision", "cpu_clock", "seq", "events"}}
    result["events"] = [event for event in state["events"]
                        if event["event_type"] != "clock.advanced"]
    return result


def validate_overview(view, expected=None):
    assert view["scope"] == "global" and view["owner_id"] is None
    assert view["now"] == view["logical_time"] and math.isfinite(view["now"])
    assert set(view["version"]) == {"epoch", "revision"}
    assert all(type(value) is int and value >= 0 for value in view["version"].values())
    assert view["counts"] == {"allocations": 2, "jobs": 2, "work_intents": 2}
    for family, count in (("allocations", "allocations"), ("jobs", "jobs"),
                          ("work", "work_intents")):
        counts = view[f"{family}_by_state"]
        assert all(type(value) is int and value > 0 for value in counts.values())
        assert sum(counts.values()) == view["counts"][count]
    assert view["allocations"] == view["jobs"] == 2
    for key, state in (("running_jobs", "Running"), ("succeeded_jobs", "Succeeded"),
                       ("failed_jobs", "Failed")):
        assert view[key] == view["jobs_by_state"].get(state, 0)
    assert view["measurement_kind"] == "authorized_record_counts"
    assert view["performance_measurements"] is None
    nodes = view["capacity"]["nodes"]
    assert len(nodes) == len({node["node_id"] for node in nodes}) == 2
    for node in nodes:
        assert node["observed_at"] == view["now"]
        assert all(0 <= node["available"][key] <= value
                   for key, value in node["capacity"].items())
    totals = view["capacity"]["totals"]
    for family in ("capacity", "available"):
        assert totals[family] == {key: sum(node[family][key] for node in nodes)
                                  for key in nodes[0][family]}
    if expected is not None:
        for family, records in (("allocations", "allocations"), ("jobs", "jobs"),
                                ("work", "intents")):
            assert view[f"{family}_by_state"] == dict(Counter(
                item["state"] for item in expected[records].values()))
        assert view["allocations_by_state"] == {"Released": 2}
        assert view["jobs_by_state"] == {"Succeeded": 2}
        assert view["work_by_state"] == {"completed": 2}
        assert totals["available"] == totals["capacity"]


def exercise(h):
    h.start()
    version = h.get("/v1/version")
    assert version["execution_mode"] == h.mode
    assert version["capabilities"]["real_cpu_execution"] == (h.mode == "cpu_reference")
    works = [h.make_work(actor) for actor in ("a", "b")]
    first = h.get(OVERVIEW, actor="admin")
    validate_overview(first)
    if h.mode == "logical_simulation":
        assert first["running_jobs"] == 2, first
        assert first["capacity"]["totals"]["available"]["gpu_count"] == 0
    else:
        # Real workers may finish between the two HTTP submissions on a busy
        # host. Require valid progress here; the logical mode above checks the
        # deterministic fully occupied snapshot, and both modes check final
        # settlement below.
        assert set(first["jobs_by_state"]) <= {"Running", "Succeeded"}, first
        assert first["running_jobs"] + first["succeeded_jobs"] == 2
    h.check("two-owner-snapshot", {"works": works, "overview": first})
    if h.mode == "logical_simulation":
        h.advance(10)
    deadline = time.monotonic() + 12
    for work in works:
        while True:
            intent = h.get("/x/v1/work-intents/" + work["intent_id"], actor=work["actor"])["intent"]
            if intent["state"] == "completed":
                break
            assert time.monotonic() < deadline, "Two-owner work did not finish within 12s"
            time.sleep(0.04)
    time.sleep(0.15)  # let the finite worker's final acknowledgment settle
    before = h.persisted()
    assert {value["owner_id"] for value in before["intents"].values()} == {"alice", "bob"}
    samples = []
    start = time.monotonic()
    for _ in range(READS):
        view = h.get(OVERVIEW, actor="admin")  # No retry on 409 or any other status.
        validate_overview(view, before)
        assert view["version"]["epoch"] == before["epoch"]
        if samples:
            assert view["version"]["revision"] >= samples[-1]["revision"]
            assert view["now"] >= samples[-1]["now"]
        samples.append({"request_id": view["request_id"], "now": view["now"],
                        "revision": view["version"]["revision"]})
        time.sleep(0.012)
    for actor in ("a", "b", "contributor"):
        h.request("GET", OVERVIEW, actor=actor, expected=403)
    after = h.persisted()
    assert business_state(before) == business_state(after), "GET changed business state"
    added = after["events"][len(before["events"]):]
    assert all(event["event_type"] == "clock.advanced" for event in added)
    assert after["revision"] - before["revision"] == len(added)
    assert after["seq"] - before["seq"] == len(added)
    if h.mode == "cpu_reference":
        assert samples[-1]["now"] > samples[0]["now"] + 0.4
        assert len({sample["revision"] for sample in samples}) >= 5
    else:
        assert before == after
        assert {sample["now"] for sample in samples} == {10.0}
    digest = hashlib.sha256(json.dumps(business_state(before), sort_keys=True).encode()).hexdigest()
    h.check("fifty-consecutive-read-only-overviews", {
        "count": READS, "all_statuses": 200, "retry_count": 0, "samples": samples,
        "elapsed_seconds": time.monotonic() - start,
        "business_state_before_after_equal": True, "business_state_sha256": digest,
        "allowed_clock_events": len(added), "business_commands_added": 0,
        "state_before_version": [before["epoch"], before["revision"]],
        "state_after_version": [after["epoch"], after["revision"]]})
    h.check("nonadmin-denied", {"alice": 403, "bob": 403, "supplier": 403})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "evidence/cp3/overview_snapshot_receipt.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build = current_build(ROOT)
    receipt = {"schema_version": "1.0.0", "check": "CP3-OVERVIEW-SNAPSHOT",
               "scope": "SUPPLEMENTAL_BACKEND_HTTP_REGRESSION", "status": "RUNNING",
               "created_at": datetime.now(timezone.utc).isoformat(), "build": build,
               "command": [sys.executable, str(Path(__file__).resolve()), "--output",
                           str(args.output.resolve())], "checks": [], "cases": [], "failure": None,
               "not_tested": ["joint CP3 gate", "B UI or human acceptance", "real GPU execution",
                              "arbitrary load or all possible concurrent schedules"]}
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    for mode in MODES:
        with tempfile.TemporaryDirectory(prefix="fabric-cp3-overview-") as directory:
            h = OverviewHarness(directory, mode)
            failure = None
            try:
                exercise(h)
            except Exception:
                failure = h.redact(traceback.format_exc())
            finally:
                try:
                    h.finish_owned_processes()
                except Exception:
                    failure = (failure or "") + "\nCleanup failed:\n" + h.redact(traceback.format_exc())
                finally:
                    h.stop()
            receipt["cases"].append({"mode": mode, "checks": h.checks,
                                     "history": h.history, "failure": failure})
            receipt["checks"].extend(h.checks)
            if failure:
                receipt["failure"] = failure
                break
    finish = current_build(ROOT)
    receipt.update(build_at_finish=finish, source_changed_during_run=build != finish,
                   status="FAIL" if receipt["failure"] else "STALE" if build != finish else "PASS")
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(f"{receipt['status']}: {args.output}")
    if receipt["failure"]:
        print(receipt["failure"], file=sys.stderr)
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
