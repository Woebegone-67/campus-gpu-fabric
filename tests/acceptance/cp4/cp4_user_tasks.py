"""Review actual CP4 user tasks evidence without manufacturing observations."""
from __future__ import annotations

import argparse
from pathlib import Path

from _delivery_review import ROOT, emit, review_check, user_tasks


def review(root: Path = ROOT) -> dict:
    return review_check(root, 'CP4-USER-TASKS', 'user_tasks_observer.json', 'cp4-user-tasks-v1', user_tasks)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    return emit(review(args.root), args.output or args.root / "evidence/cp4/user_tasks_receipt.json")


if __name__ == "__main__":
    raise SystemExit(main())
