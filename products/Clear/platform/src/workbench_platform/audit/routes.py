import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from workbench_platform.assets.routes import json_body
from workbench_platform.workspaces.routes import _loopback_host, _same_loopback_origin
from .evidence import batch_item_evidence
from .service import AuditService

router = APIRouter(prefix='/api/platform/v1', tags=['audit-workflow'])


def configure_audit(app):
    configured = os.environ.get('WORKBENCH_DATA_DIR')
    app.state.audit_service = AuditService(app, Path(configured) / 'audit') if configured else None


def service(request, write=False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, '请从本机平台页面操作')
    value = getattr(request.app.state, 'audit_service', None)
    if value is None:
        raise HTTPException(503, '审计项目存储尚未配置')
    return value


@router.get('/workspaces/{workspace_id}/audit-projects')
def projects(workspace_id: str, request: Request, limit: int = Query(50, ge=1, le=200),
             cursor: str | None = None, snapshot_token: str | None = None):
    return service(request).projects(workspace_id, limit, cursor, snapshot_token)


@router.post('/workspaces/{workspace_id}/audit-projects', status_code=201)
async def create_project(workspace_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 262144)
    return await run_in_threadpool(value.create_project, workspace_id, payload)


@router.get('/audit-projects/{project_id}')
def project(project_id: str, request: Request):
    return service(request).get(project_id)


@router.post('/audit-projects/{project_id}')
async def write_project(project_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 262144)
    return await run_in_threadpool(value.write, project_id, 'project', payload)


@router.get('/audit-projects/{project_id}/procedures')
def procedures(project_id: str, request: Request, limit: int = Query(50, ge=1, le=200),
               cursor: str | None = None, snapshot_token: str | None = None):
    return service(request).entities(project_id, 'procedure', limit, cursor, snapshot_token)


@router.post('/audit-projects/{project_id}/procedures', status_code=201)
async def create_procedure(project_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 262144)
    return await run_in_threadpool(value.write, project_id, 'procedure', payload)


@router.get('/audit-projects/{project_id}/procedures/{procedure_id}')
def procedure(project_id: str, procedure_id: str, request: Request):
    return service(request).get(project_id, 'procedure', procedure_id)


@router.post('/audit-projects/{project_id}/procedures/{procedure_id}')
async def write_procedure(project_id: str, procedure_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 262144)
    return await run_in_threadpool(value.write, project_id, 'procedure', payload, procedure_id)


@router.get('/audit-projects/{project_id}/findings')
def findings(project_id: str, request: Request, limit: int = Query(50, ge=1, le=200),
             cursor: str | None = None, snapshot_token: str | None = None, procedure_id: str | None = None):
    return service(request).entities(project_id, 'finding', limit, cursor, snapshot_token, procedure_id)


@router.post('/audit-projects/{project_id}/findings', status_code=201)
async def create_finding(project_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 262144)
    return await run_in_threadpool(value.write, project_id, 'finding', payload)


@router.get('/audit-projects/{project_id}/findings/{finding_id}')
def finding(project_id: str, finding_id: str, request: Request):
    return service(request).get(project_id, 'finding', finding_id)


@router.post('/audit-projects/{project_id}/findings/{finding_id}')
async def write_finding(project_id: str, finding_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 262144)
    return await run_in_threadpool(value.write, project_id, 'finding', payload, finding_id)


@router.post('/audit-projects/{project_id}/records', status_code=201)
async def record(project_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 65536)
    return await run_in_threadpool(value.write, project_id, 'record', payload)


@router.get('/audit-projects/{project_id}/history')
def history(project_id: str, request: Request, limit: int = Query(50, ge=1, le=200),
            cursor: str | None = None, snapshot_token: str | None = None,
            entity_kind: Literal['project', 'procedure', 'finding'] | None = None, entity_id: str | None = None):
    return service(request).history(project_id, limit, cursor, snapshot_token, entity_kind, entity_id)


@router.get('/audit-projects/{project_id}/requests/{request_id}')
def request_status(project_id: str, request_id: str, request: Request):
    return service(request).request_status(project_id, request_id)


@router.get('/batches/{batch_id}/items/{item_id}/evidence')
def evidence(batch_id: str, item_id: str, request: Request, batch_revision: int = Query(..., ge=1)):
    return batch_item_evidence(service(request).batches, batch_id, item_id, batch_revision)


@router.get('/audit-projects/{project_id}/evidence/{evidence_id}')
def evidence_snapshot(project_id: str, evidence_id: str, request: Request):
    return service(request).evidence(project_id, evidence_id)


@router.post('/audit-projects/{project_id}/workpapers', status_code=201)
async def publish_workpaper(project_id: str, request: Request):
    value, payload = service(request, True), await json_body(request, 65536)
    return await run_in_threadpool(value.publish, project_id, payload)


@router.get('/audit-projects/{project_id}/workpapers/{workpaper_id}')
def workpaper(project_id: str, workpaper_id: str, request: Request):
    return service(request).workpaper(project_id, workpaper_id)


from .todos import router as todo_router
router.include_router(todo_router)
