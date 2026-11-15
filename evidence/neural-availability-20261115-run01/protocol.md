# Neural availability study — prospective synthetic protocol

Base implementation: `0217e45de5bac7c8217c0144d51ff6414331d2f5`.

This is a third, separately frozen research study. The first placement study
and second repeated-window study, including their negative results, remain
unchanged. There are no contributor traces available. This study tests a
synthetic hypothesis and the inference-to-allocation path, not real user habits,
real GPU performance, a deployed policy, or a calibrated availability promise.

## Question and fixed model

Can causal historical availability and public time-of-day features predict a
newly opened contributor window well enough to improve task-conditioned device
assignment? Prediction and assignment are separate hypotheses. Compare a neural
forecast with simple historical forecasts under the same cost transform and
matching; separately compare greedy and exact matching on the **same neural
cost matrix**. A favourable matching result alone is not evidence of neural gain.

The single model is `availability-mlp-v1`: eight scalar inputs, 16 tanh hidden
units, eight softmax outputs; 60 epochs, minibatch 32, SGD learning rate 0.03,
seed 20261115. These values are fixed before training. No architecture search,
validation-driven epoch selection, retraining, seed search, or rejected run is
allowed in this study. Validation is diagnostic only. The support consists of
the exact discrete durations `[8, 16, 32, 64, 128, 256, 512, 900]` seconds, not
bins containing arbitrary continuous durations.

Inputs are the versioned `historical_features` contract: sine/cosine of public
hour and weekday, normalized log-window mean and standard deviation, recent-four
normalized log-window mean, and fraction shorter than 64 seconds. Use at most
32 completed past windows and require four. `available_at` and `ended_at` must
both precede or equal the decision time. Censored and unfinished windows are
never fabricated as completed windows. The explicit synthetic timezone is UTC.
Contributor ID, generator parameters, future calendar, and actual task runtime
are not model inputs. This model is for newly opened windows, not residual life
of an already-aged window.

## Longitudinal inputs and temporal split

Generate 12 fixed synthetic contributors, eight seen during training and four
entirely excluded from training and validation. Each has four availability
probes per day, at 00:00, 06:00, 12:00 and 18:00. A probe starts a new available
window; subsequent recovery windows are generated independently under that
probe's context. The observed interval is at most 900 seconds. The gaps between
probes are unmodelled: this is longitudinal sampling, not a claim of continuous
realistic daily uptime. Actual calendars have 64 intervals and must cover 900
seconds; no infinite stable tail is invented.

The fixed generator has a common sinusoidal daily pattern, a weekday cosine,
contributor offsets, and categorical noise on the eight durations. The precise
formula, constants, random seeds and all inputs are frozen with the runner.

| Days, starting at zero | Purpose | Model samples |
| --- | --- | --- |
| 0–6 | Warmup observations for causal history | 0 |
| 7–34 | Training, eight seen contributors | 896 |
| 35–41 | Diagnostic validation, eight seen contributors | 224 |
| 42–45 | Four test days × four times; no model updates | 64 paired cases |

Each training label is observed strictly before the validation boundary; each
validation label is observed strictly before test begins. The model artifact
records the maximum training-label observation time. Test observations may
update causal historical features and empirical forecasts after they become
available, but never neural weights. The four unseen contributors have legal
past history at inference: this tests new identities with history, **not** a
zero-observation cold start.

At every probe, an independent probability 0.20 selects an observation cap from
64, 128, or 256 seconds. The record is right-censored only if its actual duration
strictly exceeds the selected cap. The cap-selection rate is not the realised
censoring rate; report the latter. Censored labels contain only their lower
bound and observation time. Exact labels contain the observed category. The
model uses exact categorical likelihood or `P(duration > cap)` respectively.

## Four test groups, 16 fixed times each

1. `predictable`: stationary generator, seen contributors.
2. `abrupt_shift`: daily pattern, weekday pattern and contributor offsets
   reverse only at the test boundary; histories adapt only as observations
   arrive. No method is warned of the shift.
3. `uninformative`: test windows are independent uniform categories, unrelated
   to provider, time and history. The new noise is not revealed to methods.
4. `unseen_provider`: stationary generator for the four held-out identities.

At every test time, four devices compete for a fixed cohort of four admitted
tasks. Seen-provider cohorts rotate by a fixed schedule. Tasks declare compute
durations `[24, 72, 160, 320]` seconds and checkpoint durations `[0.5, 3, 10, 18]`
in independently seeded permutations. True task durations equal declarations
here; there is no privileged runtime estimator. This study isolates availability
forecasting. There is no admission, migration, job dropping, queue selection,
hardware-speed difference, or extra GPU allocation advantage.

