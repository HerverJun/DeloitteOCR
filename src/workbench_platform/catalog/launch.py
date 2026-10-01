"""Local HTTP entry backed by one Supervisor per platform lifespan."""

from __future__ import annotations

import http.client
import re
import sqlite3
import threading

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from packages.contracts.models import AppInstallation, LaunchReceipt, RecentWorkPage
from workbench_platform.catalog.routes import contract_error
from workbench_platform.store import WorkspaceStore
from workbench_platform.supervisor.processes import (
    LaunchFailure, Supervisor, _identity, _listener_pid, _loopback_port, _process_active,
)
from workbench_platform.workspaces.routes import _loopback_host, _same_loopback_origin

from .recent import LaunchIdConflict, RecentSnapshotExpired, RecentWorkStore

ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
CURSOR = re.compile(r"^(0|[1-9][0-9]{0,8})$")


class LaunchStatus(ValueError):
    def __init__(self, status: int, code: str):
        super().__init__("Application launch is unavailable")
        self.status, self.code = status, code


class LaunchService:
    def __init__(self, supervisor: Supervisor, recent: RecentWorkStore | None):
        self.supervisor, self.recent = supervisor, recent
        self._lock = threading.RLock()

    def _verify(self, entry, receipt: LaunchReceipt) -> LaunchReceipt:
        supervisor = self.supervisor
        with supervisor._lock:
            revision = supervisor.registry._snapshot.revision
            try:
                if not _process_active(receipt.pid, receipt.creation_time):
                    raise LaunchStatus(410, "APP_UNAVAILABLE")
                if (supervisor._closed or entry.identity[2] != revision or
                        supervisor._current.get(receipt.app_id) != (revision, receipt.instance_id)):
                    raise LaunchStatus(503, "APP_UNAVAILABLE")
                port = _loopback_port(receipt.base_url)
                if (_listener_pid(port) != receipt.pid or
                        (receipt.ownership == "owned" and (entry.job is None or not entry.job.contains(receipt.pid))) or
                        _identity(port, receipt.app_id, receipt.instance_id,
                                  receipt.handshake_nonce) != receipt.version):
                    raise LaunchStatus(503, "APP_UNAVAILABLE")
            except LaunchStatus:
                raise
            except (LaunchFailure, OSError, TimeoutError, http.client.HTTPException,
                    UnicodeError, ValueError):
                raise LaunchStatus(503, "APP_UNAVAILABLE") from None
            return receipt

    def _entry_receipt(self, entry) -> LaunchReceipt:
        if not entry.future.done() or entry.future.exception() is not None:
            raise LaunchStatus(410, "APP_UNAVAILABLE")
        return self._verify(entry, entry.future.result())

    def current(self, app_id: str) -> LaunchReceipt:
        if app_id not in self.supervisor.registry.manifests:
            raise LaunchStatus(404, "NOT_FOUND")
        with self.supervisor._lock:
            current = self.supervisor._current.get(app_id)
            candidates = [(entry, entry.future.result()) for entry in self.supervisor._entries.values()
                          if entry.future.done() and entry.future.exception() is None
                          and entry.future.result().app_id == app_id]
            matching = [(entry, receipt) for entry, receipt in candidates
                        if current == (entry.identity[2], receipt.instance_id)]
        if len(matching) != 1:
            # An inconsistent current index is not proof that an owned process
            # exited. Keep the launch slot blocked while any candidate is live.
            for entry, receipt in candidates:
                try:
                    self._verify(entry, receipt)
                except LaunchStatus as error:
                    if error.status != 410:
                        raise
                else:
                    raise LaunchStatus(503, "APP_UNAVAILABLE")
            raise LaunchStatus(404, "NOT_FOUND")
        try:
            return self._verify(*matching[0])
        except LaunchStatus as error:
            if error.status == 410:
                raise LaunchStatus(404, "NOT_FOUND") from None
            raise

    def get(self, app_id: str, launch_id: str) -> LaunchReceipt:
        if app_id not in self.supervisor.registry.manifests:
            raise LaunchStatus(404, "NOT_FOUND")
        with self.supervisor._lock:
            entry = self.supervisor._entries.get(launch_id)
            if entry is None or entry.identity[1] != app_id:
                raise LaunchStatus(404, "NOT_FOUND")
        return self._entry_receipt(entry)

    def start(self, app_id: str, launch_id: str) -> tuple[LaunchReceipt, bool]:
        if app_id not in self.supervisor.registry.manifests:
            raise LaunchStatus(404, "NOT_FOUND")
        if self.recent is None:
            # Without durable ID reservation, a restart could replay an old ID
            # into a different owned process. A workspace is not required, but
            # the platform's trusted data directory is.
            raise LaunchStatus(503, "APP_UNAVAILABLE")
        with self._lock:
            with self.supervisor._lock:
                old = self.supervisor._entries.get(launch_id)
            if old is not None:
                if old.identity[1] != app_id:
                    raise LaunchStatus(409, "IDEMPOTENCY_CONFLICT")
                receipt = self._entry_receipt(old)
                try:
                    self.recent.record_launch(receipt)  # Recover an earlier storage failure.
                except (sqlite3.Error, OSError, ValueError):
                    raise LaunchStatus(503, "APP_UNAVAILABLE") from None
                return receipt, False
            try:
                self.current(app_id)
            except LaunchStatus as error:
                if error.status != 404:
                    raise
            else:
                # The requested ID has no owned Job. The original launch ID
                # remains the only key for the live receipt; caller may GET it.
                raise LaunchStatus(409, "RESOURCE_BUSY")
            try:
                if not self.recent.reserve_launch(app_id, launch_id):
                    raise LaunchStatus(410, "APP_UNAVAILABLE")
                receipt = self.supervisor.start(app_id, launch_id)
                with self.supervisor._lock:
                    entry = self.supervisor._entries[launch_id]
                receipt = self._verify(entry, receipt)
                self.recent.record_launch(receipt)
                return receipt, True
            except LaunchIdConflict:
                raise LaunchStatus(409, "IDEMPOTENCY_CONFLICT") from None
            except (LaunchFailure, sqlite3.Error, OSError, ValueError) as error:
                if isinstance(error, LaunchStatus):
                    raise
                raise LaunchStatus(503, "APP_UNAVAILABLE") from None


