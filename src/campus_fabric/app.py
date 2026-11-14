"""Assembly seam for A's commands and B's projections; no CP1 state engine yet."""

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from campus_fabric import __version__

ROOT = Path(__file__).resolve().parents[2]


def pending_handler(operation: dict):
    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={
                "error": "not_implemented",
                "detail": "CP0 contract scaffold; this operation has no business implementation.",
                "operation_id": operation["operation_id"],
                "owner": operation["owner"],
                "checkpoint": "CP0",
            },
        )

    return endpoint


def create_app() -> FastAPI:
    app = FastAPI(
        title="Campus Fabric — CP0 development host",
        version=__version__,
        description="Development scaffold, not a competition-conformant Server.",
    )

    @app.get("/healthz")
    def health() -> dict:
        return {"status": "ok", "checkpoint": "CP0", "control_plane_ready": False}

    @app.get("/v1/version")
    def version() -> dict:
        return {
            "api_version": "2.0.0",
            "implementation_version": __version__,
            "checkpoint": "CP0",
            "capabilities": {
                "contracts": True,
                "resource_ledger": False,
                "scheduler": False,
                "checkpoint_recovery": False,
                "real_gpu_execution": False,
            },
        }

    catalogue = json.loads((ROOT / "contracts/api_ownership.json").read_text())
    for operation in catalogue["operations"]:
        if operation["path"] in {"/healthz", "/v1/version"}:
            continue
        app.add_api_route(
            operation["path"],
            pending_handler(operation),
            methods=[operation["method"]],
            name=operation["operation_id"],
            description="Unimplemented CP0 seam; returns 501 without state effects.",
        )
    return app