## Predictors and assignments

All predictors receive only the same currently observable records and public
metadata. Three non-neural baselines are fixed:

- `pooled`: last 32 observable probe labels per candidate contributor, pooled;
- `provider`: last 32 observable labels for that contributor;
- `timebucket`: last 32 labels for that contributor at the same six-hour slot
  and same weekday/weekend class; fewer than four labels falls back to provider.

Each empirical predictor uses exact and censored labels with 20 fixed rounds
of discrete tail-expectation updates and 0.5 pseudocount per category. Thus
neural comparisons do not merely compare censor-aware training to treating
censoring as a recall. Initial empirical probabilities are uniform; no test
outcome selects the number of iterations or smoothing strength.

All distributions use the existing `learned.probability_windows` conversion to
32 deterministic midpoint quantiles and the frozen
`renewal.budgeted_cost_matrix` transform. This approximates a CDF to 1/32 and
inherits the estimator's one-second conservative quantization and conditional
IID recovery assumption. The four matrix rows differ according to each task's
duration/checkpoint cost. Every task has two remaining recoveries and TTL / task
timeout / evaluation horizon **900 seconds**, an explicit study setting rather
than the product's default 300 seconds. No predictor can extend a return deadline.

Arms are `pooled_batch`, `provider_batch`, `timebucket_batch`, `neural_greedy`,
and `neural_batch`. Greedy preserves task arrival order; exact matching
minimizes the sum of the identical matrix costs. Helper ties are deterministic
lexical device indices. The product wrapper additionally preserves the B2
first-choice candidate on equal totals; this study measures the frozen helper
and does not claim wrapper-equivalent tie choices. Separate contract tests
establish wrapper legal-action and provenance behaviour.

## Independent execution and reporting

The selector never receives `actual`. Actual execution reuses only the existing
continuous-time `renewal_comparison.execute_task`, on a separately seeded hidden
calendar. The first calendar interval is the probe's actual label; remaining
intervals use the same synthetic context and independent draws. All arms use
the same task/calendar and responder. Owner-use time is 10 seconds, recall
notice 12 seconds, restart 0.5 seconds, stop 0.1 seconds. The existing responder
uses declared estimates, learns the true deadline at recall, attempts a feasible
finish/save, and otherwise stops. Physical progress, durable checkpoints,
discarded work, owner waiting, recovery use, and deadline misses remain measured
by that executor. The task stays on its initially assigned device.

Primary downstream results are completed count and mean restricted completion
time across **all four admitted tasks**. Unfinished jobs contribute 900 seconds
to the restricted endpoint; this is not an observed successful completion.
Distinguish budget-exhausted noncompletion from right-censoring at the horizon.
Report each arm, each group, equal-weight group aggregation, all harmed jobs,
worst slowdown, waste, checkpoint/restart overhead, return lateness, and counts.
Predeclared comparisons are neural matching versus each historical matching
baseline and neural matching versus neural greedy. Retain adverse controls and
unfavourable outcomes. Do not optimise an objective using the evaluation report.

Forecast diagnostics include exact/censored held-out negative log likelihood,
exact-label categorical Brier score, and count of evaluated/censored records.
Validation and test diagnostics are separate. Brier on exact observations alone
can have censoring selection bias and must be labelled accordingly. This small
study reports all four chronological test-day paired deltas and descriptive
means; no IID confidence interval over correlated rows or population claim.

## Freeze, execution gates, and reproducibility

`--freeze-only` creates a new exclusive output directory containing this
protocol, the runner, inputs, model source, learned adapter, renewal estimator,
matching implementation, and executor, with SHA-256 hashes. It performs no
training or outcome evaluation. `--run-frozen` verifies both frozen copies and
live sources, refuses an existing start marker/report, writes an exclusive
start marker, trains exactly once, saves and hash-loads the JSON artifact, then
evaluates all fixed cases and writes full raw rows, diagnostics and hashes.
A failed run remains marked and is not silently overwritten or rerun. The
first two study directories are never modified. Formal execution requires the
parent/user research go-ahead after freeze; unit tests use synthetic fixtures
and do not train or run the formal study.

Only local CPU research is authorised. No production resources, user profiles,
real GPU workloads, network training data, or default product policy changes.
Any future trace study needs a new prospective protocol, reliable observation
and contributor-consent provenance, calendar/feature calibration, and explicit
drift/OOD handling. Neither a forecast nor a saved model is a contributor promise.
