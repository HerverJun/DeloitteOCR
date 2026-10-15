"""Local workspace routes backed by the trusted platform SQLite ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError

from packages.contracts.models import Workspace, WorkspaceCreate, WorkspacePage
from workbench_platform.catalog.routes import contract_error
from workbench_platform.store import WorkspaceStore
from workbench_platform.store.workspaces import RevisionConflict

router = APIRouter(prefix="/api/platform/v1/workspaces", tags=["workspaces"])
ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
CURSOR = re.compile(r"^(0|[1-9][0-9]{0,8})$")
MAX_SAFE = (1 << 53) - 1


def configure_workspace_store(app) -> None:
    """Called at process startup; request data never chooses a database path."""
    if os.environ.get('CLEAR_EXECUTION_MODE') == 'worker':
        from workbench_platform.tasks.database import engine_for, check_schema
        from workbench_platform.tasks.workspaces import CentralWorkspaceStore
        engine = engine_for()
        check_schema(engine)
        app.state.workspace_store = CentralWorkspaceStore(engine)
        return
    configured = os.environ.get("WORKBENCH_DATA_DIR")
    if not configured:
        app.state.workspace_store = None
        return
    directory = Path(configured).resolve()
    if not directory.is_dir():
        raise RuntimeError("Configured WORKBENCH_DATA_DIR must already exist")
    app.state.workspace_store = WorkspaceStore(directory / "workspaces.sqlite3")


def get_store(request: Request) -> WorkspaceStore | None:
    return getattr(request.app.state, "workspace_store", None)


def _revision(items: list[Workspace]) -> int:
    # Hash the entire ordered result; mapping into 53 bits keeps JSON exact in JS.
    data = json.dumps([item.model_dump(mode="json") for item in items],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "big") % MAX_SAFE + 1


def _loopback_host(request: Request) -> bool:
    host = request.headers.get("host", "")
    try:
        hostname, separator, port = host.partition(":")
        return (hostname in {"localhost", "127.0.0.1"}
                and (not separator or port.isdecimal() and 1 <= int(port) <= 65535))
    except ValueError:
        return False


def _same_loopback_origin(request: Request) -> bool:
    host = request.headers.get("host", "")
    try:
        parsed = urlsplit(request.headers.get("origin", ""))
        return (_loopback_host(request) and parsed.scheme == "http" and parsed.netloc == host
                and parsed.port == (int(host.partition(":")[2]) if ":" in host else None)
                and parsed.hostname == host.partition(":")[0] and parsed.path == ""
                and parsed.query == "" and parsed.fragment == ""
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


@router.get("", response_model=WorkspacePage)
def list_workspaces(request: Request, limit: int = Query(..., ge=1, le=200),
                    cursor: str | None = Query(None, min_length=1, max_length=256),
                    snapshot_revision: int | None = Query(None, ge=1, le=MAX_SAFE),
                    store: WorkspaceStore | None = Depends(get_store)):
    if not _loopback_host(request):
        return contract_error(403, "VALIDATION_FAILED", "Local request required")
    permitted = {"limit", "cursor", "snapshot_revision"}
    if any(key not in permitted or len(request.query_params.getlist(key)) != 1
           for key in request.query_params):
        return contract_error(400, "VALIDATION_FAILED", "Invalid workspace query")
    if cursor is not None and (not CURSOR.fullmatch(cursor) or snapshot_revision is None):
        return contract_error(400, "VALIDATION_FAILED", "Invalid workspace cursor")
    if store is None:
        return contract_error(503, "APP_UNAVAILABLE", "Workspace store is not configured")
    # WorkspaceStore.list_workspaces uses one ordered SQL query for the page source.
    items = [Workspace.model_validate(asdict(item)) for item in store.list_workspaces()]
    revision = _revision(items)
    if snapshot_revision is not None and snapshot_revision != revision:
        return contract_error(409, "SNAPSHOT_EXPIRED", "Workspace snapshot is unavailable")
    offset = int(cursor) if cursor is not None else 0
    if offset > len(items):
        return contract_error(400, "VALIDATION_FAILED", "Invalid workspace cursor")
    next_offset = offset + limit
    return WorkspacePage(schema_version="1.0", items=items[offset:next_offset],
                         next_cursor=str(next_offset) if next_offset < len(items) else None,
                         snapshot_revision=revision)


@router.get("/{workspace_id}", response_model=Workspace)
def get_workspace(workspace_id: str, request: Request, store: WorkspaceStore | None = Depends(get_store)):
    if not _loopback_host(request):
        return contract_error(403, "VALIDATION_FAILED", "Local request required")
    if not ID.fullmatch(workspace_id):
        return contract_error(400, "VALIDATION_FAILED", "Invalid workspace identity")
    if store is None:
        return contract_error(503, "APP_UNAVAILABLE", "Workspace store is not configured")
    item = store.get_workspace(workspace_id)
    return Workspace.model_validate(asdict(item)) if item else contract_error(404, "NOT_FOUND", "Workspace not found")


@router.post("", response_model=Workspace, status_code=201)
async def create_workspace(request: Request, store: WorkspaceStore | None = Depends(get_store)):
    if not _same_loopback_origin(request):
        return contract_error(403, "VALIDATION_FAILED", "Local same-origin request required")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return contract_error(400, "VALIDATION_FAILED", "JSON request required")
    if store is None:
        return contract_error(503, "APP_UNAVAILABLE", "Workspace store is not configured")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            return contract_error(400, "VALIDATION_FAILED", "Invalid workspace request")
    try:
        payload = WorkspaceCreate.model_validate_json(body)
        if not payload.name.strip() or "\x00" in payload.name:
            raise ValueError("invalid name")
    except (ValidationError, ValueError):
        return contract_error(400, "VALIDATION_FAILED", "Invalid workspace request")
    try:
        return Workspace.model_validate(asdict(store.create_workspace(payload.name, request_id=payload.request_id)))
    except RevisionConflict:
        return contract_error(409, 'IDEMPOTENCY_CONFLICT', "创建请求已用于其他内容，请核对原项目")
