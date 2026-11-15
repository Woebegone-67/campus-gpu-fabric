"""Train a new immutable availability model from an audited observation export.

Example: python -m experiments.research.train_availability_model --observations rows.json
  --cutoff 1000000 --output evidence/models/run-001
Never promotes a model, edits live history, or applies a scheduling command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from campus_fabric.policies.availability_model import train_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--cutoff", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.observations.read_bytes()
    data = json.loads(raw)
    if data.get("source_kind") not in {"synthetic", "observed_export"} or not data.get("namespace"):
        parser.error("Observation export needs source_kind and a clock/data namespace")
    rows = [row for row in data["rows"] if row["label_observed_at"] <= args.cutoff]
    if args.output.exists():
        parser.error("Use a new output directory; an accepted model is never overwritten")
    artifact = train_model(rows)
    artifact["provenance"] = {"source_kind": data["source_kind"], "namespace": data["namespace"],
                              "export_sha256": hashlib.sha256(raw).hexdigest(),
                              "cutoff": args.cutoff, "excluded_later_rows": len(data["rows"]) - len(rows)}
    args.output.mkdir(parents=True, exist_ok=False)
    encoded = (json.dumps(artifact, indent=2, allow_nan=False) + "\n").encode()
    (args.output / "model.json").write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    (args.output / "model.sha256").write_text(digest + "\n")
    print(json.dumps({"model": str(args.output / "model.json"), "sha256": digest,
                      "training_rows": len(rows), "trained_through": artifact["trained_through"],
                      "production_validation": "NOT_RUN"}))


if __name__ == "__main__":
    main()
