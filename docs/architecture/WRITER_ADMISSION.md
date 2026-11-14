# Bounded writer admission

The 64-job phase diagnostic found that writes spent much longer acquiring
`BEGIN IMMEDIATE` than executing the transaction. Later arrivals repeatedly
acquired the SQLite writer lock before earlier waiters. This change addresses
that ordering problem; it does not remove SQLite's single-writer limit or claim
the CP4 latency target is met.

## Scope and order

Each `SQLiteStore` instance owns one condition-protected FIFO of at most 64
tickets, including the current writer. A caller appends a ticket before opening
its transaction connection. Only the head may attempt `BEGIN IMMEDIATE`.
The ticket remains at the head through business logic, COMMIT or ROLLBACK, and
connection close. Removal wakes the waiting callers; only the next ticket may
proceed. No new worker threads are created.

If the queue is full, the caller waits for capacity rather than failing
immediately. FIFO applies to callers that have entered this bounded queue.
Callers waiting outside a full queue do not have a guaranteed order. Separate
store instances, even for the same database in the same process, have separate
queues. There is **no cross-instance or cross-process fairness guarantee**.
Services that require this fairness must share their existing store instance;
the implementation adds no global database registry or lock lifecycle.

SQLite `BEGIN IMMEDIATE` remains the authoritative lock for every process.
Admission grants permission to attempt that lock, never permission to mutate
without it. Each process should construct its own store; an instance and its
thread condition are not intended to be inherited across `fork`.

## Timeout and failure behavior

The original 30-second writer acquisition budget starts when entering admission.
Waiting for a queue slot, waiting for the head, and acquiring the SQLite write
lock share that deadline. The BEGIN connection's `busy_timeout` receives only
the remaining budget, rather than another 30 seconds. Deadline expiry raises
the existing `sqlite3.OperationalError("database is locked")` family with
`SQLITE_BUSY` metadata. Millisecond conversion has SQLite's normal timer and
thread-scheduling precision; this is not a hard request deadline.

After BEGIN succeeds, the connection's normal 30-second busy timeout is restored
for subsequent SQL. The change does not impose a new business-transaction time
limit. A `finally` block removes the ticket on success, connection setup failure,
BEGIN failure, body failure, interrupted execution, COMMIT failure, ROLLBACK
failure, or connection-close failure. A waiter timing out removes its own ticket
without disturbing other queue members.

The queue and connection conditions are not held while user code executes.
The existing connection settings, journal, resource validation, commit ordering,
WAL mode and `synchronous=FULL` remain unchanged. Reads through `snapshot()` and
`read()` do not enter the writer queue and retain SQLite snapshot isolation.
Store startup/schema migration still uses the normal write transaction; this
does not bypass its validation or cross-process locking.

## Verification and limits

`tests/concurrency/test_writer_admission.py` covers ordered entry with a later
arrival, bounded overflow waiting, both queued and overflow timeout cleanup,
the shared BEGIN acquisition budget, snapshot reads during an uncommitted
write, failures at every transaction boundary, BaseException cleanup, and
concurrent writes by independently spawned processes. The process test reopens
and audits the database and verifies WAL/FULL after both writers finish.

These tests establish ordering and transaction behavior, not a throughput or
latency improvement. A bounded diagnostic must measure the combined writer
admission and resource-locality candidate before considering another full CP4
load. FIFO alone can distribute an expensive writer's waiting time more evenly;
it cannot reduce that writer's service time.
