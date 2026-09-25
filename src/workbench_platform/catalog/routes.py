"""Actual platform catalog HTTP routes backed by trusted in-process registry."""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from packages.contracts.models import AppHealth, AppManifest, AppManifestPage, Error, PageQuery
from .registry import AppRegistry, DEFAULT_CONFIG, SnapshotExpired

router = APIRouter(prefix="/api/platform/v1/apps", tags=["applications"])
_registry = AppRegistry(DEFAULT_CONFIG)
CURSOR = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def get_registry() -> AppRegistry:
    return _registry


def contract_error(status: int, code: str, message: str) -> JSONResponse:
    error = Error(schema_version="1.0", code=code, message=message, retryable=False,
                  request_id="r" + uuid.uuid4().hex, details={})
    return JSONResponse(status_code=status, content=error.model_dump(mode="json"))


@router.get("", response_model=AppManifestPage)
def list_apps(request: Request, limit: int = Query(..., ge=1, le=200),
              cursor: str | None = Query(None, min_length=1, max_length=256),
              snapshot_revision: int | None = Query(None, ge=1),
              registry: AppRegistry = Depends(get_registry)):
    permitted = {"limit", "cursor", "snapshot_revision"}
    if any(key not in permitted or len(request.query_params.getlist(key)) != 1
           for key in request.query_params):
        return contract_error(400, "VALIDATION_FAILED", "Invalid catalog query")
    if cursor is not None and not CURSOR.fullmatch(cursor):
        return contract_error(400, "VALIDATION_FAILED", "Invalid catalog query")
    query = PageQuery(schema_version="1.0", limit=limit, cursor=cursor,
                      snapshot_revision=snapshot_revision)
    try:
        return registry.list_page(query)
    except SnapshotExpired:
        return contract_error(409, "SNAPSHOT_EXPIRED", "Catalog snapshot is unavailable")
    except ValueError:
        return contract_error(400, "VALIDATION_FAILED", "Catalog cursor requires a snapshot")


@router.get("/{app_id}", response_model=AppManifest)
def get_app(app_id: str, registry: AppRegistry = Depends(get_registry)):
    manifest = registry.manifests.get(app_id)
    return manifest if manifest is not None else contract_error(404, "NOT_FOUND", "Application is not registered")


@router.get("/{app_id}/health", response_model=AppHealth)
def get_health(app_id: str, registry: AppRegistry = Depends(get_registry)):
    health = registry.health(app_id)
    return health if health is not None else contract_error(404, "NOT_FOUND", "Application is not registered")
