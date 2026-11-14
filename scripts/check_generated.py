"""Reject stale generated contracts without silently accepting a regenerated diff."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = [
    ROOT / "contracts/generated/contracts.schema.json",
    ROOT / "web/src/generated/contracts.ts",
]


def main() -> int:
    before = {p: p.read_bytes() if p.exists() else None for p in OUTPUTS}
    try:
        subprocess.run([sys.executable, "scripts/export_contracts.py"], cwd=ROOT, check=True)
        subprocess.run(["npm", "--prefix", "web", "run", "generate:contracts"], cwd=ROOT, check=True)
        changed = [str(p.relative_to(ROOT)) for p in OUTPUTS if before[p] != p.read_bytes()]
    finally:
        for path, data in before.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
    if changed:
        print("Generated contracts are stale. Run make contracts and commit both outputs:")
        print("\n".join(changed))
        return 1
    print("Generated Python schema / TypeScript contract parity verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
