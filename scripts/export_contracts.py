"""Export the deterministic schema consumed by the TypeScript generator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from campus_fabric.contracts import ContractCatalog
from campus_fabric.contracts.models import AccountSession

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "contracts/generated/contracts.schema.json"
SESSION_OUTPUT = ROOT / "contracts/generated/account-session.schema.json"


def schema_text() -> str:
    schema = ContractCatalog.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:campus-fabric:internal-contracts:cp0"
    schema["description"] = (
        "CP0 internal contracts. Illustrative fixtures are not acceptance evidence. "
        "Cross-field Pydantic validators and kernel transaction checks are additional to JSON Schema."
    )
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def session_schema_text() -> str:
    schema = AccountSession.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:campus-fabric:account-session:v1"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail if the checked-in schema is stale"
    )
    args = parser.parse_args()
    outputs = {OUTPUT: schema_text(), SESSION_OUTPUT: session_schema_text()}
    if args.check:
        if any(not path.exists() or path.read_text(encoding="utf-8") != expected
               for path, expected in outputs.items()):
            print("Contract schema is stale: uv run python scripts/export_contracts.py")
            return 1
        print("Contract schema is current")
        return 0
    for path, expected in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        print(f"Exported {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
