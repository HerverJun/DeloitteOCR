import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request
from workbench_platform.assets.routes import json_body
from workbench_platform.workspaces.routes import _loopback_host, _same_loopback_origin
from .service import BatchService

router = APIRouter(prefix='/api/platform/v1', tags=['processing-batches'])


def configure_batches(app):
    configured = os.environ.get('WORKBENCH_DATA_DIR')
    app.state.batch_service = BatchService(app, Path(configured) / 'batches') if configured else None


def service(request, write=False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, '请从本机平台页面操作')
    value = getattr(request.app.state, 'batch_service', None)
    if value is None:
        raise HTTPException(503, '批次存储尚未配置')
    return value


@router.get('/batch-templates')
def templates(request: Request, workspace_id: str):
    value = service(request)
    value.workspace(workspace_id)
    items = value.store.templates(workspace_id)
    return {'items': items, 'total': len(items)}


@router.post('/batch-templates', status_code=201)
async def create_template(request: Request):
    return service(request, True).create_template(await json_body(request, 65536))


@router.post('/batch-templates/from-data-session', status_code=201)
async def template_from_data(request: Request):
    return service(request, True).template_from_session(await json_body(request, 4096))


@router.get('/batch-templates/options')
@router.get('/batches/native-options')
def native_options(request: Request, workspace_id: str, scenario: str, link_id: str):
    value = service(request)
    value.workspace(workspace_id)
    try:
        return value.native.options(workspace_id, scenario, link_id)
    except PermissionError:
        raise HTTPException(403, '当前项目关联仅允许查看') from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get('/batches')
def batches(request: Request, workspace_id: str, request_key: str | None = None):
    value = service(request)
    value.workspace(workspace_id)
    items = [value.get(batch['batch_id']) for batch in value.store.list(workspace_id, request_key)]
    return {'items': items, 'total': len(items)}


@router.post('/batches/preflight')
async def preflight(request: Request):
    value = service(request, True)
    payload = await json_body(request, 65536)
    # Blocking preflight stays off the ASGI event loop, like ordinary sync routes.
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(value.preflight, payload)


@router.post('/batches', status_code=202)
async def submit(request: Request):
    return service(request, True).submit(await json_body(request, 65536))


@router.get('/batches/{batch_id}')
def get_batch(batch_id: str, request: Request):
    return service(request).get(batch_id)


@router.get('/batches/{batch_id}/issues')
def issues(batch_id: str, request: Request, disposition: str | None = None,
           offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=200)):
    batch = service(request).get(batch_id)
    items = [issue for issue in batch['issues'] if not disposition or issue['disposition'] == disposition]
    return {'items': items[offset:offset+limit], 'total': len(items), 'offset': offset,
            'summary': batch['summary']}


@router.post('/batches/{batch_id}/control')
async def control(batch_id: str, request: Request):
    return service(request, True).control(batch_id, await json_body(request, 4096))


@router.post('/batches/{batch_id}/dispositions')
async def dispositions(batch_id: str, request: Request):
    value = service(request, True)
    payload = await json_body(request, 65536)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(value.dispositions, batch_id, payload)
