# CP3 B policy experiment suite

This is an executable isolated logical-kernel experiment, not a real GPU throughput test. It never mutates the running product instance. `Kernel` owns every write and settlement; `WorkflowOrchestrator` registers/binds work through A's atomic ports. Policy input is only the immutable current `DecisionSnapshot`.

## Freeze and run

Run from the repository root after all source changes are committed and stable:

```sh
python -m experiments.cp3_suite freeze --output evidence/cp3/policy-suite
python -m experiments.cp3_suite run --suite evidence/cp3/policy-suite --workers 3
python -m experiments.cp3_suite analyze --suite evidence/cp3/policy-suite
python tests/acceptance/cp3/cp3_policy_evidence.py
python tests/acceptance/cp3/cp3_policy_target.py
```

The source build, generator, fixed model, policy fingerprints, five families, seeds `[101,211,307,401,503]`, all 25 input files and thresholds are sealed **before** any validation execution. Freeze refuses a nonempty directory. Every run checks that the source still matches that seal. The frozen set must not be used for policy tuning or re-sealed after inspecting its results. A later research revision needs a new predeclared validation set, rather than silently reusing these seeds.

Development uses `freeze --partition development --output evidence/cp3/development-suite`; only seeds `[0,1]` belong to this partition. `--limit` leaves remaining runs explicitly `not_run`. Interrupted and failed runs are retained and are never automatically replaced. Each worker owns one fresh temporary database and one output directory; only the parent writes the index. `--workers` accepts 1–4 independent processes. Timings under contention are diagnostic, not controlled performance benchmarks.

## Protocol and limits

There are 32 external work arrivals per run, 400 logical seconds, a common 10-second fixed work model, and the same 512-candidate cap for B0/B1/B2/P and `P-no-opportunity`. This is smaller than the blueprint's suggested 256–512 work scale. It exercises causal records and paired accounting; it establishes no large-scale performance claim.

The dispatcher visits 5-second intervals plus exact external arrival/recall/node/callback boundaries. The kernel processes internal due events at their own times. Once no leases or future exogenous events remain, idle time is advanced to H. No policy receives future event times or hidden duration. Job timeout is an absolute failure deadline, **not** an estimated duration. Larger CPU/GPU requests are resource-size mixtures, not longer jobs. Heterogeneous true durations and scarce-long versus many-short work remain an A-model integration dependency.

| Family | Frozen inputs and actual probes | Explicit limit |
|---|---|---|
| Compatibility / fragmentation | One-owner zero-opportunity prefix, A10/A100/H100, whole GPU/MIG-like profiles, two-GPU requests, impossible demands | Bounded single-node enumeration; no global infeasibility proof |
| Waves / multiple owners | Four arrival waves, equal priorities, mixed resource sizes, split same-owner requests, late owner | No heterogeneous actual durations |
| Recall / save | Ready, failed and withheld reports; short notices; repeated recalls of both A10 devices; no A10 alternative during hold | Ready reports remain unverified logical reports, not real checkpoint application |
| Node / interleavings | Offline/ready/draining, stable work00/generation1 failure callback, retry before timeout, retired-generation completion callback, recall deadline at time30 | A uses whole-job timeout: timeout itself ends the job; timeout-then-retry is not claimed. An absent callback target is recorded uncovered |
| Neutral / reverse | Stable homogeneous contention, nonoverlapping arrivals, scarce two-GPU versus many one-GPU requests | Resource-size conflict substitutes for the unavailable long/short model; this gap prevents claiming complete semantic coverage |

The five validation seeds select all three neutral/reverse variants and also affect material workload/event parameters. Every family enters the main ratio, including negative and neutral results. No failed, cancelled, unfinished, late or impossible work is removed.

Only the active observed-opportunity placement component has an ablation. `P-no-opportunity` actually calls `CandidatePolicy(opportunity_enabled=False)`. The inactive recovery-cost component has no fake ablation. Each P/off decision also records B2 and opportunity-off action replacement on the exact same snapshot, plus diagnostic runtime. This tests whether the action changed; it does not claim a counterfactual gain. Full B2/off work outcomes must match, as well as same-state actions.

## Evidence and analysis

Each run retains input, summary, events, decision snapshots/plans/commit receipts, all returned receipts, direct command envelopes, final authoritative read snapshot, final decision snapshot, resource samples, external injections and runtime provenance. Raw JSON bytes are hashed. Command envelopes exclude workflow internals, so this artifact is **not** a complete command replay log; the shared prefix/full-replay gate is separate.

`suite-index.json` references the immutable manifest and each artifact. `read_artifact_bytes` rejects traversal, symlinks, oversized files and hash mismatches; it preserves original bytes for downloads. `load_suite_index(suite, root=ROOT)` validates protocol/cardinality/build and paths. `audit_suite` re-reads all hashes and recomputes summaries from actual final states, validates accepted decision provenance, compares complete work cohorts, and checks B2/off outcome identity. A downloaded summary or UI label cannot grant acceptance.

Primary cost is `F = sum(min(C,H)-r)`, using H for failures, cancellations and unfinished work. Each family's five P/B2 ratios are averaged, then the five family means receive equal weight. Undefined zero denominators remain explicit with absolute differences; they are never dropped. Fixed targets: R≤0.85; each pair's completion count must not decline; no new work delay >300 seconds or positive-baseline owner effective service below80%. Service is one unit per completed fixed-model work. B0/B1/off comparisons remain separate secondary results.

Per-work data include allocation waiting, first attempt start, job lifecycle, attempts, wasted failed/stopped attempt time, terminal/failure and lease states. Tail summaries retain censoring counts and distinguish truncated full-cohort completion quantiles from observed-grant-only waiting. Resource-time integrals are left-hand **sampled** logical lease occupancy; due events inside a sampling interval can cause approximation error. They are not measured GPU utilization.

`evidence_complete` means all scheduled artifacts/runs were checked. `coverage_complete` separately remains false for the explicit duration/timeout-retry limitations. `target_met` is the fixed numerical comparison, not overall CP3 acceptance. The registered evidence validator fails closed when semantic coverage is incomplete even if all125 executions finish. The target validator preserves negative ratios and harms. Neither validator can sign A's independent model, crash recovery or CPU-equivalence gates, nor the human UI gate.
