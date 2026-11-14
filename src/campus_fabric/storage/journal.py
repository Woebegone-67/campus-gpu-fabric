"""Lossless JSON changes for the internal committed-state journal.

This is an internal persistence format, not the public domain event stream. It
records every state field, including workflow receipts and executor bookkeeping.
The independent acceptance reducer intentionally does not import this module.
"""
from __future__ import annotations

import hashlib
import json

SCHEMA = "fabric-committed-state-v1"


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def equal(left, right) -> bool:
    # JSON preserves bool/int and int/float distinctions in the state hash.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        # Signed zero has distinct canonical JSON bytes and state hashes.
        return left.hex() == right.hex()
    return left == right


def changes(before, after, path=()):
    """Map paths, append-only list tails, and exact replacement otherwise."""
    if type(before) is not type(after):
        return [{"op": "set", "path": list(path), "value": after}]
    if isinstance(before, dict):
        result = []
        for key in sorted(before.keys() - after.keys()):
            result.append({"op": "delete", "path": [*path, key]})
        for key in sorted(after):
            if key not in before:
                result.append({"op": "set", "path": [*path, key], "value": after[key]})
            else:
                result.extend(changes(before[key], after[key], (*path, key)))
        return result
    if isinstance(before, list):
        if len(after) >= len(before) and all(equal(a, b) for a, b in zip(before, after)):
            return ([{"op": "append", "path": list(path), "values": after[len(before):]}]
                    if len(after) > len(before) else [])
    elif equal(before, after):
        return []
    return [{"op": "set", "path": list(path), "value": after}]
