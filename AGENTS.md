# Campus Fabric development contract

Read `README.md`, then your assigned `docs/handoff/OWNER_A.md` or `OWNER_B.md`.
The frozen source of HTTP requirements is `spec/official/api/openapi.yaml`; do not edit it.
`docs/SEMANTICS.md` distinguishes team choices from official requirements.

- A (Woebegone-67): authoritative domain/storage/clock/auth/executors, mutation APIs,
  test-control adapters, app assembly, backend locks, deployment.
- B (xuyangma): authorized query projections, query APIs, policies, workflows, web,
  experiments, frontend locks.
- Shared contracts: A merges changes after notifying B; regenerate JSON Schema and TS.
- A command is the only write path. Policies return plans; UI consumes projections.
- Never use future scenario information in an online decision. Timeout is not duration.
- Keep WorkIntent arrival, allocation grant, job creation and attempt identity distinct.
- Test credentials never enter web bundles. Product extensions never call test backdoors.
- Preserve official inputs and evidence. A CP0 pass is not API conformance or product readiness.
- Run `make cp0` for the scaffold gate; later checkpoint gates reject missing evidence.
- Use isolated runtime directories, branches and ports. Never reset another developer's instance.
- Do not report a plan, stub, schema parse or simulation as real execution evidence.
- No force-push, destructive cleanup or changes to archived MCM material.

