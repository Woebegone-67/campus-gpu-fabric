# Independent CP3 model

`cp3_reference.py` is an executable specification using only Python's standard
library. It does not import the service, placement functions, resource counters,
database code or event reducer. `cp3_history.py` is the separate system adapter:
it invokes real kernel commands and compares public observations with the model.

The fixture is deliberately small: one node with CPU/RAM/disk capacities 4/8/6
and two whole A10 devices, each with 8 units of VRAM. Requests vary all four
consumable dimensions, GPU model and per-device VRAM. Anonymous token accounting
makes the model structurally different from device placement. Returned creation
IDs are bound as opaque names; the reference independently derives every state,
owner, grant, deadline and resource claim. It advances integer time one unit at
a time, while the service uses event jumps.

The generated sequential histories combine allocation, queued grants, priority,
idempotent replay/conflict, foreign-owner mutation, jobs, timeout, completion,
cancellation, renewal, release, clock jumps and node failures. Every command is
followed by full state, owner projection, physical-device and independently
folded signed event-delta checks. Periodic fresh database handles test persistence.
A fixed boundary trace includes pending TTL, expiration coinciding with timeout
and completion, duplicate resource return and node generation changes.

Concurrent traces use separate real SQLite connections in three worker threads,
two waves and six operations per history. Invocation and response monotonic
timestamps define real-time predecessor edges. Exhaustive search checks whether
some sequential execution satisfies every reply and the final snapshot while
preserving those edges. It does **not** infer an order from service revisions or
events. Search exhaustion is inconclusive and fails the gate. Corruption controls
reject impossible real-time ordering, altered state/deadline/status and lost
final resource occupancy; a valid reversed order for overlapping calls is accepted.

Run from the repository root:

```sh
.venv/bin/python tests/acceptance/cp3/cp3_independent_model.py \
  --output evidence/cp3/independent_model_receipt.json
.venv/bin/python -m pytest -q tests/concurrency/test_cp3_model_history.py
```

The default acceptance run retains 32 × 100 generated commands, 30 × 6 concurrent
operations, a 17-command boundary trace and oracle controls. The receipt records
seeds, actual command/error counts, overlap/order counts and exact build identity.
Each raw normalized history is retained beside the receipt with its hash. Failure
histories and traceback are retained without claiming minimality. A source change
during a run produces `STALE`, not `PASS`.

This is evidence for the enumerated bounded histories. It does not establish a
universal proof, arbitrary multi-node or MIG placement correctness, HTTP behavior,
CPU executor continuation, or a complete CP3 gate. Those require separate checks.
