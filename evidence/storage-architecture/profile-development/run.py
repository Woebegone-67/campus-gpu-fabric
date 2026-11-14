"""One bounded renewal profile in the disposable mechanism-probe database."""

import cProfile
import io
import json
import platform
import pstats
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from check_checkpoint import current_build

from campus_fabric.contracts import models as m
from campus_fabric.domain import placement
from campus_fabric.domain.kernel import Kernel


def function_name(key):
    filename, line, name = key
    return {"file": filename, "line": line, "function": name}


def main():
    output = Path(__file__).resolve().parent
    probe = json.loads((output.parent / "probe/result.json").read_text())
    database = Path(probe["runtime"]) / "state.sqlite3"
    assert database.is_file()
    build = current_build(ROOT)
    kernel = Kernel(database)
    actor = m.AuthenticatedActor(actor_id="profile-development", owner_id="profile-development",
                                  role="member")

    def commit(parameters, label):
        return kernel.commit(m.CommandEnvelope(request_id=f"profile-development-{label}",
            actor=actor, operation=parameters.kind, parameters=parameters))

    # Prepare exactly one lease outside the measured region; release it afterward.
    allocated = commit(m.AllocationCreateParameters(kind="allocation.create", owner_id=actor.owner_id,
        ttl_seconds=300, request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
            ephemeral_storage_mib=32, gpu=m.GPURequest(count=1, model="A10", memory_mib=4096))), "allocate")
    assert allocated.outcome == "committed", allocated
    aid = next(ref.entity_id for ref in allocated.objects if ref.kind == "allocation")
    renew = m.AllocationRenewParameters(kind="allocation.renew", allocation_id=aid, ttl_seconds=600)
    observed = {}
    models = Counter()
    original_save = kernel.store.save
    original_model = placement._model

    def observed_save(connection, state):
        observed["cache_counts_at_save"] = {
            name: len(container._cache) for name, container in state._containers.items()
        }
        original_save(connection, state)
        observed["write_stats"] = dict(kernel.store.last_write_stats)

    def observed_model(model, value):
        models[model.__name__] += 1
        return original_model(model, value)

    kernel.store.save = observed_save
    placement._model = observed_model
    profiler = cProfile.Profile()
    started = time.perf_counter()
    try:
        profiler.enable()
        receipt = commit(renew, "renew")
        profiler.disable()
    finally:
        profiler.disable()
        elapsed = time.perf_counter() - started
        kernel.store.save = original_save
        placement._model = original_model
        released = commit(m.AllocationTargetParameters(kind="allocation.release", allocation_id=aid), "release")
        assert released.outcome != "rejected", released
    assert receipt.outcome == "committed", receipt
    profiler.dump_stats(str(output / "renew.prof"))
    stats = pstats.Stats(profiler)
    for key, filename in (("time", "exclusive-top15.txt"), ("cumulative", "inclusive-top15.txt")):
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats(key).print_stats(15)
        (output / filename).write_text(stream.getvalue())

    rows = []
    for key, (primitive, total, exclusive, inclusive, callers) in stats.stats.items():
        rows.append({**function_name(key), "primitive_calls": primitive, "total_calls": total,
                     "exclusive_seconds": exclusive, "inclusive_seconds": inclusive,
                     "callers": [{**function_name(caller), "stats": list(values)}
                                 for caller, values in callers.items()]})
    exclusive = sorted(rows, key=lambda row: row["exclusive_seconds"], reverse=True)
    inclusive = sorted(rows, key=lambda row: row["inclusive_seconds"], reverse=True)
    relevant = [row for row in rows if (
        row["file"].endswith("storage/state.py") or
        row["function"] in {"loads", "dumps", "encode", "iterencode", "validate_json", "inventory"}
        or "validate_json" in row["function"]
    )]
    result = {
        "kind": "ONE_RENEWAL_FUNCTION_PROFILE", "build": build,
        "build_at_finish": current_build(ROOT), "database": str(database),
        "operation": "allocation.renew", "profiled_operations": 1,
        "setup_and_cleanup": "One allocation before profile, one release after profile; no history deleted",
        "receipt": receipt.model_dump(mode="json"), "profiled_elapsed_ms": elapsed * 1000,
        "profile_total_seconds": stats.total_tt, "total_calls": stats.total_calls,
        "primitive_calls": stats.prim_calls, "placement_model_calls": dict(models),
        **observed, "exclusive_top15": exclusive[:15], "inclusive_top15": inclusive[:15],
        "relevant_function_stats": relevant,
        "environment": {"platform": platform.platform(), "python": sys.version},
        "limitations": [
            "One cProfile sample, with interpreter profiler overhead; not a latency benchmark",
            "Full pytest and pre-existing CPU services were active on this shared host",
            "No services were stopped; compare call counts and attribution, not a formal CP4 result",
            "The in-memory placement model counter adds one lightweight wrapper per model conversion",
        ],
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in (
        "profiled_elapsed_ms", "profile_total_seconds", "total_calls", "placement_model_calls",
        "cache_counts_at_save", "write_stats",
    )}))
    print((output / "exclusive-top15.txt").read_text())
    print((output / "inclusive-top15.txt").read_text())


if __name__ == "__main__":
    main()
