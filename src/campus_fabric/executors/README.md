# CPU reference execution

`CpuReferenceExecutor` runs a separate, real Python process for a fixed integer
computation. Its purpose is to demonstrate saved-state continuation and confirmed
process termination without a GPU or container runtime. It does **not** execute
the submitted image, command, or environment and is not GPU execution.

## Integration

```python
executor = CpuReferenceExecutor(private_directory, CpuReferenceConfig())
executor.dispatch(command, owner_id=authoritative_job_owner, logical_time=kernel_now)
for event in executor.poll(logical_time=kernel_now, version=kernel_version):
    # Commit through the sole command kernel, including callback deduplication.
    # Definitively rejected old-epoch/old-attempt callbacks may also be acknowledged.
    kernel_accept_or_reject(event)
    executor.acknowledge(event.callback_id)
```

The driver in `driver.py` performs the kernel integration. `owner_id` must come
from the committed job record, never an untrusted request field. Dispatch and
callback IDs are durable and idempotent. Callbacks retain the original dispatch
epoch and are delivered at least once until acknowledged. Their logical
`observed_at` is the first observation time; later redelivery preserves that time.
`physical_elapsed_seconds` is separately measured by a monotonic clock.

The adapter's journal contains execution receipts, process identities, callback
delivery state, and artifact registrations. It is not an allocation ledger.
Only the kernel decides whether resources may be reused.

## Execution and evidence

The task state is `(step, value)`. For each index `i`, the worker applies

```
value = (1664525 * value + 1013904223 + i) mod 2**64
```

The default task executes 1,000,000 steps from seed 17. Batches and optional
pacing make an actual interruption observable; they do not alter the final
answer. `reference_result()` independently calculates the complete answer in
the controller. The tests also compare against a separate uninterrupted worker.

1. `start` launches a new OS process and waits for its signed start receipt.
2. `checkpoint` writes an atomic, signed `(step, value)` artifact. The process
   keeps running. A ready/verified checkpoint alone never confirms termination.
3. `stop` sends a durable control request. A process is reported stopped only
   after `Popen.poll`, or restart reconciliation with OS process state, confirms
   that it cannot continue executing. For an owned child, a timeout escalates
   from SIGTERM to SIGKILL, still followed by exit observation.
4. `resume` requires the registered checkpoint, the same owner/epoch/intent/job/
   allocation, a greater attempt generation, and a confirmed stopped source.
   A new process loads the saved state. Only its genuine start receipt yields
   `continuation_status=applied`.
5. `completed` requires both physical exit and a verified complete result.

Ordered checkpoint/stop control files are replayed from the durable journal.
Artifact URIs are opaque `fabric-cpu://...` registrations, not filesystem paths.
SHA-256 registrations and HMAC signatures reject modified content. The signature
binds owner, epoch, the complete attempt identity, worker nonce, and workload.
Unknown URIs, cross-owner/cross-epoch artifacts, altered identities, symlinks, and
configuration changes cannot authorize a resume. `read_artifact` omits internal
nonces but still requires caller authorization and the correct owner/epoch.

## Limits and failure handling

- This is a trusted local reference worker. The private executor directory and
  signing key must remain inaccessible to untrusted tasks. HMAC is not remote
  attestation and does not defend against an administrator modifying the process.
- Linux/macOS POSIX process control is required. This adapter has no Windows or
  GPU/container execution guarantee.
- The single CPU driver stores its monotonic clock sample in the same SQLite
  transaction as deadline effects. Service downtime on the same boot is charged
  before callbacks are reconciled. Repeated samples do not charge time twice.
  A host reboot or monotonic rollback blocks startup pending explicit recovery;
  automatic cross-boot recovery is not implemented.
- After controller restart, a cooperative worker can still stop through its
  nonce-bound control queue. A recovered PID without the original `Popen` handle
  is never force-killed: PID reuse must not target an unrelated process. An
  unresponsive recovered worker remains `unknown` and must remain quarantined.
- A crash after launch reservation but before a process identity can be recovered
  leaves an uncertain launch. It is not automatically relaunched or released.
- Invalid execution evidence produces failure only after physical exit is known;
  otherwise it produces `unknown`. It cannot fabricate a successful result.
- The launch command is bounded and fixed. Job-level timeout, restart budget,
  node eviction, lease accounting, and public authorization belong to the kernel.
- No claim is made about hardware activity, real container images, arbitrary
  program checkpoints, cluster-wide failover, or full CP2 acceptance.

## Verification

From the repository root:

```sh
uv run pytest -q tests/executors/test_cpu_reference.py
uv run ruff check src/campus_fabric/executors/cpu_*.py tests/executors
```

The real-process tests cover uninterrupted/resumed result equality, artifact
verification, duplicate concurrent dispatch, source termination before resume,
PID/exit observations, controller reconstruction, callback replay, ordered
control delivery, interrupted delivery, tampering, launch failure, forbidden
user commands, and conservative treatment of nonresponsive recovered workers.
All test processes and data are isolated from human acceptance sessions.
