# Real CPU interruption-loss study

Read [the final interpretation](../../../docs/research/FINAL_LOSS_ACCOUNTING.md) first. This directory preserves one bounded execution, not CP4 acceptance or a GPU benchmark.

- `PROTOCOL.md`: original protocol plus the addendum authorizing the actual third arm, both written before execution.
- `run-01/manifest.json`: pre-run source hashes, configuration and case budget.
- `run-01/*-{continuation,cold,finish_fits}.json`: twelve raw real-process receipts with step intervals, measurements, sanitized public artifacts, callback observations and failure retained.
- `run-01/kernel-path.json`: separate real Kernel/Driver recall, release, wait, resume, result and settlement path.
- `run-01/summary.json`: all four cases per arm. Continuation 3/4 completed, cold restart 4/4, fixed-boundary closeout 4/4. The save-failure row remains in the common-H denominator.
- `run-01/manifest-sha256.json`: hashes of all original JSON receipts.
- `audit-01.json`: read-only recomputation, actual-baseline clarification and resource-slot cost scope. The third arm executes fixed-boundary closeout; its recorded remaining-time forecast is diagnostic only.
- `run-01-console.log`: original execution output, including the failed continuation.
- `validation.json`: focused verification and post-study process cleanup observation.

Private executor keys, nonces, signed originals and databases remain under the isolated runtime path in the manifest. Public artifact payloads omit the nonce and carry the original digest; oracle checks can be repeated from these exported values. The recorded backend/source hashes matched at execution start/end and during the later read-only audit. An evidence hash proves consistency of the recorded material, not external replication.

Run only the read-only audit against this evidence:

```bash
uv run python -m experiments.research.release_audit_loss \
  evidence/release-20261115/research/run-01 --output /tmp/new-loss-audit.json
```

The output must not already exist. New workload executions require a new output/runtime directory and a reason; do not rerun to select a faster timing. The measurements are single observations of a scalar CPU workload. Additional grace use, failed save and zero-progress overhead are part of the conclusion.
