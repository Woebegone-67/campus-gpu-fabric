"""CP2 assembly: one persistent command kernel and replaceable read adapters."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from campus_fabric import __version__
from campus_fabric.api.mutations.http import assign_request_id, mount_cp1
from campus_fabric.api.test_control.faults import fault_targets, intercept_fault
from campus_fabric.auth.http import HTTPIdentity
from campus_fabric.contracts.models import NodeInventory
from campus_fabric.domain.kernel import Kernel

ROOT = Path(__file__).resolve().parents[2]


def create_app(data_dir=None, inventory=(), test_key=None, auth_tokens=None,
               simulation_duration_seconds=10.0, scheduling_mode=None,
               execution_mode=None, cpu_config=None) -> FastAPI:
    mode = execution_mode or os.environ.get("FABRIC_MODE", "logical_simulation")
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
                    scheduling_mode=scheduling_mode, execution_mode=mode)
    app = FastAPI(title="Campus Fabric — CP2 control plane", version=__version__,
                  description="Persistent leases, interruption recovery and bounded fault controls.")
    app.state.kernel = kernel
    app.state.identity = identity
    app.state.cpu_driver = None
    if mode == "cpu_reference":
        from campus_fabric.executors.cpu_reference import CpuReferenceExecutor
        from campus_fabric.executors.driver import CpuDriver
        executor = CpuReferenceExecutor(data_dir / "cpu-executor", config=cpu_config)
        driver = CpuDriver(kernel, executor, lock_path=data_dir / "cpu-driver.lock")
        app.state.cpu_driver = driver

        @asynccontextmanager
        async def cpu_lifespan(_app):
            driver.start()
            async def run():
                while True:
                    try:
                        await asyncio.to_thread(driver.tick)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logging.getLogger(__name__).exception("CPU driver reconciliation failed")
                    await asyncio.sleep(.05)
            task = asyncio.create_task(run())
            try:
                yield
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await asyncio.to_thread(driver.close)
        app.router.lifespan_context = cpu_lifespan

    catalogue = json.loads((ROOT / "contracts/api_ownership.json").read_text())
    app.state.fault_targets = fault_targets(catalogue)

    @app.middleware("http")
    async def correlation(request, call_next):
        try:
            assign_request_id(request)
            if app.state.cpu_driver is not None and request.url.path in {"/_test/clock", "/x/v1/admin/clock"}:
                await asyncio.to_thread(app.state.cpu_driver.tick)
            response = await intercept_fault(request, kernel, identity, app.state.fault_targets)
            if response is None:
                response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={
                "error": {401: "unauthorized", 403: "forbidden"}.get(
                    exc.status_code, "invalid_request"),
                "detail": exc.detail, "request_id": getattr(request.state, "request_id", None),
            })
        if hasattr(request.state, "request_id"):
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
        return {"status": "ok", "checkpoint": "CP2", "control_plane_ready": True,
                "checkpoint_passed": False}

    @app.get("/v1/version")
    def version():
        return {"api_version": "2.0.0", "implementation_version": __version__,
                "checkpoint": "CP2", "checkpoint_passed": False,
                "execution_mode": mode, "scheduling_mode": scheduling_mode,
                "capabilities": {
                    "contracts": True, "resource_ledger": True,
                    "scheduler": True, "checkpoint_recovery": True,
                    "real_gpu_execution": False, "real_cpu_execution": mode == "cpu_reference",
                }}

    @app.get("/x/v1/artifacts")
    def artifact(request: Request, uri: str):
        actor = identity.actor(request)
        if app.state.cpu_driver is None or actor.owner_id is None:
            raise HTTPException(404, "Artifact not found")
        from campus_fabric.executors.cpu_reference import ExecutorRejected
        try:
            return app.state.cpu_driver.executor.read_artifact(
                uri, owner_id=actor.owner_id, epoch=kernel.store.read()["epoch"])
        except (ExecutorRejected, OSError):
            raise HTTPException(404, "Artifact not found") from None

    mount_cp1(app, kernel, identity)
    existing = {(method, route.path) for route in app.routes
                for method in (getattr(route, "methods", None) or [])}
    missing = [item["operation_id"] for item in catalogue["operations"]
               if (item["method"], item["path"]) not in existing]
    if missing:
        raise RuntimeError(f"Frozen API operations are not mounted: {missing}")
    return app
