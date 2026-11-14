#!/usr/bin/env python3
"""CP3 fixed P/B2 target; never drops negative seeds or changes thresholds."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from experiments.cp3_validation import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main("CP3-POLICY-TARGET"))
