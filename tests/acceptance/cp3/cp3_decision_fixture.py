"""Test-only assembly for deterministic HTTP decision acceptance.

No additional HTTP route is installed. Real product registration triggers the
real workflow worker. Fault modes deliberately corrupt its proposal before A's
unchanged transactional commit checks. The capture is server-side test evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from campus_fabric.contracts import models as m
from campus_fabric.policies import make_policy
from campus_fabric.workflows.devhost import create_app as product_app


def create_app():
    app = product_app(worker_enabled=False)
    kernel, worker = app.state.kernel, app.state.workflow_worker
    mode = os.environ["CP3_DECISION_CASE"]
    policy = make_policy(os.environ["FABRIC_B_POLICY"], max_candidates=1 if mode == "budget" else 32)
    trace_path = Path(os.environ["CP3_DECISION_TRACE"])
    trace = {"case": mode, "plans": [], "submissions": []}
    observer = m.AuthenticatedActor(actor_id="cp3-decision-observer", role="administrator")

    def save():
        trace_path.write_text(json.dumps(trace, indent=2) + "\n")

    class FixturePolicy:
        def plan(self, snapshot):
            proposed = policy.plan(snapshot)
            if proposed.selected_candidate_id and mode == "stale":
                proposed = proposed.model_copy(update={"snapshot_version": m.VersionStamp(
                    epoch=snapshot.version.epoch, revision=snapshot.version.revision - 1)})
            if proposed.selected_candidate_id and mode == "illegal-placement":
                candidates = []
                for candidate in proposed.candidates:
                    if candidate.candidate_id == proposed.selected_candidate_id:
                        piece = candidate.placement.slices[0]
                        piece = piece.model_copy(update={"resources": piece.resources.model_copy(
                            update={"cpu_millicores": 999999})})
                        candidate = candidate.model_copy(update={"placement": m.Placement(slices=(piece,))})
                    candidates.append(candidate)
                proposed = proposed.model_copy(update={"candidates": tuple(candidates)})
            trace["plans"].append({"snapshot": snapshot.model_dump(mode="json"),
                                   "plan": proposed.model_dump(mode="json")})
            save()
            return proposed

    worker.policy = FixturePolicy()
    original_commit = kernel.commit

    def record_commit(command):
        if command.operation != "decision.commit":
            return original_commit(command)
        before = kernel.read_snapshot(m.ReadQuery(actor=observer, global_scope=True))
        response = original_commit(command)
        after = kernel.read_snapshot(m.ReadQuery(actor=observer, global_scope=True))
        trace["submissions"].append({"command": command.model_dump(mode="json"),
            "receipt": response.model_dump(mode="json"), "before": before.model_dump(mode="json"),
            "after": after.model_dump(mode="json")})
        save()
        return response

    kernel.commit = record_commit

    @app.middleware("http")
    async def deterministic_worker(request, call_next):
        response = await call_next(request)
        if request.method == "POST" and request.url.path == "/x/v1/work-intents" and response.status_code == 202:
            worker.tick()
            if mode in {"accepted", "budget"}:
                worker.tick()
        return response

    save()
    return app
