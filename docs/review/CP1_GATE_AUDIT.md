# CP1 joint gate and frontend evidence audit

Observed: 2026-11-14, 04:42 UTC. Reviewed merged commit
`9bb0bb5e1d485652d7f5184782f6a82963d7ec0d` in the isolated
`review/cp1-acceptance` worktree. This audit does not change production code or
checkpoint requirements. The lead reviewer runs the separate live-browser and
HTTP acceptance; their results must be added to the final decision separately.

## Decision supported by this audit

The existing formal CP1 gate is **NOT_RUN**, not passed. Both deliveries being
submitted is sufficient to start joint acceptance, not to infer its result.
No independent CP1-blocking frontend/backend mismatch was established by this
bounded audit. Transport tests passed, but they do not execute React components
or the actual backend and cannot establish either UI journey requirement.

The current gate explicitly requires two operators. B's own delivery states
that this is unobserved and that one agent switching identities does not replace
two-person cross-operation. Automated Alice/Bob/admin sessions can establish
technical role and isolation behavior; they cannot be relabeled as two observed
human operators. Report a technical result separately from this outstanding
handoff condition. Do not silently rewrite the gate to obtain MET.

## Commands actually executed

Working directory for all commands: repository root of this worktree.

| Command | Observed result | Scope |
|---|---|---|
| `python3 scripts/check_checkpoint.py CP1` | Exit 1; `status: NOT_RUN`; `missing runtime evidence receipt: evidence/cp1/receipt.json`; no checked IDs | Actual integrity gate execution |
| `npm --prefix web run test:client` | Exit 0; 9 tests passed, 0 failed; Node 22.23.2 / npm 10.9.8 | Mock-fetch client transport tests only |
| `rg --files tests/acceptance web/tests` | `tests/acceptance` does not exist; only `web/tests/api.test.mjs` returned | Acceptance-validator inventory at reviewed commit |

The nine client tests cover ordinary credentials, omitted empty owner header,
forbidden test routes, 503/request-ID preservation, rejected business receipts,
202 acceptance semantics, invalid HTML responses, unknown mutation outcomes,
and object IDs/version parsing. They do not cover page refresh, cross-identity
React rendering, epoch resets, partial-success controls, or the six-workspace
browser journey.

## Gate evidence still required

`configs/checkpoints.json` registers seven CP1 validators, none present in the
reviewed commit. A truthful receipt must identify executed checks, actual
artifacts and their matching validator implementation, not merely claim PASS.

| Check | Required observation |
|---|---|
| CP1-NORMAL-JOURNEY | Two operators; actual submission, execution, cancellation or settlement across six workspaces |
| CP1-CAPACITY | Actual allocations match CPU, memory, GPU, VRAM and profile inventory |
| CP1-CONCURRENCY | Independent history of 32 competing requests plus repeated idempotent requests |
| CP1-OWNER-ISOLATION | Detail, list, events and metrics under authorized identities |
| CP1-LEASES | Renewal, release and logical expiry return capacity once |
| CP1-PERSISTENCE | Process restart and page refresh preserve object identities and receipts |
| CP1-PARTIAL-SUCCESS | Allocation succeeds, job stage fails, retry does not allocate again, settlement visible in UI |

`scripts/check_checkpoint.py` verifies receipt structure, current commit/source
identity, expected check descriptions, registered validator path/digest,
non-placeholder observations, time, unresolved issues, and nonempty hashed
artifacts. Its module docstring correctly states that this is an evidence
integrity check, **not a verifier of the truth of arbitrary receipt prose**.
Adding seven empty files and a hand-authored MET receipt would not perform the
required acceptance.

The source digest includes `docs/`. Finish review documents before binding a
final build receipt; later documentation edits change this source identity.

## Frontend contract review

### Present and correctly scoped

- `web/src/api.ts` sends ordinary owner/bearer headers; rejects product calls to
  test-control paths; treats an HTTP-success/rejected receipt as a failed action;
  preserves uncertain network outcomes. It does not turn a 202 receipt into a
  running job.
- `CreateWork` fixes the work ID and input after submission and reuses them on
  response-loss retry. It warns before preparing a distinct new work item.
- `useQuery` replaces snapshots, aborts old requests and uses generation/key
  guards. `App` remounts workspaces on owner/token changes. These are sensible
  static protections, not evidence that the browser identity/reset scenarios
  have been exercised.
- Work detail reads server `legal_actions` and sends `expected_version` for its
  commands. Server authorization/version checks remain authoritative.
- Six workspaces call actual API paths. Missing recovery, checkpoint execution,
  real CPU execution and experiment writes are visibly identified as future
  work. The UI says logical simulation and 10-second model duration; it does not
  claim to execute submitted image commands.
- `devhost.create_app` uses the real A kernel with B query assembly and worker.
  Its acceptance reader intentionally emits NOT_RUN until joint evidence is
  integrated. A future receipt file alone will **not** change that UI reader:
  `src/campus_fabric/workflows/devhost.py:115–125` is currently static NOT_RUN.

### Nonblocking issues and future integration boundaries

1. **503 description is too specific.**
   `web/src/components/common.tsx:19–20` labels every 503 as a backend capability
   not yet integrated. A transient outage or injected fault is also a 503. The
   underlying error body and status remain visible, so this is not false
   success, but the title should become generic unavailability before CP2 fault
   journeys are accepted.

2. **Failed-stage semantics need alignment before durable rejected stages are
   introduced.** `Continuity.tsx:29` always retries creation as `job-v1`.
   `WorkflowOrchestrator` and its PortDouble test require a *new* stage after a
   durable failed stage. However, the actual A kernel only persists committed
   stages, not rejected ones; its real integration test intentionally retries a
   stale-version rejection using the same stage after refresh. Therefore this
   is **not a demonstrated current runtime deadlock**. Distinguish unknown
   outcome replay from deliberate retry if durable failed stages are added.

3. **A partial-success warning is not itself exercised evidence.** The warning
   checks `stage.state === 'failed'`, while current kernel rejections are not
   stored as failed stages. A transient failed create can leave an allocated
   lease and no job; the actual UI behavior and retry/settlement still need the
   prescribed live test. The mock orchestrator example cannot prove the UI
   warns about that real kernel state.

## Recommended final classification

If the lead review's runtime checks succeed, state: **CP1 technical integration
checks passed on the identified build; two-operator handoff and/or formal gate
receipt remain unobserved unless actually completed.** If they reveal defects,
list them as NOT_MET with their reproduction and do not start CP2 implementation
under an unconditional CP1-pass claim. Keep the two-person requirement visible;
its completion can be a short focused handoff using the already tested build.

No requirement is waived in this audit. No policy-quality, true GPU execution,
checkpoint recovery or CP2 claim is made.

## Follow-up: reproducible entrypoints added during acceptance

The initial missing-validator observation above describes merged `9bb0bb5`.
At the lead reviewer's request, this audit subsequently added all seven
registered entrypoints under `tests/acceptance/cp1/`, with a shared executable
runner. They are not empty pass wrappers: machine checks execute real HTTP or
process suites; capacity additionally exercises the assembled host's
CPU/memory/GPU/VRAM/profile mapping. UI checks require actual current-build
observation records, and the normal journey requires separate A/B human
records. Missing observations fail closed. The user subsequently confirmed that
the human handoff has not yet occurred. No top-level MET receipt was created.

See `CP1_HUMAN_HANDOFF.md` for exact commands, scope and the intentionally blank
human record. The fresh capacity validator was executed successfully. Additional
entrypoint execution is recorded by the lead reviewer on the final source
build; these new files do not retroactively change the initial gate result.
