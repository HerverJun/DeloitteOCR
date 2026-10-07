"""Local, same-origin APIs; browser input never selects runtime or host paths."""
import hashlib
import json
import os
import re
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from workbench_platform.workspaces.routes import _loopback_host, _same_loopback_origin
from workbench_platform.assets.registry import AssetUnavailable
from workbench_platform.assets.routes import library
from workbench_platform.assets.sets import AssetSetConflict, AssetSetMissing
from .service import DataWorkbenchService, DataError
from workbench_platform.application_services.data import stage_set_member


router = APIRouter(prefix='/api/platform/v1/data', tags=['data-workbench'])


def configure_data(app):
    location = os.environ.get('WORKBENCH_DATA_DIR')
    python = os.environ.get('WORKBENCH_DATA_PYTHON')
    app.state.data_workbench = (DataWorkbenchService(Path(location) / 'data-workbench', Path(python) if python else None)
                                if location else None)


def service(request, write=False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, '请从本机平台页面操作')
    value = getattr(request.app.state, 'data_workbench', None)
    if value is None:
        raise HTTPException(503, '数据工作台存储尚未配置')
    return value


async def body(request):
    if request.headers.get('content-type', '').split(';')[0].lower() != 'application/json':
        raise HTTPException(400, '请提交 JSON 参数')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 65536:
            raise HTTPException(413, '参数过大')
        raw.extend(chunk)
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        raise HTTPException(400, '参数无效') from None


@router.get('/health')
def health(request: Request):
    if not _loopback_host(request):
        raise HTTPException(403, '请从本机平台页面操作')
    value = getattr(request.app.state, 'data_workbench', None)
    return {'ready': bool(value and value.ready()), 'formats': ['csv', 'xlsx', 'parquet'],
            'message': None if value and value.ready() else '数据分析环境尚未配置，请使用数据工作台启动入口。'}


@router.get('/sessions')
def sessions(request: Request, workspace_id: str | None = None,
             offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    if workspace_id is not None:
        library(request, workspace_id)
    return service(request).list_page(workspace_id, offset, limit)


@router.post('/sessions', status_code=201)
async def create(request: Request):
    value = service(request, True)
    payload = await body(request)
    workspace_id = payload.get('workspace_id')
    if workspace_id is not None:
        if not isinstance(workspace_id, str):
            raise HTTPException(400, '项目选择无效')
        library(request, workspace_id, write=True)
    request_id = payload.get('request_id')
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', request_id)):
        raise HTTPException(400, '创建请求无效')
    return value.create(payload.get('name', '未命名比对'), request_key=request_id, workspace_id=workspace_id)


@router.get('/outputs')
def outputs(request: Request, workspace_id: str,
            offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    library(request, workspace_id)
    return service(request).output_page(workspace_id, offset, limit)


@router.get('/sessions/{session_id}')
def get(session_id: str, request: Request):
    return service(request).get(session_id)


@router.post('/sessions/{session_id}/uploads', status_code=201)
async def upload(session_id: str, request: Request, name: str = Query(..., min_length=1, max_length=240)):
    value = service(request, True)
    value.get(session_id)
    if any(char in name for char in ('/', '\\', '\0', ':')):
        raise HTTPException(400, '文件名无效')
    suffix = Path(name).suffix.lower()
    if suffix not in ('.csv', '.xlsx', '.parquet'):
        raise HTTPException(400, '请选择 CSV、XLSX 或 Parquet 文件')
    key = 'u' + uuid.uuid4().hex
    path = value.session_dir(session_id) / 'uploads' / (key + suffix)
    count, digest = 0, hashlib.sha256()
    try:
        with path.open('xb') as stream:
            async for chunk in request.stream():
                count += len(chunk)
                if count > 64 * 1024 * 1024:
                    raise HTTPException(413, '文件超过 64 MB，请先拆分')
                stream.write(chunk)
                digest.update(chunk)
        if not count:
            raise HTTPException(400, '文件为空')
        return value.upload(session_id, key, name, suffix, path, digest.hexdigest(), count)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@router.post('/sessions/{session_id}/asset-uploads', status_code=201)
async def upload_from_asset_set(session_id: str, request: Request):
    value = service(request, True)
    value.get(session_id)
    payload = await body(request)
    workspace_id = payload.get('workspace_id')
    set_id = payload.get('asset_set_id')
    revision = payload.get('asset_set_revision')
    item_id = payload.get('item_id')
    request_id = payload.get('request_id') or ('r' + uuid.uuid4().hex)
    if (not all(isinstance(item, str) for item in (workspace_id, set_id, item_id, request_id))
            or type(revision) is not int or revision < 1
            or set(payload) - {'workspace_id', 'asset_set_id', 'asset_set_revision', 'item_id', 'request_id'}):
        raise HTTPException(400, '资料集选择无效')
    registry, sets, _ = library(request, workspace_id, write=True)
    try:
        return stage_set_member(value, registry, sets, workspace_id, session_id,
                                set_id, revision, item_id, request_id)
    except (AssetUnavailable, AssetSetConflict, AssetSetMissing):
        raise HTTPException(409, '资料集版本已变化或原件不可用') from None



@router.post('/sessions/{session_id}/actions', status_code=202)
async def action(session_id: str, request: Request):
    value = service(request, True)
    payload = await body(request)
    return value.submit(session_id, payload.get('action'), payload.get('payload', {}), payload.get('version'))


@router.get('/sessions/{session_id}/jobs/{job_id}')
def job(session_id: str, job_id: str, request: Request):
    return service(request).job(session_id, job_id)


@router.post('/sessions/{session_id}/jobs/{job_id}/cancel')
def cancel(session_id: str, job_id: str, request: Request):
    return service(request, True).cancel(session_id, job_id)


@router.get('/sessions/{session_id}/tables/{revision}')
def table(session_id: str, revision: str, request: Request,
          offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return service(request).page(session_id, 'table', revision, offset, limit)


@router.get('/sessions/{session_id}/results/{result_id}')
def result(session_id: str, result_id: str, request: Request, category: str | None = None,
           offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return service(request).page(session_id, 'result', result_id, offset, limit, category)


@router.get('/sessions/{session_id}/exports/{export_id}/{name}')
def download(session_id: str, export_id: str, name: str, request: Request):
    path = service(request).download(session_id, export_id, name)
    return FileResponse(path, filename=name, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
