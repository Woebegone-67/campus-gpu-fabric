# Reclaim-Aware Computing — paired mechanism study

Initial inputs frozen before the first outcome run, 2026-11-15. Revision 2 was
frozen before the corrected outcome run. Case values and weights are unchanged;
the amendment aligns mandatory-checkpoint versus restartable failure behavior
with the product and gives every baseline the same 100ms reporting margin.
The complete preliminary run remains in `run-01-preliminary/`. Revision 3 aligns
the availability clock and equality boundary with the product: resource reuse
starts after the announced deadline plus owner-use duration, even if stopping
finishes early; checkpoint completion exactly at the stop boundary is rejected.
Revision 2 inputs, outputs and sources remain in `run-02-clock-audit/`. The
revision 3 paired costs, actions, failures and win/tie/loss counts are unchanged.

## Question and scope

Given the same observed progress, throughput interval, save/restart/stop cost
estimates and contributor deadline, when should a running job finish, save now,
save later, or stop without saving? The implemented pure production planner is
the adaptive arm. We do not implement a second copy for experiments.

The broader comparison is a **deterministic parametric execution model**, not
measured GPU performance. Measured CPU product-path evidence is reported
separately. Expensive checkpoint values explore larger-state workloads; the
existing approximately 600-byte CPU checkpoint does not calibrate those values.

## Frozen arms and contributor preferences

1. Cold restart: stop immediately, restart from zero after the owner-use window.
2. Immediate save: request a checkpoint immediately, stop on its completion.
3. Fixed latest save: run until the latest estimated safe save time, completing
   naturally if possible. The same cost estimates and stop margin are available.
4. Finish-if-fits, otherwise immediate save: use the same conservative remaining
   compute estimate and fallback reservation as the adaptive planner.
5. Reclaim-aware: call `campus_fabric.policies.reclaim.plan_reclaim`.

All arms have the same forced-stop boundary. Two explicitly selected product
preferences are evaluated separately, not mixed into one average:
`return_first` (owner wait weight 1.0) and `preserve_work` (0.25). No weight fitting
or case removal after results. The score is remaining useful compute **after
return**, plus save/restart overhead, plus weighted owner wait. It is not total
compute, GPU throughput, energy, or a universal welfare measure.

## Inputs and execution model

The immutable case file distinguishes `observed` inputs from hidden `actual`
outcome parameters. Only observed fields reach the planner and baselines. The
runner advances computation at the actual rate, blocks computation while
checkpointing, then confirms stop after the actual stop cost. A successful
checkpoint captures progress at save start; a failed/incomplete checkpoint does
not count as retained progress. The main table uses `best_effort`: saving
failure is counted, then the next attempt recomputes from zero. Mandatory
`checkpoint` mode is evaluated separately on four control cases: it cannot skip
an affordable mandatory save for economy, and saving failure is terminal.
Each interruption can resume once when the resource becomes available at
`announced deadline + 10 seconds`. Early stop therefore increases the waiting
time until that fixed availability point; it does not move the point earlier.
A repeated
recall case applies the same rule a second time after 2 seconds of resumed work.

The independent outcome model always enforces the planned stop-request boundary,
including a failed completion prediction. If actual stopping exceeds its input
bound, the resulting owner-deadline violation remains in the report. Expensive
saving, zero/low progress, near completion, tight windows, stale observations,
save failure, slowdown, underestimated saving and repeated recall are included.

## Report and decision rule

Report each raw row and each preference separately: action; completed/failed;
saved progress; repeated compute; save/restart overhead; owner wait; maximum
return lateness; deadline violation count; completion time from recall; and the
realized bilateral score. Preserve ties and worse rows against the strongest
simple baseline. Do not call a win on the frozen objective a Pareto improvement
unless raw metrics also support it. Report the mechanism as a cost-aware
threshold/feasibility policy, not a new general optimal scheduling theorem.

No 125-run suite, no seed-specific branches and no claim that deterministic
cases are independent samples. Source/input hashes and the first run timestamp
are saved alongside results. Tests check accounting, failure denominators and
absence of hidden outcome data from online inputs.
