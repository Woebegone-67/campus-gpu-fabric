"""Independent reducer for the private committed-state persistence journal.

Only Python's standard library is imported. No Kernel, state reducer, storage
change generator or production serializer is reused. This proves reconstruction
of persisted JSON, not the correctness of the domain transitions themselves.
"""
from __future__ import annotations

import copy
import hashlib
import json

V1 = "fabric-committed-state-v1"
V2 = "fabric-committed-state-v2"
EMPTY_ROOT = hashlib.sha256(b"fabric-state-empty-v2").hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def checksum(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def cells_from_state(state):
    """Independent, exhaustive flattening; no business-field allowlist."""
    if not isinstance(state, dict) or any(not isinstance(key, str) for key in state):
        raise ValueError("The state root must be a JSON object")
    result = {}
    for name, value in state.items():
        key = canonical([name])
        if isinstance(value, dict):
            if any(not isinstance(child, str) for child in value):
                raise ValueError("JSON object keys must be strings")
            result[key] = ["map"]
            for child, payload in value.items():
                result[canonical([name, child])] = copy.deepcopy(payload)
        elif isinstance(value, list):
            result[key] = ["list", len(value)]
            for index, payload in enumerate(value):
                result[canonical([name, index])] = copy.deepcopy(payload)
        else:
            result[key] = ["scalar", copy.deepcopy(value)]
    canonical(result)  # Reject non-JSON values and nonfinite numbers.
    return result


def cell_path(key):
    if not isinstance(key, str):
        raise ValueError("A cell key must be a canonical JSON path")
    try:
        path = json.loads(key)
    except ValueError as error:
        raise ValueError("Invalid cell key") from error
    if (not isinstance(path, list) or len(path) not in (1, 2)
            or not isinstance(path[0], str) or canonical(path) != key):
        raise ValueError("A cell key must be a canonical one/two-part path")
    if len(path) == 2 and not (isinstance(path[1], str)
                              or (type(path[1]) is int and path[1] >= 0)):
        raise ValueError("Invalid cell child key")
    return path


def state_from_cells(cells):
    """Reconstruct every SQL cell, rejecting orphans and malformed containers."""
    if not isinstance(cells, dict):
        raise ValueError("Cells must be a map")
    parents, children = {}, {}
    for key, value in cells.items():
        path = cell_path(key)
        if len(path) == 1:
            parents[path[0]] = value
        else:
            children.setdefault(path[0], []).append((path[1], value))
    if set(children) - set(parents):
        raise ValueError("Cell has no top-level marker")
    state = {}
    for name, marker in parents.items():
        rows = children.get(name, [])
        if marker == ["map"]:
            if any(not isinstance(key, str) for key, _ in rows):
                raise ValueError("Map cell children require string keys")
            state[name] = {key: copy.deepcopy(value) for key, value in rows}
        elif (isinstance(marker, list) and len(marker) == 2 and marker[0] == "list"
              and type(marker[1]) is int and marker[1] >= 0):
            if (any(type(key) is not int for key, _ in rows)
                    or {key for key, _ in rows} != set(range(marker[1]))):
                raise ValueError("List cells must exactly cover their declared length")
            state[name] = [copy.deepcopy(value) for _, value in sorted(rows)]
        elif (isinstance(marker, list) and len(marker) == 2 and marker[0] == "scalar"
              and not isinstance(marker[1], (dict, list)) and not rows):
            state[name] = copy.deepcopy(marker[1])
        else:
            raise ValueError("Invalid top-level cell marker")
    canonical(state)
    return state


def cell_root(cells):
    """Build a Cartesian tree from sorted cells, independently of online updates.

    A monotone priority stack and postorder traversal avoid reusing the producer's
    mutable treap rotations or trusting its cached tree rows/root.
    """
    nodes, stack = {}, []
    for key in sorted(cells):
        cell_path(key)
        payload = canonical(cells[key]).encode()
        priority = (hashlib.sha256(b"fabric-state-priority-v2\0" + key.encode()).hexdigest(), key)
        value_hash = hashlib.sha256(b"fabric-state-value-v2\0" + payload).hexdigest()
        node = {"priority": priority, "value_hash": value_hash, "left": None, "right": None}
        last = None
        while stack and nodes[stack[-1]]["priority"] > priority:
            last = stack.pop()
        node["left"] = last
        nodes[key] = node
        if stack:
            nodes[stack[-1]]["right"] = key
        stack.append(key)
    if not stack:
        return EMPTY_ROOT
    root = stack[0]
    pending, hashes = [(root, False)], {}
    while pending:
        key, visited = pending.pop()
        node = nodes[key]
        if not visited:
            pending.append((key, True))
            for child in (node["left"], node["right"]):
                if child is not None:
                    pending.append((child, False))
        else:
            payload = canonical([key, node["value_hash"],
                                 hashes.get(node["left"], EMPTY_ROOT),
                                 hashes.get(node["right"], EMPTY_ROOT)]).encode()
            hashes[key] = hashlib.sha256(b"fabric-state-node-v2\0" + payload).hexdigest()
    return hashes[root]


def read_sql_state(connection):
    """Observe authority through SQL, never production serializers or tree caches."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "fabric_units" in tables:
        cells = {}
        for key, payload in connection.execute("SELECT key,payload FROM fabric_units"):
            if key in cells:
                raise ValueError("Duplicate persisted cell")
            value = json.loads(payload)
            if canonical(value) != payload:
                raise ValueError("Persisted cell payload is not canonical JSON")
            cells[key] = value
        return state_from_cells(cells)
    if "fabric_state" in tables:
        row = connection.execute("SELECT payload FROM fabric_state WHERE singleton=1").fetchone()
        return None if row is None else json.loads(row[0])
    return None


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


def replay(export, *, observed_prefixes=None, expected_head=None):
    """Verify a journal against independent observations and optional fixed anchor.

    Hashes are not signatures. ``expected_head`` must come from outside the
    candidate export; changing the journal and all its self-reported hashes does
    not authenticate a history. A caller may pin sequence, record, state/root.
    """
    if export.get("schema") not in {V1, V2}:
        raise ValueError("Unknown committed-state journal schema")
    entries = export.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Journal has no initial state")
    state, previous, cells, seen_v2 = None, None, None, False
    compared = set()
    observed_prefixes = {} if observed_prefixes is None else observed_prefixes
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError("Journal record must be an object")
        schema = entry.get("schema")
        if (schema not in {V1, V2} or (export["schema"] == V1 and schema != V1)
                or (seen_v2 and schema == V1) or type(entry.get("sequence")) is not int
                or entry.get("sequence") != number or
                entry.get("previous_record_sha256") != previous):
            raise ValueError("Missing, reordered or invalid journal record")
        expected_keys = {"schema", "sequence", "origin", "previous_record_sha256", "changes", "record_sha256"}
        expected_keys |= {"before_sha256", "after_sha256"} if schema == V1 else {"before_root", "after_root"}
        if set(entry) != expected_keys:
            raise ValueError("Unexpected journal record fields")
        if entry.get("origin") not in ({"initialize", "migration-baseline"} if number == 1
                                        else {"write"}):
            raise ValueError("Invalid journal origin")
        body = {key: value for key, value in entry.items() if key != "record_sha256"}
        if checksum(body) != entry.get("record_sha256"):
            raise ValueError("Journal record digest differs")
        if not isinstance(entry.get("changes"), list):
            raise ValueError("Journal changes must be a list")
        if schema == V1:
            if entry["before_sha256"] != (checksum(state) if state is not None else None):
                raise ValueError("Journal before-state digest differs")
            for change in entry["changes"]:
                state = apply_change(state, change)
            if not isinstance(state, dict) or checksum(state) != entry["after_sha256"]:
                raise ValueError("Journal after-state digest differs")
        else:
            if not seen_v2:
                cells = {} if state is None else cells_from_state(state)
            before_root = None if state is None else cell_root(cells)
            if entry["before_root"] != before_root:
                raise ValueError("Journal before-state root differs")
            changed = set()
            for change in entry["changes"]:
                if not isinstance(change, dict):
                    raise ValueError("Cell change must be an object")
                op, key = change.get("op"), change.get("key")
                fields = {"op", "key", "value"} if op == "set" else {"op", "key"}
                if op not in {"set", "delete"} or set(change) != fields:
                    raise ValueError("Unknown cell change")
                cell_path(key)
                if key in changed:
                    raise ValueError("Duplicate cell change")
                changed.add(key)
                if op == "set":
                    cells[key] = copy.deepcopy(change["value"])
                elif key not in cells:
                    raise ValueError("Cannot remove a missing cell")
                else:
                    del cells[key]
            state = state_from_cells(cells)
            if cell_root(cells) != entry["after_root"]:
                raise ValueError("Journal after-state root differs")
            seen_v2 = True
        if number in observed_prefixes:
            if checksum(state) != observed_prefixes[number]:
                raise ValueError("Replay differs from independently observed committed state")
            compared.add(number)
        previous = entry["record_sha256"]
    if (type(export.get("head_sequence")) is not int
            or len(entries) != export.get("head_sequence") or previous != export.get("head_record_sha256")
            or checksum(state) != export.get("state_sha256")):
        raise ValueError("Journal export was truncated or detached from state")
    if export["schema"] == V2 and cell_root(cells_from_state(state)) != export.get("state_root"):
        raise ValueError("Journal export state root differs")
    if compared != set(observed_prefixes):
        raise ValueError("An observed commit is missing from the journal")
    if expected_head is not None:
        allowed = {"head_sequence", "head_record_sha256", "state_sha256", "state_root"}
        if (not isinstance(expected_head, dict)
                or not {"head_sequence", "head_record_sha256"}.issubset(expected_head)
                or set(expected_head) - allowed
                or any(export.get(key) != value for key, value in expected_head.items())):
            raise ValueError("Journal differs from the independently pinned head")
    return state
