"""CP1 assembly: one persistent command kernel and replaceable read adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from campus_fabric import __version__
from campus_fabric.api.mutations.http import assign_request_id, mount_cp1
from campus_fabric.auth.http import HTTPIdentity
from campus_fabric.contracts.models import NodeInventory
from campus_fabric.domain.kernel import Kernel

ROOT = Path(__file__).resolve().parents[2]


def pending_handler(operation):
    async def endpoint(request: Request):
        # Test endpoints remain unavailable rather than pretending CP2 is complete.
        if operation["path"].startswith("/_test/"):
            request.app.state.identity.test_actor(request)
        else:
            request.app.state.identity.actor(request)
        return JSONResponse(status_code=501, content={
            "error": "not_implemented",
            "detail": "This operation belongs to CP2 and is not implemented in CP1.",
            "operation_id": operation["operation_id"], "owner": operation["owner"],
            "checkpoint": "CP1", "request_id": request.state.request_id,
        })
    return endpoint


def create_app(data_dir=None, inventory=(), test_key=None, auth_tokens=None,
               simulation_duration_seconds=10.0, scheduling_mode=None) -> FastAPI:
    mode = os.environ.get("FABRIC_MODE", "logical_simulation")
    if mode != "logical_simulation":
        raise ValueError("CP1 supports logical_simulation only; CPU execution is not implemented")
    if data_dir is None:
        instance = os.environ.get("FABRIC_INSTANCE", "local")
        if not instance or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in instance):
            raise ValueError("FABRIC_INSTANCE must be a simple instance name")
        data_dir = os.environ.get("FABRIC_DATA_DIR") or ROOT / "runtime" / instance
    data_dir = Path(data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    inventory_file = os.environ.get("FABRIC_INVENTORY_FILE")
    if inventory_file and not inventory:
        raw = json.loads(Path(inventory_file).expanduser().read_text())
        if isinstance(raw, dict):
            raw = raw["nodes"]
        if not isinstance(raw, list):
            raise ValueError("FABRIC_INVENTORY_FILE must contain a node array or {nodes: [...]}.")
        inventory = tuple(NodeInventory.model_validate_json(json.dumps(item)) for item in raw)
    else:
        inventory = tuple(item if isinstance(item, NodeInventory) else
                          NodeInventory.model_validate_json(json.dumps(item)) for item in inventory)
    test_key = test_key if test_key is not None else os.environ.get("FABRIC_TEST_KEY")
    if auth_tokens is None and os.environ.get("FABRIC_AUTH_FILE"):
        auth_tokens = json.loads(Path(os.environ["FABRIC_AUTH_FILE"]).expanduser().read_text())
    identity = HTTPIdentity(auth_tokens, test_key=test_key)
    scheduling_mode = scheduling_mode or os.environ.get("FABRIC_SCHEDULING_MODE", "builtin")
    kernel = Kernel(data_dir / "fabric.sqlite3", inventory=inventory,
                    simulation_duration_seconds=simulation_duration_seconds,
                    scheduling_mode=scheduling_mode)
    app = FastAPI(title="Campus Fabric — CP1 control plane", version=__version__,
                  description="Persistent logical simulation; CP2 recovery and real execution pending.")
    app.state.kernel = kernel
    app.state.identity = identity

    @app.middleware("http")
    async def correlation(request, call_next):
        try:
            assign_request_id(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code,
                                content={"error": "invalid_request", "detail": exc.detail})
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={
            "error": {400: "invalid_request", 401: "unauthorized", 403: "forbidden",
                      404: "not_found", 409: "conflict"}.get(exc.status_code, "request_failed"),
            "detail": exc.detail, "request_id": getattr(request.state, "request_id", None),
        })

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=400, content={
            "error": "invalid_request", "detail": "Request does not satisfy the API contract",
            "request_id": getattr(request.state, "request_id", None),
        })

    @app.get("/healthz")
    def health():
        kernel.decision_snapshot()  # Exercise a coherent persisted read.
        return {"status": "ok", "checkpoint": "CP1", "control_plane_ready": True,
                "checkpoint_passed": False}

    @app.get("/v1/version")
    def version():
        return {"api_version": "2.0.0", "implementation_version": __version__,
                "checkpoint": "CP1", "checkpoint_passed": False,
                "execution_mode": "logical_simulation", "scheduling_mode": scheduling_mode,
                "capabilities": {
                    "contracts": True, "resource_ledger": True,
                    "scheduler": True, "checkpoint_recovery": False,
                    "real_gpu_execution": False, "real_cpu_execution": False,
                }}

    mount_cp1(app, kernel, identity)
    existing = {(method, route.path) for route in app.routes
                for method in (getattr(route, "methods", None) or [])}
    catalogue = json.loads((ROOT / "contracts/api_ownership.json").read_text())
    for operation in catalogue["operations"]:
        if (operation["method"], operation["path"]) not in existing:
            app.add_api_route(operation["path"], pending_handler(operation),
                              methods=[operation["method"]], name=operation["operation_id"],
                              description="CP2 operation; honest 501 after authentication.")
    return app
