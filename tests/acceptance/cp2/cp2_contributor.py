"""Execute actual HTTP evidence for CP2-CONTRIBUTOR, including its adversarial controls."""
import sys
from pathlib import Path

from cp2_all_operations import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        root = Path(__file__).resolve().parents[3]
        sys.argv.extend(["--output", str(root / "evidence/cp2/cp2_contributor_receipt.json")])
    raise SystemExit(main())
