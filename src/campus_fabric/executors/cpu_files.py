"""Private execution evidence, not the authoritative allocation ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

ALGORITHM = "lcg64-indexed-v1"
MASK = (1 << 64) - 1


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def signed(value: dict, key: bytes) -> dict:
    return {
        "payload": value,
        "signature": hmac.new(key, canonical(value), hashlib.sha256).hexdigest(),
    }


def read_signed(path: Path, key: bytes) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError("Execution artifact is absent, symbolic or oversized")
    record = json.loads(path.read_bytes())
    if not isinstance(record, dict) or set(record) != {"payload", "signature"}:
        raise ValueError("Malformed execution artifact")
    expected = hmac.new(key, canonical(record["payload"]), hashlib.sha256).hexdigest()
    if not isinstance(record["signature"], str) or not hmac.compare_digest(
        expected, record["signature"]
    ):
        raise ValueError("Execution artifact signature mismatch")
    return record["payload"]


def advance(value: int, start: int, end: int) -> int:
    for index in range(start, end):
        value = (value * 1_664_525 + 1_013_904_223 + index) & MASK
    return value


def workload_id(config: dict) -> str:
    return hashlib.sha256(
        canonical(
            {
                "algorithm": ALGORITHM,
                "total_steps": config["total_steps"],
                "seed": config["seed"],
            }
        )
    ).hexdigest()
