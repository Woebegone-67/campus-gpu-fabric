"""Review actual CP4 video rehearsal evidence without manufacturing observations."""
from __future__ import annotations

import argparse
from pathlib import Path

from _delivery_review import ROOT, emit, review_check, video_rehearsal


def review(root: Path = ROOT) -> dict:
    return review_check(root, 'CP4-VIDEO-REHEARSAL', 'video_rehearsal_observer.json', 'cp4-video-rehearsal-v1', video_rehearsal)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    return emit(review(args.root), args.output or args.root / "evidence/cp4/video_rehearsal_receipt.json")


if __name__ == "__main__":
    raise SystemExit(main())
