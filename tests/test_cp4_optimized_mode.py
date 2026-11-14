"""An optimized interpreter must fail before it can issue an acceptance result."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = [
    ("scripts/check_checkpoint.py", "--checkpoint", "CP4"),
    ("scripts/accept_cp4.py", "--collect-only"),
    ("tests/acceptance/cp4/cp4_scale.py", "--review-only"),
    ("tests/acceptance/cp4/cp4_latency.py", "--review-only"),
    ("tests/acceptance/cp4/cp4_reliability.py", "--review-only"),
    ("tests/acceptance/cp4/cp4_clean_deploy.py", "--review-only"),
    ("tests/acceptance/cp4/cp4_user_tasks.py", "--review-only"),
    ("tests/acceptance/cp4/cp4_evidence_index.py", "--review-only"),
    ("tests/acceptance/cp4/cp4_video_rehearsal.py", "--review-only"),
]


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("mode", ["-O", "-OO", "environment"])
def test_optimized_acceptance_cannot_report_pass(entrypoint, mode):
    env = os.environ.copy()
    env.pop("PYTHONOPTIMIZE", None)
    flags = [mode] if mode != "environment" else []
    if mode == "environment":
        env["PYTHONOPTIMIZE"] = "1"
    result = subprocess.run(
        [sys.executable, *flags, *entrypoint], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "Acceptance validation requires Python assertions" in result.stderr
    assert not result.stdout.strip(), "Optimized validation must not emit a receipt or verdict"
