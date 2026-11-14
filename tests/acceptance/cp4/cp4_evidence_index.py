"""Review actual CP4 evidence index evidence without manufacturing observations."""
from __future__ import annotations

import argparse
from pathlib import Path

from _delivery_review import ROOT, emit, evidence_index, review_check


def review(root: Path = ROOT) -> dict:
    return review_check(root, 'CP4-EVIDENCE-INDEX', 'figure_index_source.json', 'cp4-figure-index-v1', evidence_index)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare-index", action="store_true", help="Copy and audit historical bytes; never rerun an experiment")
    parser.add_argument("--historical-suite", type=Path)
    parser.add_argument("--probe-root", type=Path)
    parser.add_argument("--mcm-root", type=Path)
    parser.add_argument("--deep-audit", action="store_true", help="Recompute raw outcomes explicitly; normal reads only verify the current audit and all source hashes")
    args = parser.parse_args(argv)
    if args.prepare_index:
        if not all((args.historical_suite, args.probe_root, args.mcm_root)):
            parser.error("--prepare-index requires --historical-suite, --probe-root and --mcm-root")
        from _figure_index import prepare
        prepare(args.root, args.historical_suite, args.probe_root, args.mcm_root)
    elif args.deep_audit:
        from _figure_index import deep_audit
        deep_audit(args.root)
    return emit(review(args.root), args.output or args.root / "evidence/cp4/evidence_index_receipt.json")


if __name__ == "__main__":
    raise SystemExit(main())
