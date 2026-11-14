# CP1 two-person handoff — still to be performed

The user confirmed that A and B submitted code but have **not** cross-operated
the system. Leave this requirement NOT_RUN until they actually do so. An AI
switching Alice/Bob/admin profiles is technical UI evidence, not this handoff.

Use the same reviewed commit and source build for both operators. Start the
assembled application with `bash web/dev-local.sh api` and the frontend with
`bash web/dev-local.sh web` after `make setup`. Each operator uses their own
isolated runtime, or agrees to operate the same instance without resetting it.
Import that instance's `client-identities.json` through the identity panel;
do not share or commit bearer tokens, `auth.json`, or identity files.

Each operator should independently follow this short journey:

1. Run workspace: submit a logical work item, retain its ID, observe allocation
   and job binding rather than interpreting registration as execution.
2. Resources workspace: confirm the real CPU/memory/GPU inventory decrease.
3. Continuity workspace: inspect the same work's stages and lease; have the
   authorized administrator advance logical time. Confirm terminal status and
   the selected settlement behavior. Also cancel a separate active job.
4. Decisions workspace: inspect a real decision and distinguish selected from
   committed actions. If the relevant selection is absent, record that fact.
5. Events workspace: find corresponding committed events; change ordinary owner
   and confirm the other owner's private work is inaccessible.
6. Acceptance workspace: confirm logical simulation and NOT_RUN gate labels.
   Refresh the page and reauthenticate; the same server-side work must remain.

Record any failure instead of checking the step off. The technical persistence
and partial-success scenarios are additionally exercised by their registered
validators and observed UI records; this short human journey does not replace
those checks.

## Record format

After actual operation only, save `evidence/cp1/joint/human_handoff.json`:

```json
{
  "schema_version": "1.0.0",
  "build": {"git_commit": "", "source_sha256": "", "source_file_count": 0},
  "observed_at": "",
  "operators": [
    {"role": "A", "human_id": "", "observer_kind": "human", "status": "NOT_RUN",
     "workspaces": [], "actual": "", "evidence": []},
    {"role": "B", "human_id": "", "observer_kind": "human", "status": "NOT_RUN",
     "workspaces": [], "actual": "", "evidence": []}
  ]
}
```

The blanks above are intentional, not an executed receipt. Obtain the current
build with `python3 scripts/check_checkpoint.py --build-id`. Each operator's
`workspaces` must list `run`, `resources`, `continuity`, `decisions`, `events`,
`acceptance`; `actual` records their work IDs, outcomes and problems. Evidence
entries have repository-relative `path` and `sha256`, point to nonempty files
under `evidence/cp1`, and exclude secrets. Only successful observed records use
`status: PASS`. Identity claims are human attestations to review; a hash does not
prove authorship or presence.

## Executable acceptance entrypoints

Run with the project Python, for example:

```sh
.venv/bin/python tests/acceptance/cp1/cp1_capacity.py
.venv/bin/python tests/acceptance/cp1/cp1_concurrency.py
.venv/bin/python tests/acceptance/cp1/cp1_owner_isolation.py
.venv/bin/python tests/acceptance/cp1/cp1_leases.py
.venv/bin/python tests/acceptance/cp1/cp1_persistence.py
.venv/bin/python tests/acceptance/cp1/cp1_partial_success.py
.venv/bin/python tests/acceptance/cp1/cp1_normal_journey.py
```

Capacity executes fresh TCP requests against the assembled A+B host, including
profile/physical-parent exclusion. Concurrency, owner isolation and leases run
existing real TCP suites and check their required assertion IDs. Owner isolation
also runs B's kernel-backed query tests. Persistence and partial success rerun
the assembled process/TCP suite, then require matching actual browser evidence
in `evidence/cp1/joint/ui_receipt.json`. They intentionally fail if the observed UI
record is absent, stale or NOT_RUN. Normal journey additionally requires the two
human records above. These entrypoints do not write a top-level MET receipt.

The UI record uses `schema_version`, current `build`, `observer_kind`,
`observer_ids`, timezone-qualified `observed_at`, and `checks` containing the
applicable CP1 ID, `status`, concrete `actual`, and hashed `evidence`. The AI
observer record must continue to say `two_human_cross_operation: NOT_RUN`.

Run scripts sequentially: some reuse evidence output names. Finish source and
documentation edits before final runs because both enter the build digest.
`python3 scripts/check_checkpoint.py CP1` remains the final receipt integrity
gate. Passing individual validators does not itself supply every required
observation or constitute external competition acceptance.
