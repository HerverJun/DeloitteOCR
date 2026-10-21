"""Workspace scoped document parsing, saved reads and literal review exports."""
import os
from pathlib import Path
from urllib.parse import quote
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from workbench_platform.assets.routes import library, json_body
from workbench_platform.data_api.service import DataError
from .service import DocumentService

router = APIRouter(prefix='/api/platform/v1/workspaces/{workspace_id}/documents', tags=['documents'])


def configure_documents(app):
    configured = os.environ.get('WORKBENCH_DATA_DIR')
    app.state.document_service = DocumentService(app, Path(configured) / 'documents') if configured and app.state.asset_library else None


def service(request, workspace_id, write=False):
    library(request, workspace_id, write=write)
    value = getattr(request.app.state, 'document_service', None)
    if value is None:
        raise DataError('文档解析未配置', 503)
    return value


@router.get('')
def listing(workspace_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)):
    return service(request, workspace_id).list(workspace_id, offset, limit)


@router.post('/parse', status_code=202)
async def parse(workspace_id: str, request: Request):
    value, body = service(request, workspace_id, True), await json_body(request, 16384)
    return await run_in_threadpool(value.create, workspace_id, body)


@router.get('/{document_id}')
def detail(workspace_id: str, document_id: str, request: Request, revision: int | None = Query(None, ge=1)):
    return service(request, workspace_id).get(workspace_id, document_id, revision)


@router.get('/{document_id}/pages/{page_number}')
def page(workspace_id: str, document_id: str, page_number: int, request: Request,
         offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200), revision: int | None = Query(None, ge=1)):
    return service(request, workspace_id).page(workspace_id, document_id, page_number, offset, limit, revision)


@router.get('/{document_id}/search')
def search(workspace_id: str, document_id: str, request: Request, q: str = Query(min_length=1, max_length=200),
           offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), revision: int | None = Query(None, ge=1)):
    return service(request, workspace_id).search(workspace_id, document_id, q, offset, limit, revision)


@router.post('/{document_id}/control/{action}')
def control(workspace_id: str, document_id: str, action: str, request: Request):
    return service(request, workspace_id, True).control(workspace_id, document_id, action)


def response(content, media, name, download=True):
    return Response(content, media_type=media, headers={'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': 'sandbox', 'Cache-Control': 'no-store',
        'Content-Disposition': ('attachment' if download else 'inline') + "; filename*=UTF-8''" + quote(name)})


@router.get('/{document_id}/files/{name}')
def file(workspace_id: str, document_id: str, name: str, request: Request, download: bool = True, revision: int | None = Query(None, ge=1)):
    content, media = service(request, workspace_id).file(workspace_id, document_id, name, revision)
    return response(content, media, name, download)


@router.get('/{document_id}/original')
def original(workspace_id: str, document_id: str, request: Request, revision: int | None = Query(None, ge=1)):
    value = service(request, workspace_id)
    run = value.get(workspace_id, document_id, revision)
    with value.registry.stage_verified(workspace_id, run['asset_id'], revision=run['asset_revision'], blob_hash=run['blob_hash']) as path:
        return response(path.read_bytes(), run['source']['media_type'], run['source']['original_name'], False)


@router.get('/{document_id}/tables/{table_id}')
def table(workspace_id: str, document_id: str, table_id: str, request: Request, revision: int | None = Query(None, ge=1)):
    return service(request, workspace_id).table(workspace_id, document_id, table_id, revision)


@router.post('/{document_id}/tables/{table_id}/review')
async def review(workspace_id: str, document_id: str, table_id: str, request: Request):
    value, body = service(request, workspace_id, True), await json_body(request, 8 * 1024 * 1024)
    return await run_in_threadpool(value.review, workspace_id, document_id, table_id, body)


@router.get('/{document_id}/tables/{table_id}/export.xlsx')
def xlsx(workspace_id: str, document_id: str, table_id: str, request: Request, revision: int | None = Query(None, ge=1)):
    return response(service(request, workspace_id).xlsx(workspace_id, document_id, table_id, revision),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', f'{table_id}.xlsx')
