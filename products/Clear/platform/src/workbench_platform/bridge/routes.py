"""Mountable local HTTP issuance route; application mounting belongs to T014."""

from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from packages.contracts.models import ContextIssueRequest, OpenContext
from workbench_platform.catalog.routes import contract_error
from workbench_platform.workspaces.routes import ID, _same_loopback_origin

from .context import BridgeDenied, ContextBridge


def bridge_router(bridge: ContextBridge | Callable[[Request], ContextBridge | None]) -> APIRouter:
    router = APIRouter(tags=["bridge"])

    @router.post("/api/platform/v1/workspaces/{workspace_id}/open-contexts",
                 response_model=OpenContext, status_code=201)
    async def issue(workspace_id: str, request: Request):
        if not _same_loopback_origin(request):
            return contract_error(403, "VALIDATION_FAILED", "Local same-origin request required")
        if not ID.fullmatch(workspace_id) or request.query_params:
            return contract_error(400, "VALIDATION_FAILED", "Invalid context request")
        active = bridge(request) if callable(bridge) else bridge
        if active is None:
            return contract_error(503, "APP_UNAVAILABLE", "Context bridge is unavailable")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return contract_error(400, "VALIDATION_FAILED", "JSON request required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return contract_error(400, "VALIDATION_FAILED", "Invalid context request")
        try:
            payload = json.loads(body, object_pairs_hook=_unique)
            issued = await run_in_threadpool(active.issue, ContextIssueRequest.model_validate(payload),
                                             workspace_id=workspace_id)
            return issued
        except (ValueError, ValidationError, BridgeDenied) as error:
            if isinstance(error, BridgeDenied):
                status = 503 if error.code == "APP_UNAVAILABLE" else 400
                return contract_error(status, error.code, "Context is unavailable")
            return contract_error(400, "VALIDATION_FAILED", "Invalid context request")

    return router


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value
