"""Independent reducer for the private committed-state persistence journal.

Only Python's standard library is imported. No Kernel, state reducer, storage
change generator or production serializer is reused. This proves reconstruction
of persisted JSON, not the correctness of the domain transitions themselves.
"""
from __future__ import annotations

import copy
import hashlib
import json


def checksum(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def apply_change(state, change):
    op, path = change.get("op"), change.get("path")
    if not isinstance(path, list) or any(not isinstance(key, str) for key in path):
        raise ValueError("Invalid journal map path")
    expected_keys = {"op", "path", "value" if op == "set" else "values"}
    if op == "delete":
        expected_keys = {"op", "path"}
    if op not in {"set", "delete", "append"} or set(change) != expected_keys:
        raise ValueError("Unknown journal change")
    if not path:
        if op != "set" or not isinstance(change["value"], dict):
            raise ValueError("The journal root must be an object")
        return copy.deepcopy(change["value"])
    parent = state
    for key in path[:-1]:
        if not isinstance(parent, dict) or key not in parent:
            raise ValueError("Journal path does not exist")
        parent = parent[key]
    if not isinstance(parent, dict):
        raise ValueError("Journal parent must be a map")
    key = path[-1]
    if op == "set":
        parent[key] = copy.deepcopy(change["value"])
    elif op == "delete":
        if key not in parent:
            raise ValueError("Cannot remove a missing journal key")
        del parent[key]
    else:
        if not isinstance(parent.get(key), list) or not isinstance(change["values"], list):
            raise ValueError("Journal append requires lists")
        parent[key].extend(copy.deepcopy(change["values"]))
    return state


def replay(export, *, observed_prefixes=None):
    if export.get("schema") != "fabric-committed-state-v1":
        raise ValueError("Unknown committed-state journal schema")
    entries = export.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Journal has no initial state")
    state, previous = None, None
    compared = set()
    observed_prefixes = {} if observed_prefixes is None else observed_prefixes
    for number, entry in enumerate(entries, 1):
        if (entry.get("schema") != export["schema"] or entry.get("sequence") != number or
                entry.get("previous_record_sha256") != previous):
            raise ValueError("Missing, reordered or invalid journal record")
        if entry.get("origin") not in ({"initialize", "migration-baseline"} if number == 1
                                        else {"write"}):
            raise ValueError("Invalid journal origin")
        body = {key: value for key, value in entry.items() if key != "record_sha256"}
        if checksum(body) != entry.get("record_sha256"):
            raise ValueError("Journal record digest differs")
        if entry.get("before_sha256") != (checksum(state) if state is not None else None):
            raise ValueError("Journal before-state digest differs")
        if not isinstance(entry.get("changes"), list):
            raise ValueError("Journal changes must be a list")
        for change in entry["changes"]:
            state = apply_change(state, change)
        if not isinstance(state, dict) or checksum(state) != entry.get("after_sha256"):
            raise ValueError("Journal after-state digest differs")
        if number in observed_prefixes:
            if checksum(state) != observed_prefixes[number]:
                raise ValueError("Replay differs from independently observed committed state")
            compared.add(number)
        previous = entry["record_sha256"]
    if (len(entries) != export.get("head_sequence") or previous != export.get("head_record_sha256")
            or checksum(state) != export.get("state_sha256")):
        raise ValueError("Journal export was truncated or detached from state")
    if compared != set(observed_prefixes):
        raise ValueError("An observed commit is missing from the journal")
    return state
