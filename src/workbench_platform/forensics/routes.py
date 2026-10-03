import os
from pathlib import Path
from urllib.parse import quote
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from packages.contracts.forensics import ForensicBundle, Inspection, InspectionPage
from workbench_platform.assets.routes import json_body, library
from .service import ForensicService
from .artifacts import publish
from workbench_platform.artifacts.store import ArtifactUnavailable
from workbench_platform.data_api.service import DataError

router = APIRouter(prefix='/api/platform/v1/workspaces/{workspace_id}/forensics', tags=['image-forensics'])


def configure_forensics(app):
    configured = os.environ.get('WORKBENCH_DATA_DIR')
    app.state.forensic_service = None
    app.state.forensic_error = None
    if configured:
        try:
            app.state.forensic_service = ForensicService(app, Path(configured) / 'forensics')
        except (OSError, ValueError, KeyError) as error:
            app.state.forensic_error = f'图像取证配置或存储不可用（{type(error).__name__}），请修复后重启工具服务'


def service(request, workspace_id, write=False):
    library(request, workspace_id, write=write)
    value = getattr(request.app.state, 'forensic_service', None)
    if value is None:
        raise DataError(getattr(request.app.state, 'forensic_error', None) or '图像取证尚未配置', 503)
    return value


@router.get('', response_model=InspectionPage)
def listing(workspace_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)):
    return service(request, workspace_id).list(workspace_id, offset, limit)


@router.post('', response_model=Inspection, status_code=202)
async def create(workspace_id: str, request: Request):
    value, payload = service(request, workspace_id, True), await json_body(request, 16384)
    return await run_in_threadpool(value.create, workspace_id, payload)


@router.get('/{inspection_id}', response_model=Inspection)
def detail(workspace_id: str, inspection_id: str, request: Request):
    return service(request, workspace_id).get(workspace_id, inspection_id)


@router.post('/{inspection_id}/review', response_model=Inspection)
async def review(workspace_id: str, inspection_id: str, request: Request):
    value, payload = service(request, workspace_id, True), await json_body(request, 16384)
    return await run_in_threadpool(value.review, workspace_id, inspection_id, payload)


@router.post('/{inspection_id}/resume', response_model=Inspection)
def resume(workspace_id: str, inspection_id: str, request: Request):
    return service(request, workspace_id, True).resume(workspace_id, inspection_id)


@router.post('/{inspection_id}/collect', response_model=ForensicBundle)
def collect(workspace_id: str, inspection_id: str, request: Request):
    try:
        return publish(service(request, workspace_id, True), workspace_id, inspection_id)
    except ArtifactUnavailable as error:
        raise DataError('取证成果尚未就绪或正在归集，请保留此任务稍后查询', 409) from error


@router.get('/{inspection_id}/files/{name}')
def file(workspace_id: str, inspection_id: str, name: str, request: Request, download: bool = False):
    content, media = service(request, workspace_id).file(workspace_id, inspection_id, name)
    return Response(content, media_type=media, headers={'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': 'sandbox', 'Content-Disposition':
        ('attachment' if download or media != 'image/png' else 'inline') + "; filename*=UTF-8''" + quote(name)})


@router.get('/{inspection_id}/download')
def download(workspace_id: str, inspection_id: str, request: Request):
    content = service(request, workspace_id).archive(workspace_id, inspection_id)
    return Response(content, media_type='application/zip', headers={
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(f'forensics-{inspection_id}.zip')})
