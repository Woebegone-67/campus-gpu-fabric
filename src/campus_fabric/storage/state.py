"""Transaction-private lazy JSON entities and lossless normalized state cells."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, MutableMapping, MutableSequence

from campus_fabric.storage import journal

FILTER_FIELDS = (
    "state", "owner_id", "allocation_id", "intent_id", "status", "physical_live",
    "pending_terminal", "attempt_id", "action", "unavailable_until",
)


def unit_key(path):
    return journal.encode(list(path))


def materialize(value):
    """Detach a JSON value while its transaction connection is still open."""
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Out of range float values are not JSON compliant")
        return value
    if isinstance(value, Mapping):
        return {key: materialize(item) for key, item in value.items()}
    if isinstance(value, (list, LazyList)):
        return [materialize(item) for item in value]
    return json.loads(journal.encode(value))


def marker(value):
    if isinstance(value, Mapping):
        return ["map"]
    if isinstance(value, (list, LazyList)):
        return ["list", len(value)]
    return ["scalar", value]


def flatten(state):
    if not isinstance(state, dict):
        raise TypeError("Authoritative state must be an object")
    result = {}
    for name, value in state.items():
        if not isinstance(name, str):
            raise TypeError("JSON object keys must be strings")
        result[unit_key((name,))] = journal.encode(marker(value))
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                result[unit_key((name, key))] = journal.encode(materialize(item))
        elif isinstance(value, (list, LazyList)):
            for index, item in enumerate(value):
                result[unit_key((name, index))] = journal.encode(materialize(item))
    return result


def unflatten(units):
    """Strictly reconstruct all cells, including empty and unknown root fields."""
    roots, children = {}, {}
    for key, payload in units.items():
        path, value = json.loads(key), json.loads(payload)
        if (not isinstance(path, list) or len(path) not in (1, 2)
                or not isinstance(path[0], str) or journal.encode(path) != key
                or journal.encode(value) != payload):
            raise RuntimeError("Invalid canonical state cell")
        if len(path) == 1:
            roots[path[0]] = value
        else:
            children.setdefault(path[0], []).append((path[1], value))
    if children.keys() - roots.keys():
        raise RuntimeError("State cells lack a root marker")
    state = {}
    for name, shape in roots.items():
        entries = children.get(name, [])
        if shape == ["map"]:
            if any(not isinstance(key, str) for key, _ in entries):
                raise RuntimeError("Invalid map cell key")
            state[name] = dict(entries)
        elif (isinstance(shape, list) and len(shape) == 2 and shape[0] == "list"
              and type(shape[1]) is int and shape[1] >= 0):
            length = shape[1]
            if (len(entries) != length or any(type(key) is not int for key, _ in entries)
                    or sorted(key for key, _ in entries) != list(range(length))):
                raise RuntimeError("State list cells are not contiguous")
            state[name] = [value for _, value in sorted(entries)]
        elif (isinstance(shape, list) and len(shape) == 2 and shape[0] == "scalar"
              and not isinstance(shape[1], (dict, list)) and not entries):
            state[name] = shape[1]
        else:
            raise RuntimeError("Invalid state root marker")
    return state


def indexed_value(value, field):
    if not isinstance(value, dict):
        return None
    if field in ("attempt_id", "action") and isinstance(value.get("command"), dict):
        command = value["command"]
        if field == "action":
            return command.get("action")
        attempt = command.get("attempt")
        return attempt.get("attempt_id") if isinstance(attempt, dict) else None
    return value.get(field)


def index_columns(key, payload):
    path, value = json.loads(key), json.loads(payload)
    is_marker = len(path) == 1
    columns = [path[0], "marker" if is_marker else "map" if isinstance(path[1], str) else "list",
               None if is_marker or not isinstance(path[1], str) else path[1],
               path[1] if not is_marker and type(path[1]) is int else None]
    for field in FILTER_FIELDS:
        indexed = None if is_marker else indexed_value(value, field)
        if field == "unavailable_until":
            columns.append(indexed if type(indexed) in (int, float) else None)
        else:
            columns.append(None if is_marker else journal.encode(indexed))
    return columns


class LazyMap(MutableMapping):
    def __init__(self, connection, name):
        self.connection, self.name = connection, name
        self._cache, self._original, self._deleted = {}, {}, set()
        self._decoded_rows = 0
        self._detached = False

    def _read(self, key):
        if self._detached:
            return None
        row = self.connection.execute("SELECT payload FROM fabric_units WHERE key=?",
                                      (unit_key((self.name, key)),)).fetchone()
        return None if row is None else row[0]

    def __getitem__(self, key):
        if not isinstance(key, str) or key in self._deleted:
            raise KeyError(key)
        if key not in self._cache:
            payload = self._read(key)
            if payload is None:
                raise KeyError(key)
            self._original[key], self._cache[key] = payload, json.loads(payload)
            self._decoded_rows += 1
        return self._cache[key]

    def __setitem__(self, key, value):
        if not isinstance(key, str):
            raise TypeError("JSON object keys must be strings")
        if key not in self._original:
            self._original[key] = self._read(key)
        self._cache[key] = value
        self._deleted.discard(key)

    def __delitem__(self, key):
        self[key]
        self._deleted.add(key)
        del self._cache[key]

    def __iter__(self):
        if self._detached:
            return iter(sorted(self._cache))
        keys = {row[0] for row in self.connection.execute(
            "SELECT item_key FROM fabric_units WHERE root_key=? AND kind='map'", (self.name,))}
        return iter(sorted((keys | self._cache.keys()) - self._deleted))

    def __len__(self):
        if self._detached:
            return len(self._cache)
        count = self.connection.execute(
            "SELECT count(*) FROM fabric_units WHERE root_key=? AND kind='map'",
            (self.name,)).fetchone()[0]
        return (count + sum(self._original[key] is None for key in self._cache)
                - sum(self._original[key] is not None for key in self._deleted))

    def touched_values(self):
        return tuple(self._cache.values())

    def detach(self):
        """Keep object identity when this root is moved or aliased before save."""
        if not self._detached:
            for key in tuple(self):
                self[key]
            self._detached = True

    def values_where(self, **filters):
        predicates, parameters, tests = [], [self.name], []
        for expression, expected in filters.items():
            field, _, operation = expression.partition("__")
            operation = operation or "eq"
            if field not in FILTER_FIELDS or operation not in ("eq", "gt", "ge", "lt", "le"):
                raise ValueError("Unsupported entity filter")
            if operation != "eq":
                if field != "unavailable_until" or type(expected) not in (int, float):
                    raise ValueError("Range filtering requires a numeric unavailable_until")
                symbol = {"gt": ">", "ge": ">=", "lt": "<", "le": "<="}[operation]
                predicates.append(f"{field} {symbol} ?")
                parameters.append(expected)
            else:
                values = expected if isinstance(expected, tuple) else (expected,)
                if field == "unavailable_until":
                    nonnull = [value for value in values if value is not None]
                    if any(type(value) not in (int, float) for value in nonnull):
                        raise ValueError("Numeric entity filter required")
                    clauses = []
                    if nonnull:
                        clauses.append(f"{field} IN ({','.join('?' for _ in nonnull)})")
                        parameters.extend(nonnull)
                    if None in values:
                        clauses.append(f"{field} IS NULL")
                    predicates.append("(" + " OR ".join(clauses) + ")" if clauses else "0")
                else:
                    predicates.append(f"{field} IN ({','.join('?' for _ in values)})" if values else "0")
                    parameters.extend(journal.encode(value) for value in values)
            tests.append((field, operation, expected))
        query = "SELECT item_key FROM fabric_units WHERE root_key=? AND kind='map'"
        if predicates:
            query += " AND " + " AND ".join(predicates)
        keys = (set() if self._detached else
                {row[0] for row in self.connection.execute(query, parameters)})
        for key in sorted((keys | self._cache.keys()) - self._deleted):
            value = self[key]
            if all(self._matches(indexed_value(value, field), operation, expected)
                   for field, operation, expected in tests):
                yield value

    @staticmethod
    def _matches(actual, operation, expected):
        if operation == "eq":
            values = expected if isinstance(expected, tuple) else (expected,)
            return any(journal.equal(actual, item) for item in values)
        if type(actual) not in (int, float):
            return False
        if operation == "gt":
            return actual > expected
        if operation == "ge":
            return actual >= expected
        if operation == "lt":
            return actual < expected
        return actual <= expected

    def changed_units(self):
        result = {}
        for key in self._deleted:
            if self._original[key] is not None:
                result[unit_key((self.name, key))] = None
        for key, value in self._cache.items():
            payload = journal.encode(materialize(value))
            if payload != self._original[key]:
                result[unit_key((self.name, key))] = payload
        return result

    def rebase(self, changes):
        # save already encoded every changed value. Unchanged rows retain their
        # original payload; do not encode the full read cache a second time.
        self._original = {key: changes.get(unit_key((self.name, key)), self._original.get(key))
                          for key in self._cache}
        self._deleted.clear()


class LazyList(MutableSequence):
    def __init__(self, connection, name, length):
        self.connection, self.name = connection, name
        self._length = self._base_length = length
        self._cache, self._original = {}, {}
        self._decoded_rows = 0
        self._detached = False

    def __len__(self):
        return self._length

    def _index(self, index):
        if not isinstance(index, int):
            raise TypeError("List index must be an integer")
        index = index + self._length if index < 0 else index
        if not 0 <= index < self._length:
            raise IndexError(index)
        return index

    def _read(self, index):
        if self._detached:
            return None
        row = self.connection.execute("SELECT payload FROM fabric_units WHERE key=?",
                                      (unit_key((self.name, index)),)).fetchone()
        return None if row is None else row[0]

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._length))]
        index = self._index(index)
        if index not in self._cache:
            payload = self._read(index)
            if payload is None:
                raise RuntimeError("Missing persisted list element")
            self._original[index], self._cache[index] = payload, json.loads(payload)
            self._decoded_rows += 1
        return self._cache[index]

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            values = list(self)
            values[index] = value
            self._replace(values)
            return
        index = self._index(index)
        if index not in self._original:
            self._original[index] = self._read(index)
        self._cache[index] = value

    def __delitem__(self, index):
        values = list(self)
        del values[index]
        self._replace(values)

    def _replace(self, values):
        self._cache = dict(enumerate(values))
        self._length = len(values)
        for index in range(max(self._base_length, self._length)):
            if index not in self._original:
                self._original[index] = self._read(index)

    def insert(self, index, value):
        if index >= self._length:
            index = self._length
            if index not in self._original:
                self._original[index] = None if index >= self._base_length else self._read(index)
            self._cache[index] = value
            self._length += 1
            return
        values = list(self)
        values.insert(index, value)
        self._replace(values)

    def detach(self):
        if not self._detached:
            for index in range(self._length):
                self[index]
            self._detached = True

    def changed_units(self):
        result = {unit_key((self.name, index)): None
                  for index in range(self._length, self._base_length)}
        for index, value in self._cache.items():
            payload = journal.encode(materialize(value))
            if payload != self._original[index]:
                result[unit_key((self.name, index))] = payload
        return result

    def rebase(self, changes):
        self._original = {index: changes.get(unit_key((self.name, index)), self._original.get(index))
                          for index in self._cache}
        self._base_length = self._length


class StateDict(dict):
    """Root dictionary with lazy entities; only touched nested JSON is compared."""
    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self._markers, self._containers = {}, {}
        for name, payload in connection.execute(
                "SELECT root_key,payload FROM fabric_units WHERE kind='marker'"):
            shape = json.loads(payload)
            self._markers[name] = payload
            if shape == ["map"]:
                value = LazyMap(connection, name)
            elif shape[0] == "list":
                value = LazyList(connection, name, shape[1])
            else:
                value = shape[1]
            self[name] = value
            if isinstance(value, (LazyMap, LazyList)):
                self._containers[name] = value

    def __deepcopy__(self, memo):
        return materialize(self)

    def changed_units(self):
        result = {}
        # Replacing/deleting a root must also preserve containers retained by a
        # caller but absent from the current root dict. They may be reinserted
        # after this save; they must not follow the replacement's SQL rows.
        for name, container in self._containers.items():
            if self.get(name) is not container:
                container.detach()
        # A root may be moved via pop/update/setdefault, or aliased under another
        # name. Fix its current value before SQL deletion/replacement; retaining
        # the same container object keeps outstanding references coherent.
        for name, value in self.items():
            if isinstance(value, (LazyMap, LazyList)) and value.name != name:
                value.detach()
        for name in self._markers.keys() | self.keys():
            value = self.get(name)
            if (name in self and name in self._containers and self._containers[name] is value
                    and not value._detached):
                payload = journal.encode(marker(value))
                if self._markers[name] != payload:
                    result[unit_key((name,))] = payload
                result.update(value.changed_units())
            elif (name in self and name in self._markers
                  and not isinstance(value, (Mapping, list, LazyList))
                  and json.loads(self._markers[name])[0] == "scalar"):
                payload = journal.encode(marker(value))
                if self._markers[name] != payload:
                    result[unit_key((name,))] = payload
            else:
                before = dict(self.connection.execute(
                    "SELECT key,payload FROM fabric_units WHERE root_key=?", (name,)))
                after = flatten({name: value}) if name in self else {}
                result.update({key: after.get(key) for key in before.keys() | after.keys()
                               if before.get(key) != after.get(key)})
        return result

    def rebase(self, changes):
        self._markers = {name: journal.encode(marker(value)) for name, value in self.items()}
        self._containers = {name: container for name, container in self._containers.items()
                            if self.get(name) is container and not container._detached}
        for container in self._containers.values():
            container.rebase(changes)
