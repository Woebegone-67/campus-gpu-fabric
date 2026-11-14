# Real CPU loss accounting — preregistered bounded protocol

Frozen before execution, 2026-11-15. This is a mechanism/value study, not CP4 acceptance or a GPU benchmark. No production kernel changes, no CP3 125-run rerun.

## Questions and arms

Q1: Does verified continuation reduce actual repeated computation relative to a legal cold restart after the same interruption trigger? Four cases, two arms each, eight runs total:

- zero: request interruption immediately after dispatch, before observed computation; require checkpoint step zero or mark this control invalid.
- middle: interrupt when first observed progress reaches 50%.
- late: interrupt when first observed progress reaches 90%.
- save-failure: middle trigger, inject an OSError at a checkpoint file write before bytes are written. The continuation arm must fail safely without pretending recovery; cold restart does not require a save and may complete.

Both arms use the actual CpuReferenceExecutor and unchanged CPU worker, same logical work/job/allocation identity across generations, same configuration (10,000,000 steps, 50,000 per batch, seed 17, batch_pause_seconds=0), same owner authorization, one interruption, notice budget 1 second, stop fence, 40-second observation horizon and restart budget one. An isolated research harness issues executor commands after confirming the previous process stopped; it does not claim the product API already offers an optional cold restart when a verified checkpoint exists. No extra devices, extended lease, future knowledge or added compute pacing.

The continuation arm saves, waits for trusted verification, stops, and resumes from that artifact. Cold restart stops directly and starts generation two at step zero. Actual trigger and stop steps are recorded because wall-clock scheduling may overshoot a progress threshold. Work starts at initial dispatch and the eight traces are separate paired observations, not identical physical execution or statistical replication. Arm order alternates by case. No timing-based parameter selection or rerun to select a winner.

Q2: Does the current single-workload setting contain an opportunity beyond a correct simple finish-fits baseline? Use only pre-notice progress/compute observations to estimate remaining duration conservatively (slowest observed per-step batch rate, multiplied by two). Baseline: finish if that estimate plus a fixed 100ms stop/safety margin fits the original one-second notice, otherwise checkpoint immediately. Report decisions and observable prediction checks; do not equate a counterfactual estimate with an executed finish strategy. If all candidate information is already consumed by this strong baseline, do not claim a new algorithm or run a larger matrix. Any novel candidate needs a separately frozen discriminating protocol.

## Measurement and checks

- A research launch wrapper calls unchanged worker.run and wraps original advance/atomic_json only to collect monotonic wall time, process CPU time, step intervals and file sizes. Measurement overhead exists equally in both arms; the failure case explicitly changes checkpoint IO only. No artificial sleep in the worker. Harness observation polls may sleep up to 1ms and are not counted as worker compute.
- Independently calculate every exported checkpoint/result value via the existing matrix oracle; record original artifact SHA, verified source, initial generation step and applied continuation. Reject a corrupted registered checkpoint before a successful resume; corruption probe uses a copy of the private runtime, never modifies the original artifact or its hash.
- Main outcomes: completed/failed/unfinished for all four cases per arm; per-work end-to-end seconds and common-H truncated completion cost (failed/unfinished contribute H). No exclusion of failure or slow runs.
- Record executed steps by generation, retained checkpoint step, discarded tail, and total-executed-minus-required steps for completed work. Do not add discarded and repeated work together: two views of the same overhead.
- Record compute CPU/wall, total worker CPU/wall, checkpoint write/verification cost, dispatch-to-resume-start cost, process-confirmed stop latency and notice overrun. Resource hold is executor process occupancy in these eight runs, not an authoritative GPU ledger.
- A ninth, separate real Kernel + CpuDriver run uses monotonic time, actual recall, actual checkpoint, owner-authorized resume and unchanged lease. It checks process stop before ledger release and reuse, result oracle, exact work/attempt identity and resource release. Its timings validate the product path, not a paired performance claim.
- Raw private secrets/nonces stay under runtime. Public receipts export sanitized artifacts, their original SHA, measurements and source hashes. Record build and hashes before/after; concurrent UI edits do not invalidate pinned backend hashes.

## Stop and integrity rules

Unexpected error is recorded, remaining independent cases still run if safe, failed cases remain in totals. Deadline overrun, incorrect result, unauthorized recovery, duplicated execution or invalid zero control blocks a positive claim for that case. Do not silently replace evidence. One bounded run only; rerun requires a documented harness defect and retains failed output, not parameter tuning. Correctness alone proves neither speedup nor strategy superiority. The fixed scalar CPU state is not a GPU training checkpoint, and a microbenchmark cannot support universal percentage claims.

## Addendum A — frozen before any run, actual strong baseline

Parent review requires executing Q2, not only reporting a prediction. Add one finish_fits arm to each of the four cases, bringing the budget to twelve arms plus one Kernel/Driver path. Keep the original protocol above as the revision record. The strong simple baseline uses the same causal estimate and original one-second notice: allow completion until a fixed 100ms-before-deadline saving boundary; if not already complete, checkpoint and stop. If completion is predicted to fit, that is recorded before the decision, not inferred from the later result. Without sufficient information it still uses the fixed safe checkpoint boundary. At the original deadline request stop regardless of checkpoint readiness. Stop receipt still requires physical exit. A failed save is not a success. Extra grace use, release delay and any deadline overrun are charged explicitly.

This intentionally strong fixed-boundary baseline may already capture all the opportunity. Do not add a new candidate arm if it would execute identical actions. No 24-run matrix is authorized by this addendum. Third arms run after the original two in each case; single observations and nonidentical host timing are reported rather than claiming statistical speedup. Wait for the team's concurrent preview CPU work to finish before timed execution; do not stop user processes.
