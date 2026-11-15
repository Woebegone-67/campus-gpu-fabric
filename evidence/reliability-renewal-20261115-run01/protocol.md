# Recurrent availability study — frozen before outcomes

Date: 2026-11-15. Base: `0217e45de5bac7c8217c0144d51ff6414331d2f5`.

## Why a second study

The first sealed placement study assumes stable continuation after one recall.
Its paired batch policy saves 0.5996% of offered useful computation, and even a
separate hindsight completion-time oracle improves mean completion by at most
3.6224% over the same-cost greedy baseline. The first results and protocol remain
unchanged. This second study changes the execution process, not the old weights:
contributors repeatedly reclaim their devices, which may prevent long jobs from
ever accumulating sufficient progress when checkpointing cannot finish in time.

This is a prospective synthetic mechanism test, not independent confirmation of
the first study, production trace validation, or evidence of real GPU throughput.
The hypothesis was chosen after seeing the first study's limited headroom. No
second-study outcomes are inspected before its protocol and sources are sealed.

## Inputs and information boundary

- Reuse the exact 180 task/history cases from the first sealed study (nine
  regimes, seeds 0–19). Preserve that input file's SHA-256. Do not select cases
  by outcome, drop adverse regimes, or change task durations/checkpoint costs.
- Each device has 64 future available intervals generated with a separate fixed
  RNG stream and the same regime-specific means and [0.75, 1.25] factors. The
  no-interruption control retains 300-second windows. History never contains
  these future draws. Future runtimes retain the original error-control inputs.
- Online assignment receives task declarations, past windows, declared notice
  duration, and declared owner-use duration of 10 seconds only. It cannot access
  the future calendar, actual runtime, outcomes, or an oracle.
- Assignment is made once for four admitted tasks on four devices. Each task
  remains on its assigned device across interruptions; no migration, queue
  selection, capacity expansion, or admission-order advantage is introduced.
- All tasks declare **two remaining recoveries**, matching the contract's default
  maximum (`max_restarts=2`). Initial execution plus at most two new attempts is
  allowed; checkpoint restoration and cold restart both consume a recovery.
  The observation lease and task timeout are explicitly **900 seconds**, a legal
  configuration rather than the product's default 300-second lease. No extension
  or renewal of that lease is performed.

## Common execution semantics

All eight policies use the same continuous-time executor and recall responder.
Device calendars are independent of assignment and task progress. Each available
interval starts at zero or at the prior recall deadline plus 10 seconds of owner
use. Early stopping does not bring the next available interval forward.

The first execution has no restart cost. Every subsequent execution consumes
0.5 seconds of its available interval to restart. Computation and checkpointing
cannot proceed simultaneously. A successful checkpoint retains progress;
otherwise only progress since the last successful checkpoint is discarded.

At notification, the actual notice deadline becomes public. The responder uses
declared remaining work and checkpoint/stop costs. It attempts completion before
the checkpoint fallback time when feasible, otherwise immediately saves when
the save fits; otherwise it stops. Stop reserve is 0.1 seconds and the report
margin is 0.1 seconds. Actual completion is determined by physical work done,
not the declared runtime. Hidden runtime cannot choose the response action.

Every admitted task remains in the denominator. The fixed observation horizon
is **900 seconds**. Jobs still active at the horizon are right-censored, never
counted completed at 900. Recovery-budget exhaustion is an explicit blocked
noncompletion reason, not an observed completion or an unobserved censoring
event. Both kinds contribute 900 to the completion endpoint. Report partial
costs already incurred at the horizon. Calendar exhaustion
is an explicit error, never a manufactured stable tail. The fixed 64 windows
must cover the horizon; tests check that property.

## Fixed methods

The six first-study selectors are unchanged: ID order, stable-first FCFS,
duration-to-stability sorting, maximum one-step-loss sorting, one-step edge
greedy, and one-step joint matching. Add exactly two selectors:

1. `renewal_edge_greedy`: arrival order, minimum remaining edge using the new
   recurrent completion-cost estimate.
2. `renewal_batch`: exact injective minimum using that same cost matrix, with
   deterministic lexical ties.

The new predictor estimates completion under empirical renewal of observed
windows, with separate first-start and restart states. It uses conservative
one-second remaining-work/window quantization and accounts for restart consuming
the next available interval. Its state also contains remaining recovery budget
and remaining observation time. Each branch returns its actual bounded finish
cost, or the remaining horizon on noncompletion; the model averages these branch
costs. This is `E[min(T, 900)]` **under the discrete empirical model**, not a
calibrated real-world completion-time prediction. Budget depletion never creates
an extra attempt. The wrapper requires an explicit sourced remaining-budget datum
and currently supports 0–2 recoveries; larger budgets fall back rather than being
silently clamped. An earlier infinite-renewal helper remains a separately labeled
diagnostic and is not the primary selector. Exact recurrence and input
restrictions are sealed with the implementation before outcomes.

## Endpoints and reporting

The primary endpoint is restricted mean completion time, averaging `min(T, 900)`
over **all admitted jobs**, alongside the number actually completed by 900.
An unfinished job has infinite completion time for this endpoint and therefore
contributes 900; report known blocked/budget-exhausted status separately.
Keep raw per-job completion/censoring, checkpoint/restart costs, discarded
computation, owner waiting and return lateness. Also report absolute discarded
work and its ratio to all offered actual useful computation; never promote a
large percentage reduction in a small overhead term as total-compute speedup.

Report all nine regimes separately and equally weighted overall. Compare the
new batch method against every baseline, especially the same-cost renewal
greedy and duration sorting. Preserve individual harmed jobs, the largest
slowdown, no-information and history-reversal controls. Seed-paired bootstrap
uses 2,000 replicates and seed 20261115; intervals describe only this synthetic
generator. Do not replace the primary baseline after seeing results. If a
strict non-cold-restart control is included, label and report it separately.

No hyperparameter sweep, seed search, threshold adjustment, horizon change or
post-outcome case exclusion. A weak or negative result is retained as such.

## Evidence and scope

Before execution, freeze this file, JSON protocol, sources, complete input
calendars, first-study input hash and implementation hashes into a new exclusive
directory. Refuse overwriting or rerunning an existing result directory. Store
all eight-policy per-job outputs, summaries and evidence hashes.

Only local CPU simulation and isolated SQLite contract checks are authorized
here. No production task, actual GPU job, default policy switch, frontend redesign
or publication is part of this study. Product rollout requires A's causal profile
pipeline and calibration; fabricated reliability inputs are not a substitute.
