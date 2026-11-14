# CP4 declared scale and latency workload

This is an engineering load test of the integrated HTTP host in logical execution
mode. It is not a policy benchmark or evidence of real GPU performance.

- Frozen target: 128 nodes, 8 distinct physical A10 devices each, 64 actual
  researcher Owners, 10,000 independently created jobs and 50,000 mutation requests.
- Each job performs allocation create, job create, lease renew, job cancel and
  allocation release. A job-detail query is issued for every eighth job, producing
  1,250 additional ordinary queries. No events, terminal jobs or receipts are
  removed to accelerate the test. All failed requests remain in the history.
- 32 parallel lifecycle flows are submitted in bounded waves; maximum client
  requests in flight is measured rather than inferred from a thread count.
- Fixed requests use one GPU, 1 CPU core, 1 GiB memory, 32 MiB temporary storage,
  initial TTL300, renewed TTL600, timeout100. Every renewal must actually extend
  its returned deadline by300 seconds and the final state must agree. Logical time
  is not advanced during this load. Cancellation
  and release return the official 202 response; it is not a failure.
- Client-observed TCP wall latency includes waiting for the server and is measured
  for every request. Report nearest-rank p50/p95/p99 and throughput for the combined
  ordinary workload and each operation. No warmup or slow samples are deleted.
  Clock batches, injected latency and isolated policy experiments are not exercised
  here and are explicitly reported as unmeasured excluded classes.
- The latency target requires observed Linux cgroup limits of 4 vCPU and 8 GiB;
  native host diagnostics cannot satisfy that target. Both the load client and
  service are inside the constrained container. Source/build identity must remain
  unchanged from execution through review.
- SQLite uses the container's native runtime filesystem, with its original FULL
  durability unchanged. The database path is recorded and retained separately;
  compressed HTTP and final-state evidence is written to the mounted evidence
  directory. This avoids silently measuring host/VM shared-filesystem fsync as
  though it were native deployment disk latency.
- A predeclared 1,200-second wall-time limit bounds a full attempt. A partial run or
  diagnostic `--jobs` count is NOT_MET, even if every performed operation succeeds.
- The independent reviewer reopens hashed HTTP history, initial inventory and final
  authoritative state. It verifies all created identities and Owners, the exact
  operation counts, all final job/lease states and every committed resource delta:
  no CPU/memory/storage oversell, physical-device double allocation, double return,
  or final disagreement between the fold and stored placement.

Commands from repository root:

```sh
uv run python tests/acceptance/cp4/cp4_scale.py
uv run python tests/acceptance/cp4/cp4_latency.py
uv run python tests/acceptance/cp4/cp4_scale.py --review-only
uv run python tests/acceptance/cp4/cp4_latency.py --review-only
```

Development smoke requests at smaller counts measure feasibility and do not alter
the target. Initial harness defects, if encountered, are preserved with their raw
records and corrected before the final frozen candidate run.
The development v1 diagnostic renewed TTL300 to300 at logical time0; that legal
but ineffective renewal is replaced in frozen workload v2 by TTL600. Old diagnostic
records are not rewritten or presented as v2 acceptance evidence.
