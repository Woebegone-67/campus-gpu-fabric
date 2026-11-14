"""An actual, deliberately narrow CPU reference process.

It never executes a submitted image, shell command or environment variable.
Pacing makes interruption observable; the integer computation is deterministic.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path

if __package__:
    from .cpu_files import ALGORITHM, advance, atomic_json, read_signed, signed, workload_id
else:
    from cpu_files import ALGORITHM, advance, atomic_json, read_signed, signed, workload_id


def run(manifest_path: Path) -> None:
    # The lock spans all workload effects. Recovery may cancel a launch that
    # has not acquired it yet; a delayed child must then exit without working.
    with (manifest_path.parent / "launch.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            _run_locked(manifest_path)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _run_locked(manifest_path: Path) -> None:
    directory = manifest_path.parent
    key = (directory.parent.parent / "secret.key").read_bytes()
    manifest = read_signed(manifest_path, key)
    config = manifest["config"]
    identity = {name: manifest[name] for name in ("owner_id", "epoch", "attempt", "token")}
    cancelled = directory / "launch-cancelled.json"
    if cancelled.exists():
        if read_signed(cancelled, key) != identity:
            raise ValueError("Launch cancellation targets another execution")
        return
    if (directory / "started.json").exists():
        raise ValueError("This attempt already executed; refusing a second launch")
    started_ns = time.monotonic_ns()
    pid = os.getpid()
    base = {
        **identity,
        "pid": pid,
        "algorithm": ALGORITHM,
        "workload_id": workload_id(config),
        "started_monotonic_ns": started_ns,
    }
    step, value = manifest["initial"]["step"], manifest["initial"]["value"]
    seen_controls = set()

    def save(name, payload):
        atomic_json(directory / name, signed({**base, **payload}, key))

    save("started.json", {"initial_step": step, "resumed_from": manifest.get("resumed_from")})
    while True:
        for control_path in sorted(directory.glob("control-*.json")):
            control = read_signed(control_path, key)
            if control["token"] != manifest["token"]:
                raise ValueError("Control message targets another execution")
            if control["dispatch_id"] not in seen_controls:
                seen_controls.add(control["dispatch_id"])
                if control["action"] == "checkpoint":
                    save(
                        control["filename"],
                        {
                            "kind": "checkpoint",
                            "dispatch_id": control["dispatch_id"],
                            "state": {"step": step, "value": value},
                            "total_steps": config["total_steps"],
                        },
                    )
                elif control["action"] == "stop":
                    save(
                        "stopped.json",
                        {
                            "kind": "stopped",
                            "state": {"step": step, "value": value},
                            "finished_monotonic_ns": time.monotonic_ns(),
                        },
                    )
                    return
        if step >= config["total_steps"]:
            save(
                "result.json",
                {
                    "kind": "result",
                    "state": {"step": step, "value": value},
                    "total_steps": config["total_steps"],
                    "finished_monotonic_ns": time.monotonic_ns(),
                },
            )
            return
        end = min(step + config["batch_steps"], config["total_steps"])
        value = advance(value, step, end)
        step = end
        save(
            "progress.json",
            {"state": {"step": step, "value": value}, "observed_monotonic_ns": time.monotonic_ns()},
        )
        if config["batch_pause_seconds"]:
            time.sleep(config["batch_pause_seconds"])


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
