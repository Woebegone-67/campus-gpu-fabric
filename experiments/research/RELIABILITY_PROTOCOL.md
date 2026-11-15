# Reclaim-aware placement: frozen prospective comparison v1

Frozen before running comparative outcomes, 2026-11-15. Base: `0217e45de5bac7c8217c0144d51ff6414331d2f5`.

## Question and claim boundary

Does jointly assigning a fixed admitted cohort to devices using causal, past availability samples and declared task profiles reduce interruption waste beyond strong simple placement rules, while respecting the same contributor return deadlines?

This measures placement, not the already sealed notification-time four-action experiment. Its percentages are not comparable to that experiment's weighted cost J. The previous positive and negative results remain unchanged. This is a finite, synthetic, one-recall-per-device model; it does not establish measured GPU performance, real-world prediction accuracy, or a general scheduling theorem.

## Inputs fixed before outcomes

- Seeds: integers 0 through 19, paired across every policy in every regime; no seed exclusion.
- Four admitted tasks and four devices. Base declared task compute seconds `[16, 38, 72, 110]`, checkpoint seconds `[0.1, 1, 4, 8]`; independently permute the task attributes and device order per seed. Report order/ID effects through these permutations.
- Device mean uninterrupted windows `[10, 30, 60, 140]` seconds. Each device has 16 completed historical windows. Samples are independent uniform `[0.75, 1.25] * mean`. All observations are available before allocation. Held-out future windows use independent draws and never enter the selector.
- Base notice: 12 seconds; stop cost: 0.1 seconds; restart cost: 0.5 seconds; contributor use after the notice deadline: 10 seconds. A 0.1-second save margin is shared across policies.
- Same causal notification responder in every placement arm: finish within the notice if the declared remaining duration permits; otherwise save immediately if its declared cost fits; otherwise stop. Actual compute duration and future recall timing are visible only to the outcome executor. Saving blocks computation. Failed saves discard unsaved progress. No delayed-save tuning.
- Main cohort is restartable so failure is not silently dropped from averages. Any nonrestartable control is reported separately, retaining terminal failures.

Regimes, equally represented (20 seeds each):

1. `baseline_controls`: history and future windows 300 seconds (no interruption before any base task completes).
2. `predictable_mixed`: base heterogeneous tasks and device windows.
3. `homogeneous_devices`: all device window means 45 seconds.
4. `homogeneous_tasks`: all tasks compute 60 seconds, save 2 seconds.
5. `uninformative_history`: all historical means 45 seconds; heterogeneous future means.
6. `reversed_history`: base historical means; future mean reliability order reversed.
7. `runtime_estimate_error`: actual task compute independently scaled by uniform `[0.5, 2]` relative to the declared duration; no actual duration leakage.
8. `short_notice`: base case with 2-second notices.
9. `mixed_checkpoint`: notice seconds `[2, 12, 2, 12]` paired with device profiles; base independent task checkpoint diversity.

The implementation must write the full generated inputs and source SHA-256 hashes before calculating outcomes, refuse an existing output directory, and preserve all rows. Code corrections required for semantic correctness are documented; outcome-driven generator changes are a new protocol, not a replacement run.

## Comparators and metrics

Compare legal ID/FIFO placement (B2 placement analogue), risk-only stable-first FCFS, longest-task-to-stable, highest-interruption-loss-to-stable, per-arrival greedy minimum of the same cost matrix, and bounded exact minimum-cost cohort assignment. Every arm receives the same historical/declared information and same admitted tasks. No future-aware oracle is a deployable baseline.

Pre-outcome comparator clarification: `loss_sort` ranks tasks by their maximum predicted interruption overhead across candidate devices (the maximum of their observed-data cost-matrix row), then pairs them to descending mean historical windows. Using duration plus save cost would duplicate duration sorting for these declared durations, so that duplicate is excluded before the first outcome run. The six named arms and all generator constants are unchanged.

Primary outcome: total wasted computation plus saving/restart overhead, in seconds. Also report mean task completion, batch makespan, completed/failed count, contributor waiting and deadline violations, and per-task completion harm relative to each baseline. No weighted composite score may hide contributor lateness. Report every regime and the equally weighted overall population; positive slices must never replace the overall result. Report paired-seed bootstrap 95% intervals, conditional on this synthetic generator, with a fixed bootstrap seed.

## Acceptance and stopping rules

1. A mechanism-level result requires meaningful improvement over at least the strong simple matching and same-cost greedy baselines in the declared predictable heterogeneous regimes, supported by paired evidence. Merely beating ID order is insufficient.
2. Any contributor deadline violation is reported separately and blocks a safe-operation claim; lower mean cost cannot offset it.
3. Homogeneous, uninformative, and reversed histories are deliberate falsification cases. Negative results are retained. Their presence prevents an unconditional production recommendation; opt-in use requires a trusted, calibrated causal profile source and a fallback.
4. Do not change weights or generator constants to obtain a preferred result. If joint assignment has no material advantage over simple matching, say so and prefer the simpler policy; do not call extra complexity a breakthrough.
5. Validate the pure policy against the real isolated SQLite kernel for legality, version conflicts, exclusivity, and unchanged admission; simulated performance and kernel compatibility are distinct evidence.

## Deployment boundary

Existing production snapshots do not yet populate historical availability windows and task runtime/save profiles. Missing, stale, insufficient, unsupported, or invalid inputs must return the existing B2 plan. The new policy is opt-in and is not the default. No future scenario fields, lease TTL, or timeout may be used as duration estimates. No production tasks, external resources, payments, or UI changes are part of this experiment.
