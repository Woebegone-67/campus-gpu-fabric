"""Run exactly 64 unchanged lifecycle flows once, with a diagnostic service wrapper."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "tests/acceptance/cp4"))
import _load_runner as runner  # noqa: E402

OUTPUT = HERE / "run-b19acae-64"
RUNTIME = Path("/tmp/fabric-phases-b19acae-64-20261115")
EVENTS = HERE / "service-events.json"


def main():
    if OUTPUT.exists() or RUNTIME.exists() or EVENTS.exists():
        raise RuntimeError("One-shot diagnostic output/runtime already exists; refusing a rerun")
    if runner.current_build(ROOT)["git_commit"] != "b19acae7f0ea185e6289388fb629d2a15dd84fad":
        raise RuntimeError("Unexpected source candidate")
    original_popen = subprocess.Popen

    def start(arguments, *args, **kwargs):
        arguments = list(arguments)
        factory = "campus_fabric.workflows.devhost:create_app"
        if factory in arguments:
            arguments[arguments.index(factory)] = "phase_service:create_app"
            environment = dict(kwargs["env"])
            environment["PYTHONPATH"] = os.pathsep.join((str(HERE), str(ROOT / "src"), str(ROOT)))
            environment["PHASE_EVENTS_PATH"] = str(EVENTS)
            kwargs["env"] = environment
        return original_popen(arguments, *args, **kwargs)

    # Only this process's child-service factory changes. The frozen Load.flow,
    # 128-node inventory, 32-flow windows, HTTP bodies and durability are reused.
    subprocess.Popen = start
    metadata = runner.execute(OUTPUT, jobs=64, deadline_seconds=300, runtime_directory=RUNTIME)
    metadata["diagnostic"] = {
        "kind": "INSTRUMENTED_DIAGNOSTIC_NOT_ACCEPTANCE",
        "predeclared_jobs": 64, "predeclared_maximum_jobs": 64,
        "writes": 320, "queries": 8,
        "service_wrapper": str(HERE / "phase_service.py"),
        "service_wrapper_sha256": hashlib.sha256((HERE / "phase_service.py").read_bytes()).hexdigest(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": "No automatic rerun; no production edits; timing instrumentation adds overhead",
    }
    runner.write_json(OUTPUT / "run.json", metadata)
    print(json.dumps({"diagnostic_finished": True, "completed_flows": len(metadata["completed_flows"]),
                      "elapsed_seconds": metadata["elapsed_seconds"], "errors": metadata["errors"],
                      "service_events_exist": EVENTS.exists(), "runtime": str(RUNTIME)}, indent=2))


if __name__ == "__main__":
    main()
