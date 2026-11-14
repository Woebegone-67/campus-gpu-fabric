"""Frozen CP3 input generator. Future input stays exclusively in the runner."""
from __future__ import annotations

import json
import random
from pathlib import Path

FAMILIES = ("compatibility-fragmentation", "waves-multi-owner", "recall-save", "node-interleavings", "neutral-reverse")
VALIDATION_SEEDS = (101, 211, 307, 401, 503)
DEVELOPMENT_SEEDS = (0, 1)
POLICIES = ("B0", "B1", "B2", "P", "P-no-opportunity")
MODEL = {
    "name": "cp3-constant-work-v1", "duration_seconds": 10,
    "execution_mode": "logical_simulation", "dispatcher_quantum_seconds": 5,
    "max_candidates": 512, "horizon_seconds": 400, "works_per_run": 32,
    "service_unit": "completed_work_equivalents",
    "recovery_cost_component": "inactive: snapshot has no verified recovery-cost model",
    "duration_limit": "All jobs use the same declared 10 second duration. Resource-size mixtures are not duration mixtures; timeout is a failure deadline, never a duration estimate.",
    "scale_limit": "32 work inputs per run, below the blueprint suggested 256-512; bounded integration evidence, not a large-scale performance claim.",
}
LIMITATIONS = [
    MODEL["duration_limit"], MODEL["scale_limit"],
    "Logical checkpoint ready is an unverified report; simulated restart is not verified artifact continuation.",
    "Single-node bounded candidate enumeration does not prove global or multi-node feasibility.",
    "The kernel treats timeout as a whole-job absolute deadline: injected execution failure retries before that deadline, while a timeout itself terminates the job; timeout-then-retry is not claimed.",
    "No real CPU/GPU throughput, utilization or physical recovery measurement is claimed.",
    "Late callbacks use stable work and generation identities; absent targets are recorded as uncovered, not silently retargeted.",
]


def scenario_input(root: Path, family: str, seed: int, *, development: bool = False) -> dict:
    if family not in FAMILIES or type(seed) is not int:
        raise ValueError("Unknown family or invalid seed")
    if seed not in (DEVELOPMENT_SEEDS if development else VALIDATION_SEEDS):
        raise ValueError("Development and validation seeds are disjoint and fixed")
    rng = random.Random(f"cp3-input-v1:{family}:{seed}")
    inventory = json.loads((root / "configs/cp1_inventory.json").read_text())["nodes"]
    works = []
    for i in range(32):
        work = dict(intent_id=f"cp3-work-{i:02d}", owner_id=f"owner-{i % 8}", arrived_at=float(5 * (i // 8)),
                    gpu_model="any", gpu_count=1, gpu_memory_mib=1024, profile=None,
                    cpu_millicores=1000, memory_mib=1024, priority=0, ttl_seconds=300,
                    timeout_seconds=30, max_retries=0, max_restarts=2,
                    checkpoint_behavior="ready", checkpoint_timeout_seconds=3)
        if family == "compatibility-fragmentation":
            shape = i % 8
            work.update(gpu_model=("A10", "any", "A100", "A100", "A10", "H100", "any", "A100")[shape],
                        gpu_count=2 if shape in (0, 4) else 1,
                        profile="mig-2g.20gb" if shape == 3 else None,
                        gpu_memory_mib=20480 if shape in (2, 3, 7) else 1024,
                        cpu_millicores=rng.choice((1000, 4000, 8000)))
            # One owner alone gives an explicit zero-opportunity prefix.
            if i < 2:
                work.update(owner_id="owner-zero-prefix", arrived_at=0.)
            else:
                work["arrived_at"] += 5
        elif family == "waves-multi-owner":
            work.update(arrived_at=float((i // 8) * 20), gpu_model=rng.choice(("any", "A10", "A100")),
                        gpu_count=2 if i % 7 == 0 else 1,
                        cpu_millicores=4000 if i % 3 == 0 else 1000)
            if i in (6, 7, 14, 15):  # Same owner splits identical requests.
                work.update(owner_id="owner-split", gpu_model="any", gpu_count=1, cpu_millicores=1000)
            if i >= 24:
                work["owner_id"] = "owner-late" if i % 2 else f"owner-{i % 4}"
        elif family == "recall-save":
            work.update(gpu_model="A10", arrived_at=float(0 if i < 8 else 5 * (i // 8)),
                        checkpoint_behavior=("ready", "failed", "timeout")[i % 3], max_restarts=1 if i % 5 == 0 else 2,
                        memory_mib=rng.choice((1024, 2048)))
        elif family == "node-interleavings":
            work.update(gpu_model="A10", ttl_seconds=30 if i % 3 == 0 else 300,
                        timeout_seconds=3 if i % 4 == 0 else 30,
                        max_retries=1 if i % 4 == 0 else 0,
                        checkpoint_behavior="failed" if i % 5 == 0 else "ready",
                        memory_mib=rng.choice((1024, 2048)))
        else:
            # Fixed variants cover homogeneous contention, no contention, and scarce large requests.
            variant = (seed // 100) % 3 if not development else seed % 3
            if variant == 0:
                work.update(gpu_model="A10", arrived_at=0., cpu_millicores=rng.choice((1000, 2000)))
            elif variant == 1:
                work.update(gpu_model="A10", arrived_at=float(i * 11 + rng.choice((0, 1))))
            else:
                work.update(gpu_model="A10", arrived_at=float((i // 8) * 10),
                            gpu_count=2 if i % 8 == 0 else 1,
                            owner_id="owner-large" if i % 8 == 0 else f"owner-small-{i % 4}",
                            memory_mib=rng.choice((1024, 2048)))
        works.append(work)
    events = []
    if family == "recall-save":
        hold = rng.choice((15, 20, 25))
        second_at = 6 + hold
        events = [dict(at=2, kind="recall", device_ids=["sim-a10-gpu0", "sim-a10-gpu1"], notice_seconds=3, duration_seconds=hold),
                  dict(at=second_at, kind="recall", device_ids=["sim-a10-gpu0", "sim-a10-gpu1"], notice_seconds=1, duration_seconds=15),
                  dict(at=second_at+17, kind="recall", device_ids=["sim-a10-gpu0"], notice_seconds=2, duration_seconds=20)]
    if family == "node-interleavings":
        events = [dict(at=1, kind="failure_callback", intent_id="cp3-work-00", generation=1),
                  dict(at=5, kind="node_state", node_id="sim-a10", state="NotReady"),
                  dict(at=20+rng.choice((0, 2, 4)), kind="node_state", node_id="sim-a10", state="Ready"),
                  dict(at=25, kind="recall", device_ids=["sim-a10-gpu0", "sim-a10-gpu1"], notice_seconds=5, duration_seconds=10),
                  dict(at=35, kind="late_callback", intent_id="cp3-work-00", generation=1),
                  dict(at=60, kind="node_state", node_id="sim-a10", state="Draining"),
                  dict(at=70+rng.choice((0, 5, 10)), kind="node_state", node_id="sim-a10", state="Ready")]
    return dict(schema="cp3-input-v1", scenario_family=family, seed=seed,
                partition="development" if development else "validation", horizon=400,
                model=MODEL, works=works, events=events, inventory=inventory,
                cancellation_policy="count_until_horizon", service_unit=MODEL["service_unit"],
                scope="Isolated authoritative logical-kernel execution; not real CPU/GPU evidence",
                limitations=LIMITATIONS)
