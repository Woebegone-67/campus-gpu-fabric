"""Read adapters and isolated experiments; no operating-ledger mutation or test backdoor."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from campus_fabric.contracts.models import EntityRef, EventCursor, EvidenceRun, VersionStamp
from campus_fabric.projections.read_models import (
    ProjectionError,
    allocation_json,
    job_json,
    legal_actions_json,
    metrics_json,
    resource_totals,
    timestamp,
)
from campus_fabric.workflows.experiment_runner import ExperimentBuildChangedError

from .service import QueryFailure, QueryServices, invoke, unavailable


def request_id(request: Request) -> str:
    trusted = getattr(request.state, "request_id", None)
    if isinstance(trusted, str) and trusted:
        return trusted
    supplied = request.headers.get("X-Request-Id", "")
    return supplied if re.fullmatch(r"[\w.:-]{1,128}", supplied, flags=re.ASCII) else uuid4().hex


def integer_parameter(request: Request, name: str, default: int, maximum: int) -> int:
    value = request.query_params.get(name)
    if value is None:
        return default
    if not value.isascii() or not value.isdigit() or len(value) > 16:
        raise QueryFailure(400, "invalid_query", f"{name} must be a nonnegative integer.")
    parsed = int(value)
    if parsed > maximum:
        raise QueryFailure(400, "invalid_query", f"{name} must be at most {maximum}.")
    return parsed


def page(request: Request, items: list[dict], version: VersionStamp) -> dict[str, Any]:
    limit = integer_parameter(request, "limit", 50, 500)
    offset = integer_parameter(request, "offset", 0, 9_007_199_254_740_991)
    if limit == 0:
        raise QueryFailure(400, "invalid_query", "limit must be between 1 and 500.")
    has_epoch = "at_epoch" in request.query_params
    has_revision = "at_revision" in request.query_params
    after = request.query_params.get("after")
    if after is not None:
        if any(key in request.query_params for key in ("offset", "at_epoch", "at_revision")):
            raise QueryFailure(400, "invalid_query", "Use after or pinned offset, not both.")
        if not re.fullmatch(r"[0-9]{1,16}:[0-9]{1,16}:[0-9]{1,16}", after):
            raise QueryFailure(400, "invalid_query", "after must be epoch:revision:offset.")
        epoch, revision, offset = map(int, after.split(":"))
        if max(epoch, revision, offset) > 9_007_199_254_740_991:
            raise QueryFailure(400, "invalid_query", "Object cursor exceeds the supported range.")
        if (epoch, revision) != (version.epoch, version.revision):
            raise QueryFailure(409, "stale_version", "The snapshot changed; restart pagination.")
    if has_epoch != has_revision or (offset > 0 and not has_epoch and after is None):
        raise QueryFailure(400, "invalid_query", "Pagination requires at_epoch and at_revision.")
    if has_epoch:
        epoch = integer_parameter(request, "at_epoch", 0, 9_007_199_254_740_991)
        revision = integer_parameter(request, "at_revision", 0, 9_007_199_254_740_991)
        if (epoch, revision) != (version.epoch, version.revision):
            raise QueryFailure(409, "stale_version", "The snapshot changed; restart pagination.")
    next_offset = offset + limit if offset + limit < len(items) else None
    return {
        "items": items[offset : offset + limit],
        "next_cursor": (
            None if next_offset is None else f"{version.epoch}:{version.revision}:{next_offset}"
        ),
        "page": {
            "limit": limit,
            "offset": offset,
            "total": len(items),
            "next_offset": next_offset,
            "at_epoch": version.epoch,
            "at_revision": version.revision,
        },
    }


def event_cursor(request: Request) -> EventCursor | None:
    raw = request.query_params.get("after")
    if raw is None:
        return None
    if not re.fullmatch(r"[0-9]{1,16}:[0-9]{1,16}", raw):
        raise QueryFailure(400, "invalid_query", "after must be epoch:sequence.")
    epoch, sequence = map(int, raw.split(":"))
    if max(epoch, sequence) > 9_007_199_254_740_991:
        raise QueryFailure(400, "invalid_query", "Event cursor exceeds the supported range.")
    return EventCursor(epoch=epoch, after_sequence=sequence)


def create_query_router(services: QueryServices | None = None) -> APIRouter:
    services = services or QueryServices()
    router = APIRouter()

    def endpoint(path: str, handler: Callable[[Request, Any], dict | Response]) -> None:
        def call(request: Request) -> Response:
            identity = request_id(request)
            try:
                actor = services.actor(request)
                body = handler(request, actor)
                if isinstance(body, Response):
                    body.headers["X-Request-Id"] = identity
                    body.headers["Cache-Control"] = "no-store"
                    return body
                return JSONResponse(
                    {**body, "request_id": identity},
                    headers={"X-Request-Id": identity, "Cache-Control": "no-store"},
                )
            except QueryFailure as error:
                return JSONResponse(
                    {"error": error.code, "detail": error.detail, "request_id": identity},
                    status_code=error.status,
                    headers={"X-Request-Id": identity, "Cache-Control": "no-store"},
                )
            except ProjectionError as error:
                return JSONResponse(
                    {"error": "provider_contract_error", "detail": str(error),
                     "request_id": identity}, status_code=502,
                    headers={"X-Request-Id": identity, "Cache-Control": "no-store"},
                )

        router.add_api_route(path, call, methods=["GET"], name="query_" + path.replace("/", "_"))

    def metadata(snapshot: Any) -> dict:
        result = {"version": snapshot.version.model_dump(mode="json"), "now": snapshot.now,
                  "logical_time": snapshot.now}
        if hasattr(snapshot, "global_scope"):
            result["scope"] = "global" if snapshot.global_scope else "owner"
            result["owner_id"] = None if snapshot.global_scope else snapshot.viewer.owner_id
        return result

    def actions(snapshot: Any, related: set[tuple[str, str]] | None = None) -> list[dict]:
        return legal_actions_json(snapshot, related, include_cp1=services.cp1_advisory_actions)

    def read_items(request: Request, actor: Any, kind: str) -> dict:
        snapshot = services.read(actor)
        if kind == "allocations":
            origin = services.origin()
            items = [allocation_json(x, origin) for x in snapshot.allocations]
        elif kind == "jobs":
            origin = services.origin()
            items = [job_json(x, origin) for x in snapshot.jobs]
        else:
            items = [x.model_dump(mode="json") for x in snapshot.intents]
        result = {
            **metadata(snapshot),
            **page(request, items, snapshot.version),
            "legal_actions": actions(snapshot),
        }
        result[kind] = result["items"]
        return result

    def detail(request: Request, actor: Any, kind: str, parameter: str) -> dict:
        key = request.path_params[parameter]
        if kind in {"allocation", "job"}:
            snapshot = services.read(actor, entities=(EntityRef(kind=kind, entity_id=key),),
                                     records_only=True)
        else:
            snapshot = services.read(actor)
        collection, attribute = {
            "allocation": (snapshot.allocations, "allocation_id"),
            "job": (snapshot.jobs, "job_id"),
            "intent": (snapshot.intents, "intent_id"),
        }[kind]
        item = next((x for x in collection if getattr(x, attribute) == key), None)
        if item is None:
            raise QueryFailure(404, "not_found", "The requested object was not found.")
        related = {(kind, key)}
        if kind == "intent":
            related.update(("allocation", x) for x in item.allocation_ids)
            related.update(("job", x) for x in item.job_ids)
        data = (
            allocation_json(item, services.origin()) if kind == "allocation"
            else job_json(item, services.origin()) if kind == "job"
            else item.model_dump(mode="json")
        )
        result = {
            **metadata(snapshot), "item": data,
            "legal_actions": actions(snapshot, related),
        }
        if kind in {"allocation", "job"}:
            # Frozen GET Allocation fields are present at the response root as well.
            result.update(data)
        if kind == "intent":
            result["intent"] = data
            result["allocations"] = [
                allocation_json(x, services.origin())
                for x in snapshot.allocations if x.allocation_id in item.allocation_ids
            ]
            result["jobs"] = [job_json(x, services.origin()) for x in snapshot.jobs
                              if x.job_id in item.job_ids]
            result["events"] = [
                x.model_dump(mode="json") for x in snapshot.events
                if any((entity.kind, entity.entity_id) in related for entity in x.entities)
            ]
        return result

    def capacity(request: Request, actor: Any) -> dict:
        inventory = services.inventory(actor)
        # Ownership identity is not required for public inventory discovery.
        nodes = [
            {**x.model_dump(mode="json", exclude={"contributor_owner_id"}), "id": x.node_id}
            for x in inventory.nodes
        ]
        paginated = page(request, nodes, inventory.version)
        return {
            **metadata(inventory), **paginated, "nodes": paginated["items"],
            "totals": resource_totals(inventory.nodes),
            "observed_at": timestamp(services.origin(), inventory.now),
        }

    def events(request: Request, actor: Any) -> dict:
        after = event_cursor(request)
        snapshot = services.read(actor, after=after)
        if (after is not None and snapshot.next_cursor is not None
                and after.after_sequence > snapshot.next_cursor.after_sequence):
            raise QueryFailure(409, "invalid_cursor", "Event cursor is beyond the committed sequence.")
        entries = sorted(
            (x for x in snapshot.events if after is None or x.sequence > after.after_sequence),
            key=lambda x: x.sequence,
        )
        limit = integer_parameter(request, "limit", 50, 500)
        if limit == 0:
            raise QueryFailure(400, "invalid_query", "limit must be between 1 and 500.")
        visible = entries[:limit]
        next_sequence = visible[-1].sequence if visible else (after.after_sequence if after else 0)
        # Cursor tracks delivered tenant events, never another owner's later sequence.
        records = [x.model_dump(mode="json") for x in visible]
        return {
            **metadata(snapshot),
            "items": records, "events": records,
            "next_cursor": {"epoch": snapshot.version.epoch, "after_sequence": next_sequence},
            "next_after": f"{snapshot.version.epoch}:{next_sequence}",
            "has_more": len(entries) > limit,
            "limit": limit,
        }

    def metrics(request: Request, actor: Any) -> dict:
        snapshot = services.read(actor)
        return {**metadata(snapshot), **metrics_json(snapshot)}

    def decision(request: Request, actor: Any) -> dict:
        snapshot = services.read(actor, global_scope=actor.role == "administrator")
        decision_id = request.path_params["decision_id"]
        plan = next((x for x in snapshot.decisions if x.decision_id == decision_id), None)
        if plan is None:
            raise QueryFailure(404, "not_found", "The requested decision was not found.")
        actual_events = tuple(x for x in snapshot.events if x.decision_id == decision_id)
        event_ids = {x.event_id for x in actual_events}
        receipts = [
            stage.receipt.model_dump(mode="json")
            for intent in snapshot.intents for stage in intent.stages
            if stage.receipt is not None and event_ids.intersection(stage.receipt.event_ids)
        ]
        return {
            **metadata(snapshot), "item": plan.model_dump(mode="json"),
            "actual_events": [x.model_dump(mode="json") for x in actual_events],
            "commit_receipts": receipts,
            "commit_status": "committed" if actual_events else "not_observed",
        }

    def contributions(request: Request, actor: Any) -> dict:
        if not actor.can_contribute_resources:
            raise QueryFailure(403, "forbidden", "Resource contribution authority is required.")
        inventory = services.inventory(actor)
        nodes = [
            x.model_dump(mode="json") for x in inventory.nodes
            if actor.role == "administrator" or x.contributor_owner_id == actor.owner_id
        ]
        return {**metadata(inventory), **page(request, nodes, inventory.version)}

    def overview(request: Request, actor: Any) -> dict:
        if actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        view = services.overview(actor)
        snapshot, inventory = view.snapshot, view.inventory
        return {
            **metadata(snapshot), **metrics_json(snapshot),
            "capacity": {"nodes": [x.model_dump(mode="json") for x in inventory.nodes],
                         "totals": resource_totals(inventory.nodes)},
        }

    def experiment_records(actor: Any) -> tuple[Any, tuple[EvidenceRun, ...]]:
        runner = services.experiments(actor)
        snapshot = services.read(actor)
        records = invoke(runner.list_runs, actor)
        try:
            runs = tuple(EvidenceRun.model_validate_json(row.run.model_dump_json())
                         for row in records)
        except (ValidationError, AttributeError, TypeError, ValueError):
            raise QueryFailure(502, "provider_contract_error", "Invalid experiment records.")
        return snapshot, runs

    def experiment(request: Request, actor: Any) -> dict:
        # Isolated runs belong to the administrator scope. Preserve the opaque
        # 404 for other owners without invoking a repository-wide evidence gate.
        if actor.role != "administrator":
            raise QueryFailure(404, "not_found", "The requested run was not found.")
        snapshot, runs = experiment_records(actor)
        run = next((run for run in runs
                    if run.run_id == request.path_params["run_id"]), None)
        if run is None:
            raise QueryFailure(404, "not_found", "The requested run was not found.")
        return {**metadata(snapshot), "item": run.model_dump(mode="json")}

    def experiment_catalog(request: Request, actor: Any) -> dict:
        runner = services.experiments(actor)
        return {"items": invoke(runner.catalog), "execution_mode": "logical_simulation"}

    def experiments(request: Request, actor: Any) -> dict:
        # Listing persisted runs must not execute checkpoint receipt validation;
        # that can recursively parse gigabytes of unrelated acceptance evidence.
        snapshot, runs = experiment_records(actor)
        # Run records advance independently of the operating ledger revision.
        # Do not pretend that its version pins experiment pagination. The
        # registered runner bounds retained runs, so return that complete list.
        return {**metadata(snapshot), "items": [
            run.model_dump(mode="json") for run in runs
        ]}

    def experiment_artifact(request: Request, actor: Any) -> Response:
        runner = services.experiments(actor)
        try:
            content, media_type = invoke(runner.artifact, actor,
                request.path_params["run_id"], request.path_params["artifact_id"])
        except KeyError:
            raise QueryFailure(404, "not_found", "The requested experiment artifact was not found.")
        except (ValueError, OSError):
            raise QueryFailure(409, "invalid_evidence", "The registered artifact failed validation.")
        if not isinstance(content, bytes) or media_type != "application/json":
            raise QueryFailure(502, "provider_contract_error", "Invalid experiment artifact.")
        return Response(content, media_type=media_type,
                        headers={"Content-Disposition": "attachment",
                                 "X-Content-Type-Options": "nosniff"})

    @router.post("/x/v1/experiments")
    async def submit_experiment(request: Request) -> Response:
        identity = request_id(request)
        headers = {"X-Request-Id": identity, "Cache-Control": "no-store"}
        try:
            actor = services.actor(request)
            runner = services.experiments(actor)
            try:
                raw = await request.body()
                if len(raw) > 8192:
                    raise ValueError("oversize")
                body = json.loads(raw)
                if (not isinstance(body, dict) or set(body) - {"scenario_id", "policy", "seed"}
                        or not isinstance(body.get("scenario_id"), str)
                        or body.get("policy") not in {"B0", "B1", "B2", "P"}
                        or type(body.get("seed", 0)) is not int):
                    raise ValueError("invalid input")
            except (ValueError, TypeError, UnicodeDecodeError):
                raise QueryFailure(400, "invalid_request", "Expected registered scenario_id, policy and integer seed.")
            try:
                value = await asyncio.to_thread(invoke, lambda: runner.submit(
                    actor=actor, scenario_id=body["scenario_id"],
                    policy=body["policy"], seed=body.get("seed", 0)))
            except KeyError:
                raise QueryFailure(404, "not_found", "The registered scenario was not found.")
            except ExperimentBuildChangedError:
                raise QueryFailure(409, "stale_version",
                                   "Source build changed since service startup; restart the service before submitting experiments.")
            except ValueError:
                raise QueryFailure(400, "invalid_request", "Experiment parameters or run capacity were rejected.")
            try:
                run = EvidenceRun.model_validate_json(value.model_dump_json())
            except (ValidationError, AttributeError):
                raise QueryFailure(502, "provider_contract_error", "Invalid experiment registration.")
            return JSONResponse({"item": run.model_dump(mode="json"), "request_id": identity},
                                status_code=202, headers=headers)
        except QueryFailure as error:
            return JSONResponse({"error": error.code, "detail": error.detail, "request_id": identity},
                                status_code=error.status, headers=headers)

    def acceptance(request: Request, actor: Any) -> dict:
        if actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        evidence = services.evidence(actor)
        return {
            **metadata(evidence), "items": [x.model_dump(mode="json") for x in evidence.checks],
            "build_matches": evidence.build_matches,
            "status": "NOT_RUN" if not evidence.checks or all(
                x.status == "NOT_RUN" for x in evidence.checks
            ) else (
                "MET" if evidence.build_matches is True
                and all(x.status == "MET" for x in evidence.checks) else "NOT_MET"
            ),
        }

    def policy_validation(request: Request, actor: Any) -> dict:
        reader = services.policy_validation(actor)
        try:
            return invoke(reader.snapshot)
        except (ValueError, OSError, KeyError, TypeError):
            raise QueryFailure(409, "invalid_evidence",
                               "CP3 evidence is incomplete, corrupt or belongs to another build.")

    def policy_validation_artifact(request: Request, actor: Any) -> Response:
        reader = services.policy_validation(actor)
        try:
            content = invoke(reader.artifact, request.path_params["artifact_id"])
        except KeyError:
            raise QueryFailure(404, "not_found", "The registered CP3 artifact was not found.")
        except (ValueError, OSError, TypeError):
            raise QueryFailure(409, "invalid_evidence",
                               "The CP3 artifact failed build or content validation.")
        return Response(content, media_type="application/json",
                        headers={"Content-Disposition": "attachment",
                                 "X-Content-Type-Options": "nosniff"})

    def reclaim_validation(request: Request, actor: Any) -> dict | Response:
        if actor.role != "administrator":
            raise QueryFailure(403, "forbidden", "Administrator authority is required.")
        from campus_fabric.workflows import reclaim_validation as report

        artifact_id = request.path_params.get("artifact_id")
        try:
            if artifact_id is None:
                return report.snapshot()
            content = report.artifact(artifact_id)
        except KeyError:
            if artifact_id is not None and artifact_id not in report.FILES:
                raise QueryFailure(404, "not_found", "The reclaim report artifact was not found.")
            raise QueryFailure(409, "invalid_evidence", "The reclaim report is incomplete.")
        except (ValueError, OSError, TypeError):
            raise QueryFailure(409, "invalid_evidence", "The reclaim report failed validation.")
        return Response(content, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="reclaim-{artifact_id}.json"',
            "X-Content-Type-Options": "nosniff",
        })

    def artifact(request: Request, actor: Any) -> Response:
        intent_id, artifact_id = request.path_params["intent_id"], request.path_params["artifact_id"]
        snapshot = services.read(actor, global_scope=actor.role == "administrator")
        if not any(x.intent_id == intent_id for x in snapshot.intents):
            raise QueryFailure(404, "not_found", "The requested artifact was not found.")
        if services.artifact_reader is None:
            raise unavailable("Registered artifact reader")
        value = invoke(services.artifact_reader, actor, intent_id, artifact_id)
        if (value is None or value.intent_id != intent_id or value.artifact_id != artifact_id
                or (actor.role != "administrator" and value.owner_id != actor.owner_id)):
            raise QueryFailure(404, "not_found", "The requested artifact was not found.")
        return Response(value.content, media_type=value.media_type,
                        headers={"Content-Disposition": "attachment",
                                 "X-Content-Type-Options": "nosniff"})

    endpoint("/v1/capacity", capacity)
    endpoint("/v1/allocations", lambda r, a: read_items(r, a, "allocations"))
    endpoint("/v1/allocations/{allocation_id}", lambda r, a: detail(r, a, "allocation", "allocation_id"))
    endpoint("/v1/jobs", lambda r, a: read_items(r, a, "jobs"))
    endpoint("/v1/jobs/{job_id}", lambda r, a: detail(r, a, "job", "job_id"))
    endpoint("/v1/events", events)
    endpoint("/v1/metrics", metrics)
    endpoint("/x/v1/work-intents", lambda r, a: read_items(r, a, "intents"))
    endpoint("/x/v1/work-intents/{intent_id}", lambda r, a: detail(r, a, "intent", "intent_id"))
    endpoint("/x/v1/work-intents/{intent_id}/artifacts/{artifact_id}", artifact)
    endpoint("/x/v1/decisions/{decision_id}", decision)
    endpoint("/x/v1/contributions", contributions)
    endpoint("/x/v1/admin/overview", overview)
    endpoint("/x/v1/experiments/catalog", experiment_catalog)
    endpoint("/x/v1/experiments", experiments)
    endpoint("/x/v1/experiments/{run_id}", experiment)
    endpoint("/x/v1/experiments/{run_id}/artifacts/{artifact_id}", experiment_artifact)
    endpoint("/x/v1/acceptance", acceptance)
    endpoint("/x/v1/policy-validation", policy_validation)
    endpoint("/x/v1/policy-validation/artifacts/{artifact_id}", policy_validation_artifact)
    endpoint("/x/v1/reclaim-validation", reclaim_validation)
    endpoint("/x/v1/reclaim-validation/artifacts/{artifact_id}", reclaim_validation)
    return router


def register_queries(
    app: FastAPI, services: QueryServices | None = None, *, replace_existing: bool = False
) -> None:
    """Install B reads and isolated experiment submission, retaining A commands.

    Duplicate routes fail unless the host explicitly replaces its B adapters.
    The A app remains unchanged until its assembly owner opts in.
    """
    router = create_query_router(services)
    methods = {}
    for route in router.routes:
        methods.setdefault(route.path, set()).update(route.methods)
    conflicts = [route for route in app.router.routes
                 if methods.get(getattr(route, "path", None), set())
                 & getattr(route, "methods", set())]
    if conflicts and not replace_existing:
        raise ValueError("Query routes already registered; explicitly replace existing GET bridges.")
    if replace_existing:
        for route in conflicts:
            replaced = methods[route.path] | ({"HEAD"} if "GET" in methods[route.path] else set())
            remaining = route.methods - replaced
            if remaining:
                route.methods = remaining
            else:
                app.router.routes.remove(route)
    app.include_router(router)
    app.openapi_schema = None
