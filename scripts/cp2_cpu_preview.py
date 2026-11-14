"""Explicitly paced real CPU reference instance for human/browser CP2 demos.

The worker computes 1,000,000 deterministic integer steps, sleeping 50 ms between
1,000-step batches (about 50 seconds uninterrupted). This is declared demo pacing,
not measured hardware throughput or a simulated successful callback. Save/stop/
resume/result all use A's actual adapter. The regular devhost default is unchanged.
"""
from campus_fabric.executors.cpu_reference import CpuReferenceConfig
from campus_fabric.workflows.devhost import create_app as create_host


def create_app():
    return create_host(execution_mode="cpu_reference", cpu_config=CpuReferenceConfig(
        total_steps=1_000_000, batch_steps=1_000, batch_pause_seconds=0.05, seed=17,
    ))
