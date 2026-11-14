"""HTTP command adapters and temporary CP1 read bridges for B's projections.

All state changes use the kernel. These bridge handlers can be replaced by B's
query router without changing the command or snapshot contracts.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from campus_fabric.api.test_control.faults import fault_parameters
from campus_fabric.contracts.models import (
    CommandEnvelope,
    EntityRef,
    EventCursor,
    NodeInventory,
    ReadQuery,
    VersionStamp,
    WorkIntentRegistration,
)

ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def timestamp(seconds):
    return (ORIGIN + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def request_id(request):
    return request.state.request_id


async def object_body(request, *, optional=False):
    raw = await request.body()
    if not raw and optional:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Expected a JSON object") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "Expected a JSON object")
    return value


def exact_fields(body, allowed):
    if set(body) - set(allowed):
        raise HTTPException(400, "Unsupported request field")


def allocation_parameters(body):
    exact_fields(body, {"owner", "request", "queue", "priority", "ttl_seconds", "preemptible",
                        "preemption_policy", "node_selector"})
    value = dict(body)
    value["kind"] = "allocation.create"
    value["owner_id"] = value.pop("owner", None)
    policy = value.pop("preemption_policy", {})
    if not isinstance(policy, dict):
        raise HTTPException(400, "preemption_policy must be an object")
    exact_fields(policy, {"mode", "checkpoint_timeout_seconds", "max_restarts"})
    for name, target in (("mode", "preemption_mode"),
                         ("checkpoint_timeout_seconds", "checkpoint_timeout_seconds"),
                         ("max_restarts", "max_restarts")):
        if name in policy:
            value[target] = policy[name]
    selector = value.pop("node_selector", {})
    if not isinstance(selector, dict):
        raise HTTPException(400, "node_selector must be an object")
    value["node_selector"] = [{"key": k, "value": v} for k, v in sorted(selector.items())]
    return value


def job_parameters(body):
    exact_fields(body, {"allocation_id", "image", "command", "env", "timeout_seconds", "max_retries"})
    value = dict(body)
    value["kind"] = "job.create"
    env = value.pop("env", {})
    if not isinstance(env, dict):
        raise HTTPException(400, "env must be an object")
    value["env"] = [{"key": k, "value": v} for k, v in sorted(env.items())]
    return value


def error_status(code):
    return {"unauthorized": 401, "forbidden": 403, "not_found": 404,
            "invalid_command": 400, "rate_limited": 429,
            "internal_error": 500}.get(code, 409)


def command(request, actor, parameters, *, key=None, expected=None):
    try:
        return CommandEnvelope.model_validate_json(json.dumps({
            "request_id": request_id(request), "actor": actor.model_dump(mode="json"),
            "operation": parameters["kind"], "parameters": parameters,
            "idempotency_key": key, "expected_version": expected,
        }))
    except ValidationError as exc:
        raise HTTPException(400, "Request does not satisfy the command contract") from exc


def receipt_response(kernel, receipt, *, internal=False, status=None, payload=None):
    if receipt.outcome == "rejected":
        return JSONResponse(status_code=error_status(receipt.error.code), content={
            "error": receipt.error.code, "detail": receipt.error.detail,
            "request_id": receipt.request_id,
            "receipt": receipt.model_dump(mode="json"),
        })
    if internal:
        return JSONResponse(status_code=status or 200, content=receipt.model_dump(mode="json"))
    if payload is None:
        payload = kernel.receipt_payload(receipt)
    code = status or payload["status_code"]
    if code == 204:
        return Response(status_code=204)
    return JSONResponse(status_code=code, content=payload["body"])


def allocation_view(item):
    return {**item.model_dump(mode="json"), "id": item.allocation_id, "owner": item.owner_id, "state": item.state,
            "request": item.request.model_dump(mode="json"),
            "placement": item.placement.model_dump(mode="json") if item.placement else None,
            "created_at": timestamp(item.created_at),
            "expires_at": timestamp(item.expires_at) if item.expires_at is not None else None}


def job_view(item):
    return {**item.model_dump(mode="json"), "id": item.job_id, "allocation_id": item.allocation_id, "owner": item.owner_id,
            "state": item.state, "created_at": timestamp(item.created_at),
            "attempt": item.attempt.model_dump(mode="json") if item.attempt else None}


def page_limit(request):
    try:
        limit = int(request.query_params.get("limit", "500"))
    except ValueError as exc:
        raise HTTPException(400, "limit must be an integer from 1 to 500") from exc
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be an integer from 1 to 500")
    return limit


def object_page(request, rows, version):
    """Revision-bound pages avoid silently skipping objects after intervening writes."""
    limit = page_limit(request)
    offset = 0
    cursor = request.query_params.get("after")
    if cursor:
        try:
            epoch, revision, offset = (int(value) for value in cursor.split(":"))
            if min(epoch, revision, offset) < 0:
                raise ValueError("negative cursor component")
        except ValueError as exc:
            raise HTTPException(400, "Object cursor must be epoch:revision:offset") from exc
        if (epoch, revision) != (version.epoch, version.revision):
            raise HTTPException(409, "Object page changed; refresh without the cursor")
        if offset > len(rows):
            raise HTTPException(400, "Object cursor is outside this result")
    page = rows[offset:offset + limit]
    next_offset = offset + len(page)
    next_cursor = (f"{version.epoch}:{version.revision}:{next_offset}"
                   if next_offset < len(rows) else None)
    return page, next_cursor


def mount_cp1(app, kernel, identity):
    def snapshot(request, *, entities=(), after=None):
        actor = identity.actor(request)
        return kernel.read_snapshot(ReadQuery(actor=actor, global_scope=actor.role == "administrator",
                                              entities=entities, after=after))

    def commit(request, parameters, *, test=False, key=None, internal=False, status=None):
        actor = identity.test_actor(request) if test else identity.actor(request)
        envelope = command(request, actor, parameters, key=key)
        receipt, payload = kernel.commit_with_payload(envelope)
        return receipt_response(kernel, receipt, internal=internal, status=status, payload=payload)

    @app.post("/v1/allocations")
    async def create_allocation(request: Request):
        actor = identity.actor(request)
        body = await object_body(request)
        if body.get("owner") != actor.owner_id:
            raise HTTPException(403, "Allocation owner must match the authenticated owner")
        envelope = command(request, actor, allocation_parameters(body),
                           key=request.headers.get("idempotency-key"))
        receipt, payload = await run_in_threadpool(kernel.commit_with_payload, envelope)
        return receipt_response(kernel, receipt, payload=payload)

    @app.delete("/v1/allocations/{allocation_id}")
    def release_allocation(allocation_id: str, request: Request):
        return commit(request, {"kind": "allocation.release", "allocation_id": allocation_id})

    @app.post("/v1/allocations/{allocation_id}/renew")
    async def renew_allocation(allocation_id: str, request: Request):
        identity.actor(request)
        body = await object_body(request, optional=True)
        exact_fields(body, {"ttl_seconds"})
        return await run_in_threadpool(commit, request, {"kind": "allocation.renew", "allocation_id": allocation_id, **body})

    @app.post("/v1/allocations/{allocation_id}/checkpoint")
    async def checkpoint_allocation(allocation_id: str, request: Request):
        identity.actor(request)
        body = await object_body(request)
        exact_fields(body, {"status", "artifact_uri"})
        if "artifact_uri" in body and not isinstance(body["artifact_uri"], str):
            raise HTTPException(400, "artifact_uri must be a string when supplied")
        return await run_in_threadpool(commit, request, {"kind": "allocation.checkpoint",
                                "allocation_id": allocation_id, **body})

    @app.post("/v1/allocations/{allocation_id}/resume")
    async def resume_allocation(allocation_id: str, request: Request):
        identity.actor(request)
        body = await object_body(request, optional=True)
        exact_fields(body, set())
        return await run_in_threadpool(commit, request, {"kind": "allocation.resume", "allocation_id": allocation_id})

    @app.post("/v1/jobs")
    async def create_job(request: Request):
        identity.actor(request)
        return await run_in_threadpool(commit, request, job_parameters(await object_body(request)))

    @app.delete("/v1/jobs/{job_id}")
    def cancel_job(job_id: str, request: Request):
        return commit(request, {"kind": "job.cancel", "job_id": job_id})

    @app.post("/_test/clock")
    async def advance_clock(request: Request):
        identity.test_actor(request)
        body = await object_body(request)
        exact_fields(body, {"advance_seconds"})
        return await run_in_threadpool(commit, request, {"kind": "clock.advance", "source": "test_control", **body}, test=True)

    def recall_parameters(body, kind):
        exact_fields(body, {"gpu_ids", "notice_seconds", "duration_seconds"})
        return {"kind": kind, "device_ids": body.get("gpu_ids"),
                "notice_seconds": body.get("notice_seconds"),
                "duration_seconds": body.get("duration_seconds")}

    @app.post("/_test/contributor-demand")
    async def contributor_demand(request: Request):
        identity.test_actor(request)
        parameters = recall_parameters(await object_body(request), "test.contributor_demand")
        return await run_in_threadpool(commit, request, parameters, test=True, status=201)

    @app.post("/_test/node-state")
    async def node_state(request: Request):
        identity.test_actor(request)
        body = await object_body(request)
        exact_fields(body, {"node_id", "state"})
        return await run_in_threadpool(commit, request, {"kind": "test.node_state", **body}, test=True)

    @app.post("/_test/fault")
    async def configure_fault(request: Request):
        identity.test_actor(request)
        parameters = fault_parameters(await object_body(request), app.state.fault_targets)
        return await run_in_threadpool(commit, request, parameters, test=True, status=204)

    @app.post("/x/v1/admin/clock")
    async def admin_clock(request: Request):
        actor = identity.actor(request)
        if actor.role != "administrator":
            raise HTTPException(403, "Administrator authority is required")
        body = await object_body(request)
        exact_fields(body, {"advance_seconds"})
        return await run_in_threadpool(commit, request, {"kind": "clock.advance", "source": "manual", **body})

    @app.post("/_test/reset")
    def reset(request: Request):
        return commit(request, {"kind": "test.reset"}, test=True, status=204)

    @app.post("/x/v1/contributions")
    async def register_contribution(request: Request):
        actor = identity.actor(request)
        if not actor.can_contribute_resources:
            raise HTTPException(403, "Contributor authority is required")
        node = NodeInventory.model_validate_json(json.dumps(await object_body(request)))
        if node.contributor_owner_id != actor.owner_id:
            raise HTTPException(403, "Contribution owner must match the authenticated owner")
        envelope = command(request, actor, {"kind": "contribution.register",
                                            "node": node.model_dump(mode="json")})
        receipt = await run_in_threadpool(kernel.commit, envelope)
        return receipt_response(kernel, receipt, internal=True, status=201)

    @app.post("/x/v1/contributions/recall")
    async def recall_contribution(request: Request):
        actor = identity.actor(request)
        if not actor.can_contribute_resources:
            raise HTTPException(403, "Contributor authority is required")
        parameters = recall_parameters(await object_body(request), "contribution.recall")
        envelope = command(request, actor, parameters)
        receipt = await run_in_threadpool(kernel.commit, envelope)
        return receipt_response(kernel, receipt, internal=True, status=201)

    @app.post("/x/v1/work-intents")
    async def register_intent(request: Request):
        actor = identity.actor(request)
        registration = WorkIntentRegistration.model_validate_json(json.dumps(await object_body(request)))
        if registration.owner_id != actor.owner_id:
            raise HTTPException(403, "Intent owner must match the authenticated owner")
        match = request.headers.get("if-match")
        if match is None:
            receipt = await run_in_threadpool(kernel.register_current_intent,
                registration, actor=actor, request_id=request_id(request))
        else:
            try:
                epoch, revision = match.split(":")
                version = VersionStamp(epoch=int(epoch), revision=int(revision))
            except (ValueError, ValidationError) as exc:
                raise HTTPException(400, "If-Match must be epoch:revision") from exc
            receipt = await run_in_threadpool(kernel.register_intent, registration, version, actor=actor,
                                             request_id=request_id(request))
        return receipt_response(kernel, receipt, internal=True, status=202)

    @app.post("/x/v1/work-intents/{intent_id}/actions")
    async def intent_action(intent_id: str, request: Request):
        actor = identity.actor(request)
        body = await object_body(request)
        exact_fields(body, {"action", "stage_id", "expected_version", "expected_epoch"})
        if "expected_version" in body and "expected_epoch" in body:
            raise HTTPException(400, "Choose expected_version or expected_epoch")
        action = body.get("action")
        stage_id = body.get("stage_id")
        if action in {"release", "cancel", "resume"} and "expected_version" not in body:
            try:
                epoch = VersionStamp.model_validate_json(json.dumps({
                    "epoch": body["expected_epoch"], "revision": 0,
                })).epoch
            except (KeyError, ValueError, ValidationError) as exc:
                raise HTTPException(400, "Expected expected_epoch") from exc
            receipt = await run_in_threadpool(kernel.act_on_current_intent,
                intent_id, action, epoch, actor=actor, request_id=request_id(request))
            return receipt_response(kernel, receipt, internal=True)
        try:
            version = VersionStamp.model_validate_json(json.dumps(body["expected_version"]))
        except (KeyError, ValueError, ValidationError) as exc:
            raise HTTPException(400, "Expected expected_version") from exc
        if action in {"allocate", "create_job"}:
            if not isinstance(stage_id, str) or not stage_id:
                raise HTTPException(400, "Allocation and job creation require a stable stage_id")
            method = (kernel.create_allocation_and_bind if action == "allocate"
                      else kernel.create_job_and_bind)
            receipt = await run_in_threadpool(method, intent_id, stage_id, version, actor=actor,
                             request_id=request_id(request))
        elif action in {"release", "cancel", "resume"}:
            view = await run_in_threadpool(kernel.read_snapshot, ReadQuery(actor=actor,
                global_scope=actor.role == "administrator",
                entities=(EntityRef(kind="intent", entity_id=intent_id),)))
            intent = next((item for item in view.intents if item.intent_id == intent_id), None)
            if intent is None:
                raise HTTPException(404, "Work intent not found")
            ids = intent.job_ids if action == "cancel" else intent.allocation_ids
            if not ids:
                raise HTTPException(409, "The intent has no object for this action")
            kind = "job.cancel" if action == "cancel" else f"allocation.{action}"
            key = "job_id" if action == "cancel" else "allocation_id"
            receipt = await run_in_threadpool(kernel.commit, command(request, actor, {"kind": kind, key: ids[-1]},
                                             expected=version.model_dump(mode="json")))
        else:
            raise HTTPException(400, "Supported actions: allocate, create_job, release, cancel, resume")
        return receipt_response(kernel, receipt, internal=True)

    # Minimal A transport bridges. B may replace these using the same immutable ports.
    @app.get("/v1/capacity")
    def capacity(request: Request):
        identity.actor(request)
        view = kernel.decision_snapshot()
        nodes = []
        for item in view.inventory:
            node = item.model_dump(mode="json")
            node.pop("contributor_owner_id", None)
            node["id"] = node["node_id"]
            nodes.append(node)
        fields = ("cpu_millicores", "memory_mib", "ephemeral_storage_mib", "gpu_count", "gpu_memory_mib")
        totals = {side: {key: sum(node[side][key] for node in nodes) for key in fields}
                  for side in ("capacity", "available")}
        rows, cursor = object_page(request, nodes, view.version)
        return {"observed_at": timestamp(view.now), "logical_time": view.now,
                "version": view.version.model_dump(), "nodes": rows, "totals": totals,
                "next_cursor": cursor}

    @app.get("/v1/allocations")
    def list_allocations(request: Request):
        view = snapshot(request)
        rows, cursor = object_page(request, view.allocations, view.version)
        return {"allocations": [allocation_view(item) for item in rows],
                "version": view.version.model_dump(), "next_cursor": cursor}

    @app.get("/v1/allocations/{allocation_id}")
    def get_allocation(allocation_id: str, request: Request):
        view = snapshot(request, entities=(EntityRef(kind="allocation", entity_id=allocation_id),))
        item = next((item for item in view.allocations if item.allocation_id == allocation_id), None)
        if item is None:
            raise HTTPException(404, "Allocation not found")
        return allocation_view(item)

    @app.get("/v1/jobs")
    def list_jobs(request: Request):
        view = snapshot(request)
        rows, cursor = object_page(request, view.jobs, view.version)
        return {"jobs": [job_view(item) for item in rows],
                "version": view.version.model_dump(), "next_cursor": cursor}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        view = snapshot(request, entities=(EntityRef(kind="job", entity_id=job_id),))
        item = next((item for item in view.jobs if item.job_id == job_id), None)
        if item is None:
            raise HTTPException(404, "Job not found")
        return job_view(item)

    @app.get("/v1/events")
    def events(request: Request):
        after = None
        cursor = request.query_params.get("after")
        if cursor:
            try:
                epoch, sequence = cursor.split(":")
                after = EventCursor(epoch=int(epoch), after_sequence=int(sequence))
            except (ValueError, ValidationError) as exc:
                raise HTTPException(400, "Cursor must be epoch:sequence") from exc
        try:
            view = snapshot(request, after=after)
        except ValueError as exc:
            raise HTTPException(409, "Event cursor is stale; refresh without the cursor") from exc
        if after and view.next_cursor and after.after_sequence > view.next_cursor.after_sequence:
            raise HTTPException(409, "Event cursor is ahead of the current event stream")
        rows = view.events[:page_limit(request)]
        sequence = rows[-1].sequence if rows else (after.after_sequence if after else 0)
        cursor = EventCursor(epoch=view.version.epoch, after_sequence=sequence)
        return {"events": [event.model_dump(mode="json") for event in rows],
                "version": view.version.model_dump(),
                "next_cursor": cursor.model_dump(),
                "next_after": f"{cursor.epoch}:{cursor.after_sequence}",
                "has_more": len(view.events) > len(rows)}

    @app.get("/v1/metrics")
    def metrics(request: Request):
        view = snapshot(request)
        return {"version": view.version.model_dump(), "logical_time": view.now,
                "scope": "administrator" if view.global_scope else "owner",
                "allocations": len(view.allocations), "jobs": len(view.jobs),
                "running_jobs": sum(item.state == "Running" for item in view.jobs),
                "succeeded_jobs": sum(item.state == "Succeeded" for item in view.jobs),
                "failed_jobs": sum(item.state == "Failed" for item in view.jobs),
                "execution_mode": kernel.decision_snapshot().execution_mode}

    @app.get("/x/v1/work-intents")
    def intents(request: Request):
        view = snapshot(request)
        rows, cursor = object_page(request, view.intents, view.version)
        return {"intents": [item.model_dump(mode="json") for item in rows],
                "version": view.version.model_dump(), "next_cursor": cursor}

    @app.get("/x/v1/work-intents/{intent_id}")
    def intent(intent_id: str, request: Request):
        view = snapshot(request, entities=(EntityRef(kind="intent", entity_id=intent_id),))
        item = next((item for item in view.intents if item.intent_id == intent_id), None)
        if item is None:
            raise HTTPException(404, "Work intent not found")
        return {"intent": item.model_dump(mode="json"), "version": view.version.model_dump()}


def assign_request_id(request):
    value = request.headers.get("x-request-id") or uuid.uuid4().hex
    if not value or len(value) > 128 or any(char.isspace() for char in value):
        raise HTTPException(400, "Invalid X-Request-Id")
    request.state.request_id = value
