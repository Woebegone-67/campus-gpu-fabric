# Committed-state journal

`fabric_state` remains the authoritative JSON aggregate. Schema 2 adds
`fabric_journal`: each successful state save and its lossless JSON changes are
written in the same SQLite transaction. The journal includes workflow stages,
saved responses, deduplication, execution outbox/callbacks and clock bookkeeping,
including writes that do not increment a domain revision.

The format uses dictionary paths, `set`, `delete`, and append-only list tails.
Records have a contiguous sequence, before/after state hashes and a hash chain.
This detects accidental modification, omission and reordering; it is not an
external signature or evidence that domain transitions were valid.

`export_journal()` is an internal local audit function with a coherent SQLite
read snapshot. It is not exposed through public or owner-scoped HTTP. Never
publish private state journals as an ordinary user's event feed.

Opening a valid schema-1 database atomically adds one `migration-baseline`
record containing the exact existing state. Concurrent openers serialize through
SQLite. Migration does not reconstruct past history. An unknown schema or
state/journal mismatch fails closed. Existing live instances are not migrated by
acceptance tests: all tests use their own temporary databases.

The independent reducer is `tests/acceptance/cp3/cp3_state_replay.py`, importing
only the standard library. Acceptance separately checks public resource deltas,
complete persisted JSON reconstruction, online causal prefixes and domain
correctness; these are distinct properties. The journal does not certify actual
CPU execution or speed. The existing aggregate serialization cost remains a
separate scale/latency concern.
