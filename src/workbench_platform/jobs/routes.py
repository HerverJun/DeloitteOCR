"""Workspace-scoped HTTP entry for native-backed task projections."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from workbench_platform.bridge.context import ResolvedContext
from workbench_platform.catalog.launch import LaunchService, LaunchStatus
from workbench_platform.catalog.routes import contract_error
from workbench_platform.jobs.projection import JobProjectionStore, ProjectionConflict, ProjectionUnavailable
from workbench_platform.jobs.native_classification import ClassificationAdapter, ClassificationUnavailable
from workbench_platform.jobs.native_ocr import OcrAdapter, OcrUnavailable
from workbench_platform.store import WorkspaceStore
from workbench_platform.workspaces.routes import _loopback_host, _same_loopback_origin

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
ACTION = frozenset({"cancel", "pause", "resume", "retry_failed"})
router = APIRouter(prefix="/api/platform/v1/workspaces/{workspace_id}/jobs", tags=["jobs"])


class JobService:
    def __init__(self, ledger: JobProjectionStore, workspaces: WorkspaceStore,
                 launches: LaunchService, *, ocr_token_file: str | None):
        self.ledger, self.workspaces, self.launches = ledger, workspaces, launches
        self.ocr_token_file = Path(ocr_token_file).resolve() if ocr_token_file else None

    def _context(self, workspace_id: str, app_id: str, link_id: str,
                 instance_id: str | None = None) -> ResolvedContext:
        if app_id not in {"ocr", "classification"} or self.workspaces.get_workspace(workspace_id) is None:
            raise ProjectionUnavailable("Workspace or app is unavailable")
        links = [link for link in self.workspaces.list_links(workspace_id)
                 if link.app_id == app_id and link.link_id == link_id]
        if len(links) != 1:
            raise ProjectionUnavailable("Linked project is unavailable")
        receipt = self.launches.current(app_id)
        if instance_id is not None and receipt.instance_id != instance_id:
            raise ProjectionUnavailable("Native generation is no longer live")
        link = links[0]
        return ResolvedContext(workspace_id, app_id, receipt.instance_id, link_id,
                               link.module_project_id, link.role, "/jobs", receipt.base_url)

    def _live(self, context: ResolvedContext) -> bool:
        try:
            receipt = self.launches.current(context.app_id)
            return receipt.instance_id == context.instance_id and receipt.base_url == context.base_url
        except LaunchStatus:
            return False

    def _token(self) -> str:
        if self.ocr_token_file is None or not self.ocr_token_file.is_file():
            raise OcrUnavailable()
        # The path is fixed by the platform administrator at startup. Never
        # accept a credential or token path from the browser.
        try:
            return self.ocr_token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise OcrUnavailable() from None

    def _adapter(self, context: ResolvedContext):
        if context.app_id == "classification":
            return ClassificationAdapter(context, live=self._live)
        return OcrAdapter(context, live=self._live, token=self._token)

    @staticmethod
    def _items(adapter: ClassificationAdapter, job_id: str, total: int):
        if total > 5000:
            raise ClassificationUnavailable()
        rows = []
        for offset in range(0, total, 200):
            page = adapter.results(job_id, limit=min(200, total - offset), offset=offset)
            if not page:
                raise ClassificationUnavailable()
            rows.extend(page)
        return tuple(rows)

    def _snapshot(self, context: ResolvedContext, native_ids: list[str]):
        adapter = self._adapter(context)
        if context.app_id == "classification":
            job = adapter.job(native_ids[0])
            items = self._items(adapter, native_ids[0], job.total) if job.kind == "inference" else None
            return job, items
        return adapter.batch(native_ids), None

    def register(self, workspace_id: str, link_id: str, app_id: str,
                 native_ids: list[str]) -> dict:
        context = self._context(workspace_id, app_id, link_id)
        if context.role == "viewer":
            raise PermissionError()
        native, items = self._snapshot(context, native_ids)
        ref = self._record(context, native_ids)
        if ref["last_event_seq"] == 0:
            return self.ledger.observe(context, ref["platform_job_id"], generation=1,
                                       expected_event_seq=0, native=native, items=items)
        return self.refresh(workspace_id, ref)

    def _record(self, context: ResolvedContext, native_ids: list[str]) -> dict:
        identity = json.dumps([context.workspace_id, context.link_id, context.app_id,
                               context.instance_id, native_ids], separators=(",", ":")).encode()
        digest = hashlib.sha256(identity).hexdigest()
        return self.ledger.register(context, platform_job_id="j" + digest[:32],
                                    operation_id="o" + digest[:32], input_manifest_hash=digest,
                                    native_job_ids=native_ids, generation=1)

    def refresh(self, workspace_id: str, ref: dict) -> dict:
        try:
            context = self._context(workspace_id, ref["app_id"], ref["link_id"], ref["instance_id"])
        except (ProjectionUnavailable, LaunchStatus, ValueError):
            return {**ref, "status": "unknown", "availability": "not_ready", "capabilities": []}
        try:
            native, items = self._snapshot(context, ref["native_job_ids"])
        except (ClassificationUnavailable, OcrUnavailable):
            return self.ledger.observe(context, ref["platform_job_id"],
                                       generation=ref["generation"],
                                       expected_event_seq=ref["last_event_seq"], failure="unreachable")
        return self.ledger.observe(context, ref["platform_job_id"],
                                   generation=ref["generation"],
                                   expected_event_seq=ref["last_event_seq"], native=native, items=items)

    def action(self, workspace_id: str, platform_job_id: str, action: str) -> dict:
        ref = self.ledger.get(workspace_id, platform_job_id)
        context = self._context(workspace_id, ref["app_id"], ref["link_id"], ref["instance_id"])
        if context.role == "viewer":
            raise PermissionError()
        current = self.refresh(workspace_id, ref)
        if action not in current["capabilities"] or current["availability"] != "ready":
            raise ProjectionConflict("Action is unavailable")
        try:
            adapter = self._adapter(context)
            if context.app_id == "classification":
                result = adapter.action(ref["native_job_ids"][0], action)
                if action == "retry_failed":
                    # The acknowledgement contains a new native ID. It may
                    # not be queryable immediately, so persist an unknown
                    # reference without pretending its work has started.
                    new_ref = self._record(context, [result])
                    return {"request_state": "sent", "job": current, "new_job": new_ref}
            else:
                if action != "cancel":
                    raise ProjectionConflict("Action is unavailable")
                affected = adapter.cancel(ref["native_job_ids"])
                if not affected:
                    return {"request_state": "unknown", "job": {**current, "status": "unknown",
                                                                "availability": "not_ready", "capabilities": []}}
            # A native acknowledgement is not worker termination. A later GET
            # must verify the new native state before the UI changes status.
            return {"request_state": "sent", "job": current}
        except (ClassificationUnavailable, OcrUnavailable):
            return {"request_state": "unknown", "job": {**current, "status": "unknown",
                                                        "availability": "unreachable", "capabilities": []}}


def configure_jobs(app) -> None:
    configured = os.environ.get("WORKBENCH_DATA_DIR")
    if configured and app.state.workspace_store is not None:
        app.state.job_service = JobService(JobProjectionStore(Path(configured).resolve() / "jobs.sqlite3"),
                                           app.state.workspace_store, app.state.launch_service,
                                           ocr_token_file=os.environ.get("WORKBENCH_OCR_TOKEN_FILE"))
    else:
        app.state.job_service = None


def _service(request: Request) -> JobService | None:
    return getattr(request.app.state, "job_service", None)


def _error(error: Exception):
    if isinstance(error, PermissionError):
        return contract_error(403, "VALIDATION_FAILED", "Job control is unavailable")
    if isinstance(error, (ProjectionUnavailable, LaunchStatus)):
        return contract_error(404, "NOT_FOUND", "Job or native application is unavailable")
    if isinstance(error, (ClassificationUnavailable, OcrUnavailable)):
        return contract_error(503, "APP_UNAVAILABLE", "Native job is unavailable")
    if isinstance(error, ProjectionConflict):
        return contract_error(409, "REVISION_CONFLICT", "Job state changed; refresh and retry")
    return contract_error(400, "VALIDATION_FAILED", "Invalid job request")


async def _json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise ValueError()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            raise ValueError()
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError()
    return value


@router.get("")
async def list_jobs(workspace_id: str, request: Request, limit: int = 50, offset: int = 0):
    if not _loopback_host(request) or not ID.fullmatch(workspace_id):
        return contract_error(403, "VALIDATION_FAILED", "Local workspace request required")
    if (set(request.query_params) - {"limit", "offset"} or
            any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
        return contract_error(400, "VALIDATION_FAILED", "Invalid job query")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Job service is unavailable")
    try:
        if service.workspaces.get_workspace(workspace_id) is None:
            raise ProjectionUnavailable()
        if not 1 <= limit <= 50 or not 0 <= offset <= 1_000_000:
            raise ValueError()
        refs = service.ledger.list(workspace_id, limit=limit + 1, offset=offset)
        return {"items": await run_in_threadpool(lambda: [service.refresh(workspace_id, ref) for ref in refs[:limit]]),
                "next_offset": offset + limit if len(refs) > limit else None}
    except (ProjectionUnavailable, ProjectionConflict, LaunchStatus, ValueError) as error:
        return _error(error)


@router.get("/links")
def list_job_links(workspace_id: str, request: Request):
    if not _loopback_host(request) or not ID.fullmatch(workspace_id) or request.query_params:
        return contract_error(400, "VALIDATION_FAILED", "Invalid workspace request")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Job service is unavailable")
    try:
        if service.workspaces.get_workspace(workspace_id) is None:
            raise ProjectionUnavailable()
        return {"items": [{"link_id": link.link_id, "app_id": link.app_id,
                           "module_project_id": link.module_project_id, "role": link.role}
                          for link in service.workspaces.list_links(workspace_id)
                          if link.app_id in {"ocr", "classification"}]}
    except (ProjectionUnavailable, ValueError) as error:
        return _error(error)


@router.get("/{platform_job_id}")
async def get_job(workspace_id: str, platform_job_id: str, request: Request):
    if not _loopback_host(request) or not ID.fullmatch(workspace_id):
        return contract_error(403, "VALIDATION_FAILED", "Local workspace request required")
    if not ID.fullmatch(platform_job_id) or request.query_params:
        return contract_error(400, "VALIDATION_FAILED", "Invalid job identity")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Job service is unavailable")
    try:
        ref = service.ledger.get(workspace_id, platform_job_id)
        return await run_in_threadpool(service.refresh, workspace_id, ref)
    except (ProjectionUnavailable, ProjectionConflict, LaunchStatus, ValueError) as error:
        return _error(error)


@router.post("", status_code=201)
async def register_job(workspace_id: str, request: Request):
    if not _same_loopback_origin(request):
        return contract_error(403, "VALIDATION_FAILED", "Local same-origin request required")
    if not ID.fullmatch(workspace_id) or request.query_params:
        return contract_error(400, "VALIDATION_FAILED", "Invalid workspace identity")
    try:
        body = await _json_body(request)
        ids = body.get("native_job_ids")
        if (set(body) != {"link_id", "app_id", "native_job_ids"}
                or not all(isinstance(body.get(key), str) and ID.fullmatch(body[key])
                           for key in ("link_id", "app_id"))
                or not isinstance(ids, list) or not 1 <= len(ids) <= 200
                or any(not isinstance(item, str) or not ID.fullmatch(item) for item in ids)
                or len(set(ids)) != len(ids)
                or body["app_id"] == "classification" and len(ids) != 1):
            raise ValueError()
        service = _service(request)
        if service is None:
            return contract_error(503, "APP_UNAVAILABLE", "Job service is unavailable")
        return await run_in_threadpool(service.register, workspace_id, body["link_id"],
                                       body["app_id"], ids)
    except (ValueError, ProjectionConflict, ProjectionUnavailable, LaunchStatus,
            ClassificationUnavailable, OcrUnavailable, PermissionError) as error:
        return _error(error)


@router.post("/{platform_job_id}/actions/{action}", status_code=202)
async def control_job(workspace_id: str, platform_job_id: str, action: str, request: Request):
    if not _same_loopback_origin(request):
        return contract_error(403, "VALIDATION_FAILED", "Local same-origin request required")
    if (not ID.fullmatch(workspace_id) or not ID.fullmatch(platform_job_id)
            or action not in ACTION or request.query_params):
        return contract_error(400, "VALIDATION_FAILED", "Invalid job action")
    async for chunk in request.stream():
        if chunk:
            return contract_error(400, "VALIDATION_FAILED", "Job action has no body")
    service = _service(request)
    if service is None:
        return contract_error(503, "APP_UNAVAILABLE", "Job service is unavailable")
    try:
        return await run_in_threadpool(service.action, workspace_id, platform_job_id, action)
    except (ValueError, ProjectionConflict, ProjectionUnavailable, LaunchStatus,
            ClassificationUnavailable, OcrUnavailable, PermissionError) as error:
        return _error(error)