router = APIRouter(prefix="/api/platform/v1", tags=["application-launch"])


def _service(request: Request) -> LaunchService | None:
    return getattr(request.app.state, "launch_service", None)


@router.get("/apps/{app_id}/installation", response_model=AppInstallation, response_model_exclude_none=True)
def installation(app_id: str, request: Request):
    if not _loopback_host(request):
        return contract_error(403, "VALIDATION_FAILED", "Local request required")
    if not ID.fullmatch(app_id) or request.query_params:
        return contract_error(400, "VALIDATION_FAILED", "Invalid app identity")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Launch service is unavailable")
    snapshot = service.supervisor.registry._snapshot
    record = snapshot.installations.get(app_id)
    if record is None:
        return contract_error(404, "NOT_FOUND", "Application is not registered")
    verifying = app_id in snapshot.verifying
    ready = record.installed and service.recent is not None and not verifying
    return AppInstallation(schema_version="1.0", app_id=app_id, installed=record.installed,
                           launch_ready=ready,
                           state=("verifying" if verifying else "not_registered" if not record.installed else
                                  "registered" if ready else "storage_unavailable"),
                           verification=service.supervisor.registry.verification(app_id))


@router.get("/apps/{app_id}/launches/current", response_model=LaunchReceipt)
def current_launch(app_id: str, request: Request):
    if not _loopback_host(request):
        return contract_error(403, "VALIDATION_FAILED", "Local request required")
    if not ID.fullmatch(app_id) or request.query_params:
        return contract_error(400, "VALIDATION_FAILED", "Invalid launch identity")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Launch service is unavailable")
    try:
        return service.current(app_id)
    except LaunchStatus as error:
        return contract_error(error.status, error.code, "Application launch is unavailable")


@router.get("/apps/{app_id}/launches/{launch_id}", response_model=LaunchReceipt)
def get_launch(app_id: str, launch_id: str, request: Request):
    if not _loopback_host(request):
        return contract_error(403, "VALIDATION_FAILED", "Local request required")
    if not ID.fullmatch(app_id) or not ID.fullmatch(launch_id) or launch_id == "current" or request.query_params:
        return contract_error(400, "VALIDATION_FAILED", "Invalid launch identity")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Launch service is unavailable")
    try:
        return service.get(app_id, launch_id)
    except LaunchStatus as error:
        return contract_error(error.status, error.code, "Application launch is unavailable")


@router.post("/apps/{app_id}/launches/{launch_id}", response_model=LaunchReceipt, status_code=201)
async def start_launch(app_id: str, launch_id: str, request: Request):
    if not _same_loopback_origin(request):
        return contract_error(403, "VALIDATION_FAILED", "Local same-origin request required")
    if not ID.fullmatch(app_id) or not ID.fullmatch(launch_id) or launch_id == "current" or request.query_params:
        return contract_error(400, "VALIDATION_FAILED", "Invalid launch identity")
    async for chunk in request.stream():
        if chunk:
            return contract_error(400, "VALIDATION_FAILED", "Launch request has no body")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Launch service is unavailable")
    try:
        receipt, created = await run_in_threadpool(service.start, app_id, launch_id)
        if created:
            return receipt
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=200, content=receipt.model_dump(mode="json"))
    except LaunchStatus as error:
        return contract_error(error.status, error.code, "Application launch is unavailable")


@router.get("/recent-work", response_model=RecentWorkPage)
def recent_work(request: Request, limit: int = Query(..., ge=1, le=200),
                cursor: str | None = None, snapshot_revision: int | None = Query(None, ge=1, le=9_007_199_254_740_991),
                workspace_id: str | None = None):
    if not _loopback_host(request):
        return contract_error(403, "VALIDATION_FAILED", "Local request required")
    permitted = {"limit", "cursor", "snapshot_revision", "workspace_id"}
    if any(key not in permitted or len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        return contract_error(400, "VALIDATION_FAILED", "Invalid recent query")
    if ((cursor is not None and (not CURSOR.fullmatch(cursor) or snapshot_revision is None))
            or (workspace_id is not None and not ID.fullmatch(workspace_id))):
        return contract_error(400, "VALIDATION_FAILED", "Invalid recent query")
    store: RecentWorkStore | None = getattr(request.app.state, "recent_store", None)
    if store is None:
        return contract_error(503, "APP_UNAVAILABLE", "Recent store is not configured")
    if workspace_id is not None:
        workspaces: WorkspaceStore | None = getattr(request.app.state, "workspace_store", None)
        if workspaces is None or workspaces.get_workspace(workspace_id) is None:
            return contract_error(404, "NOT_FOUND", "Workspace not found")
    try:
        return store.list_page(limit=limit, cursor=int(cursor) if cursor is not None else 0,
                               snapshot_revision=snapshot_revision, workspace_id=workspace_id)
    except RecentSnapshotExpired:
        return contract_error(409, "SNAPSHOT_EXPIRED", "Recent work changed; reload the list")
