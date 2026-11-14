"""Run one registered CP2 sample in an independent temporary Kernel.

python -m experiments.run_scenarios --scenario cp2-contention-v1 --policy P --seed 0
No operating API, test token, database path, command or arbitrary scenario is accepted.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from campus_fabric.contracts.models import AuthenticatedActor
from campus_fabric.workflows.experiment_runner import POLICIES, ExperimentService, catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=[s["scenario_id"] for s in catalog()], required=True)
    parser.add_argument("--policy", choices=POLICIES, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path("evidence/local/cp2-experiments"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    service = ExperimentService(root, args.output)
    try:
        record = service.submit(actor=AuthenticatedActor(actor_id="experiment-cli", role="administrator"),
                                scenario_id=args.scenario, policy=args.policy, seed=args.seed)
        result = service.wait(record.run_id)
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "completed" else 1
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
