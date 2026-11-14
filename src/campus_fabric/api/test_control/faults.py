"""Bounded transport faults, selected by frozen method and route template.

A rule is claimed durably by the kernel after transport authentication, before
request parsing or a domain command. Latency is wall time only. Neither extensions
nor test controls can be targeted, so the recovery control remains reachable.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.routing import compile_path


def fault_targets(catalogue):
    return {
        f"{item['method']} {item['path']}": compile_path(item["path"])[0]
        for item in catalogue["operations"] if item["audience"] == "public_api"
    }


def fault_parameters(body, targets):
    if set(body) - {"operation", "mode", "count", "delay_ms"}:
        raise HTTPException(400, "Unsupported request field")
    operation = body.get("operation")
    if not isinstance(operation, str) or operation not in targets:
        raise HTTPException(400, "operation must match a frozen public METHOD and route template")
    mode = body.get("mode")
    # The frozen YAML contains numeric status-code enum values despite type:string.
    if type(mode) is int and mode in {429, 500, 503}:
        mode = str(mode)
    if not isinstance(mode, str) or mode not in {"none", "429", "500", "503", "latency"}:
        raise HTTPException(400, "Unsupported fault mode")
    for name, default, maximum in (("count", 1, 20), ("delay_ms", 0, 30000)):
        value = body.get(name, default)
        if type(value) is not int or not 0 <= value <= maximum:
            raise HTTPException(400, f"{name} must be an integer from 0 to {maximum}")
    return {"kind": "test.fault", "target_operation": operation, "mode": mode,
            "count": body.get("count", 1), "delay_ms": body.get("delay_ms", 0)}


async def intercept_fault(request, kernel, identity, targets):
    target = next((operation for operation, pattern in targets.items()
                   if operation.split(" ", 1)[0] == request.method
                   and pattern.fullmatch(request.url.path)), None)
    if target is None:
        return None
    # Public metadata already permits anonymous access. For protected routes,
    # invalid credentials must neither consume the rule nor mask authorization.
    if request.url.path not in {"/healthz", "/v1/version"}:
        identity.actor(request)
    fault = kernel.take_fault(target)
    if fault is None or fault["mode"] == "none":
        return None
    if fault["mode"] == "latency":
        await asyncio.sleep(fault["delay_ms"] / 1000)
        return None
    status = int(fault["mode"])
    return JSONResponse(status_code=status, content={
        "error": {429: "rate_limited", 500: "internal_error", 503: "service_unavailable"}[status],
        "detail": "Configured test transport fault; no domain command was executed.",
        "request_id": request.state.request_id,
    })
