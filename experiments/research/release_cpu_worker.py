"""Measurement-only launcher around the unchanged real CPU worker.

No pacing is introduced. The optional checkpoint write failure is a declared
fault injection, not a naturally observed storage failure.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from campus_fabric.executors import cpu_worker


def run(manifest: Path, fail_checkpoint: bool = False) -> None:
    started_wall = time.monotonic_ns()
    started_cpu = time.process_time_ns()
    advance = cpu_worker.advance
    atomic_json = cpu_worker.atomic_json
    measurements = {"compute": [], "writes": [], "fault": None}

    def measured_advance(value, start, end):
        before, cpu = time.monotonic_ns(), time.process_time_ns()
        result = advance(value, start, end)
        measurements["compute"].append({
            "start_step": start, "end_step": end,
            "start_ns": before, "end_ns": time.monotonic_ns(),
            "cpu_ns": time.process_time_ns() - cpu,
        })
        return result

    def measured_write(path, value):
        before, cpu = time.monotonic_ns(), time.process_time_ns()
        payload = value.get("payload", {})
        is_checkpoint = payload.get("kind") == "checkpoint"
        if fail_checkpoint and is_checkpoint:
            measurements["fault"] = {
                "kind": "injected_checkpoint_write_error", "at_ns": before,
                "step": payload["state"]["step"], "bytes_written": 0,
            }
            raise OSError("preregistered research checkpoint write failure")
        atomic_json(path, value)
        measurements["writes"].append({
            "filename": path.name, "kind": payload.get("kind", path.stem),
            "step": payload.get("state", {}).get("step", payload.get("initial_step")),
            "start_ns": before, "end_ns": time.monotonic_ns(),
            "cpu_ns": time.process_time_ns() - cpu, "bytes": path.stat().st_size,
        })

    cpu_worker.advance = measured_advance
    cpu_worker.atomic_json = measured_write
    try:
        cpu_worker.run(manifest)
    finally:
        measurements.update(
            worker_start_ns=started_wall, worker_end_ns=time.monotonic_ns(),
            worker_cpu_ns=time.process_time_ns() - started_cpu,
            pacing_seconds=0,
        )
        (manifest.parent / "research-measurements.json").write_text(
            json.dumps(measurements, sort_keys=True) + "\n"
        )


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve(), len(sys.argv) > 2 and sys.argv[2] == "fail-save")
