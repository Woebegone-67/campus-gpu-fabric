"""Bounded host mechanism probe; does not satisfy the CP4 reference load gate."""

import argparse
import gzip
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from check_checkpoint import current_build

from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.storage.sqlite import SQLiteStore
from campus_fabric.workflows.devhost import WorkflowWorker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source / "final_state.json.gz"
    initial = json.load(gzip.open(source, "rt"))
    assert len(initial["jobs"]) == 256
    runtime = Path(tempfile.mkdtemp(prefix="fabric-entity-probe-"))
    path = runtime / "state.sqlite3"
    store = SQLiteStore(path)
    store.initialize(initial)
    store.close()
    kernel = Kernel(path)
    build = current_build(ROOT)
    measures = []
    touched = []
    save = kernel.store.save

    def observed_save(conn, state):
        count = {name: len(container._cache)
                 for name, container in state._containers.items()}
        save(conn, state)
        touched.append({"entity_cache_counts": count,
                        "write_stats": getattr(kernel.store, "last_write_stats", None)})

    kernel.store.save = observed_save

    def measure(label, call):
        touched.clear()
        start = time.perf_counter()
        result = call()
        measures.append({"operation": label, "elapsed_ms": 1000 * (time.perf_counter() - start),
                         "transactions": list(touched)})
        return result

    def flow(number, measured):
        actor = m.AuthenticatedActor(actor_id=f"owner-{number % 64:03}",
                                    owner_id=f"owner-{number % 64:03}", role="researcher")

        def call(label, parameters):
            command = m.CommandEnvelope(request_id=f"entity-probe-{number}-{label}",
                actor=actor, operation=parameters.kind, parameters=parameters)
            result = (measure(label, lambda: kernel.commit(command)) if measured
                      else kernel.commit(command))
            assert result.outcome != "rejected", result
            return result

        receipt = call("allocate", m.AllocationCreateParameters(kind="allocation.create",
            owner_id=actor.owner_id, ttl_seconds=300, request=m.ResourceRequest(
                cpu_millicores=1000, memory_mib=1024, ephemeral_storage_mib=32,
                gpu=m.GPURequest(count=1, model="A10", memory_mib=4096))))
        aid = next(ref.entity_id for ref in receipt.objects if ref.kind == "allocation")
        receipt = call("create_job", m.JobCreateParameters(kind="job.create", allocation_id=aid,
            image="cp4-logical-fixed-model", command=("work",), timeout_seconds=100, max_retries=0))
        jid = next(ref.entity_id for ref in receipt.objects if ref.kind == "job")
        call("renew", m.AllocationRenewParameters(kind="allocation.renew", allocation_id=aid,
                                                 ttl_seconds=600))
        call("cancel", m.JobCancelParameters(kind="job.cancel", job_id=jid))
        call("release", m.AllocationTargetParameters(kind="allocation.release", allocation_id=aid))
        return actor, jid

    flow(1000, False)
    for number in (1001, 1002):
        actor, jid = flow(number, True)
    query = m.ReadQuery(actor=actor, entities=(m.EntityRef(kind="job", entity_id=jid),))
    for _ in range(3):
        measure("selected_job_record_read", lambda: kernel.record_snapshot(query))
    measure("inactive_worker_tick", lambda: WorkflowWorker(kernel).tick())
    final = kernel.store.read()
    assert len(final["jobs"]) == 259
    assert final["events"][:len(initial["events"])] == initial["events"]
    for collection in ("jobs", "allocations", "intents", "decisions", "dedup", "payloads"):
        assert all(final[collection][key] == value for key, value in initial[collection].items())
    with kernel.store._connect() as conn:
        durability = {name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                      for name in ("journal_mode", "synchronous")}
    result = {"kind": "HOST_MECHANISM_DIAGNOSTIC", "build": build,
        "build_at_finish": current_build(ROOT), "runtime": str(runtime),
        "platform": platform.platform(), "python": sys.version,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "initial_jobs": 256, "final_jobs": 259, "history_preserved": True,
        "durability": durability, "measurements": measures,
        "median_write_ms": statistics.median(row["elapsed_ms"] for row in measures[:10]),
        "limitations": ["Small host diagnostic, not 4 vCPU / 8 GiB CP4 acceptance",
                        "No paired same-environment baseline speed ratio is claimed",
                        "GPU inventory simulated; no policy effectiveness result"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "median_write_ms": result["median_write_ms"],
                      "history_preserved": True, "source_unchanged": build == result["build_at_finish"]}))


if __name__ == "__main__":
    main()
