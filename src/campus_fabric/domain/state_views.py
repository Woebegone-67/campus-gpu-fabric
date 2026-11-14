"""Transaction-local selections shared by lazy stores and ordinary diagnostic dicts."""

LIVE_ALLOCATIONS = ("Pending", "Allocated", "Running", "PreemptRequested", "Preempted", "Rejected")
HELD_ALLOCATIONS = ("Allocated", "Running", "PreemptRequested")
LIVE_JOBS = ("Pending", "Running", "Checkpointing", "Preempted")


def _field(value, name):
    if name == "attempt_id":
        return value.get("command", {}).get("attempt", {}).get("attempt_id")
    if name == "action":
        return value.get("command", {}).get("action")
    return value.get(name)


def values_where(collection, **filters):
    """Select current values, including mutations made earlier in this transaction."""
    indexed = getattr(collection, "values_where", None)
    if indexed is not None:
        return indexed(**filters)

    def matches(value):
        for name, expected in filters.items():
            if name.endswith("__gt"):
                actual = _field(value, name[:-4])
                if actual is None or actual <= expected:
                    return False
            else:
                actual = _field(value, name)
                matches_filter = actual in expected if isinstance(expected, tuple) else actual == expected
                if not matches_filter:
                    return False
        return True

    return (value for value in collection.values() if matches(value))


def touched_values(collection):
    """A plain dict has no access tracking, so its validation stays conservative."""
    touched = getattr(collection, "touched_values", None)
    return collection.values() if touched is None else touched()
