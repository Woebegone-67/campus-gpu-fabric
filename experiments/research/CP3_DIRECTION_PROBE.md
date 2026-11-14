# Bounded direction probe, declared before execution

2026-11-14. This is a mechanism diagnostic, not a new validation benchmark.
Do not retune on the old 125 validation runs or change their manifests.

Question: is fixed B2 admission preventing meaningful changes to unfinished-work
area, even when every job has the same duration? Compare existing B2 and P with
a research-only admission relaxation. It ranks current legal candidates by the
number of distinct currently legal demands that can coexist with that candidate.
Only maximum-priority legal demands participate; waiting 20 seconds activates an
oldest-demand guard. Ties retain B2 enumeration order. This is a pairwise proxy,
not an exact batch optimizer or a proven fairness policy. It receives only the
current DecisionSnapshot. It cannot see future arrivals, true duration or input
files. No product policy or public API is changed.

Five inputs use one existing two-A10 node and an 80-second horizon. Work duration
is uniform within each input: 10 seconds except the explicit 20-second lease
counterexample. All work, including failed or unfinished work, remains in F.

1. Packing witness: one two-GPU demand followed by two one-GPU demands, all at0.
   Predicted B2/P F=50 and admission-relaxation F=40 if actual B2 admits wide first.
2. Lease counterexample: same three demands, but the wide request has a 30-second
   lease and every work has a declared 20-second model duration. Test whether
   delay causes lease expiry/lower completion; do not remove failure.
3. Homogeneous neutral: four one-GPU demands at0. Expect unchanged F.
4. Late-wide: packing witness plus one two-GPU arrival at15.
5. Late-narrow: identical observed prefix, but one one-GPU arrival at15.
   Compare admission-relaxation action prefixes strictly before15; they must match.

Execution budget: B2 and relaxation on all five inputs, plus existing P on the
packing witness: 11 tiny kernel runs, one pass, no parameter search. Each uses a
new temporary database. Seal the exact input bytes and script digest before any
run; retain plans, receipts, events, final states and summaries. Runtime is not a
performance benchmark. No hidden/future information is supplied to policies.

Decision rule: a >=10% witness gain with unchanged completion count establishes
that the larger action class can matter on that witness only. Any completion
loss or new failure rejects promotion. Neutral or inverse cases are retained.
Even a positive probe does not establish a general advantage or CP3 R<=0.85.
After this single diagnostic, freeze the existing placement-only route; further
research requires a new information/action contract and newly frozen validation.

Input correction before the corrected run: the first attempt used TTL15, which
the real allocation contract rejects (minimum30). Preserve that failed manifest
and partial outputs. Replace that invalid case with legal TTL30/uniform-duration20,
without changing the policy or other four inputs. This is an input-validity repair,
not a result-based policy search. Retain both attempts separately.
