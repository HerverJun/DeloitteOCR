"""Local, workspace-authorized document comparison endpoints."""
import os
import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request, Query
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from workbench_platform.assets.routes import library, json_body
from workbench_platform.assets.registry import AssetUnavailable
from workbench_platform.data_api.service import DataError
from workbench_platform.agent.providers import ProviderFault
from .service import ComparisonService

router = APIRouter(prefix='/api/platform/v1/workspaces/{workspace_id}/comparisons', tags=['comparisons'])


def configure_comparisons(app):
    root = os.environ.get('WORKBENCH_DATA_DIR')
    app.state.comparison_service = ComparisonService(app, Path(root) / 'comparisons') if root and app.state.asset_library else None
    if getattr(app.state, 'artifact_center', None):
        app.state.artifact_center.comparison_service = app.state.comparison_service


def service(request, workspace_id, write=False):
    library(request, workspace_id, write=write)
    value = getattr(request.app.state, 'comparison_service', None)
    if value is None:
        raise DataError('文档比较服务未配置', 503)
    return value


async def invoke(function, *args, **kwargs):
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except AssetUnavailable:
        raise DataError('资料版本已变化或不可用，请刷新', 409) from None
    except ProviderFault as error:
        raise DataError(str(error), 422) from None


@router.get('/profiles')
def profiles(workspace_id: str, request: Request):
    return service(request, workspace_id).profiles(workspace_id)


@router.post('/profiles', status_code=201)
async def save_profile(workspace_id: str, request: Request):
    value = service(request, workspace_id, True)
    return await invoke(value.save_profile, workspace_id, await json_body(request, 128 * 1024))


@router.post('/profiles/suggest')
async def suggest(workspace_id: str, request: Request):
    value = service(request, workspace_id, True)
    return await invoke(value.suggest, workspace_id, await json_body(request, 32768))


@router.get('/connections')
def connections(workspace_id: str, request: Request):
    return service(request, workspace_id).connections(workspace_id)


@router.post('/connections', status_code=201)
async def save_connection(workspace_id: str, request: Request):
    value = service(request, workspace_id, True)
    return await invoke(value.save_connection, workspace_id, await json_body(request, 32768))


@router.post('/connections/{connection_id}/probe')
async def probe(workspace_id: str, connection_id: str, request: Request):
    value = service(request, workspace_id, True)
    body = await json_body(request)
    return await invoke(value.probe, workspace_id, connection_id, body.get('version'))


@router.get('')
def listing(workspace_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return service(request, workspace_id).list(workspace_id, offset, limit)


@router.post('', status_code=202)
async def create(workspace_id: str, request: Request):
    value = service(request, workspace_id, True)
    return await invoke(value.create, workspace_id, await json_body(request, 256 * 1024))


@router.get('/{comparison_id}')
def detail(workspace_id: str, comparison_id: str, request: Request, revision: int | None = Query(None, ge=1)):
    return service(request, workspace_id).get(workspace_id, comparison_id, revision)


@router.post('/{comparison_id}/control/{action}')
async def control(workspace_id: str, comparison_id: str, action: str, request: Request):
    return await invoke(service(request, workspace_id, True).control, workspace_id, comparison_id, action)


@router.post('/{comparison_id}/rerun-pages', status_code=202)
async def rerun(workspace_id: str, comparison_id: str, request: Request):
    value = service(request, workspace_id, True)
    return await invoke(value.rerun, workspace_id, comparison_id, await json_body(request, 32768))


@router.post('/{comparison_id}/recompare', status_code=202)
async def recompare(workspace_id: str, comparison_id: str, request: Request):
    value = service(request, workspace_id, True)
    return await invoke(value.recompare, workspace_id, comparison_id, await json_body(request, 32768))


@router.get('/{comparison_id}/pages/{side}/{page_number}')
def page(workspace_id: str, comparison_id: str, side: str, page_number: int, request: Request,
         revision: int | None = Query(None, ge=1)):
    return service(request, workspace_id).page(workspace_id, comparison_id, side, page_number, revision)


@router.get('/{comparison_id}/pages/{side}/{page_number}/image')
def page_image(workspace_id: str, comparison_id: str, side: str, page_number: int, request: Request,
               revision: int | None = Query(None, ge=1)):
    path = service(request, workspace_id).image(workspace_id, comparison_id, side, page_number, revision)
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or 'image/png', headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})


@router.post('/{comparison_id}/pages/{side}/{page_number}/review', status_code=202)
async def review(workspace_id: str, comparison_id: str, side: str, page_number: int, request: Request):
    value = service(request, workspace_id, True)
    return await invoke(value.review, workspace_id, comparison_id, side, page_number, await json_body(request, 8 * 1024 * 1024))


@router.get('/{comparison_id}/files/{name}')
def file(workspace_id: str, comparison_id: str, name: str, request: Request,
         revision: int | None = Query(None, ge=1), download: bool = True):
    path = service(request, workspace_id).file(workspace_id, comparison_id, name, revision)
    media = 'text/html' if name.endswith('.html') else 'text/markdown' if name.endswith('.md') else 'application/json'
    return FileResponse(path, media_type=media, headers={'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
        'Cache-Control': 'no-store', 'Content-Disposition': ('attachment' if download else 'inline') + "; filename*=UTF-8''" + quote(name)})
