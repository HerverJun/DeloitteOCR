import os
from pathlib import Path
from fastapi import APIRouter, Request, Query
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from workbench_platform.assets.routes import library,json_body
from workbench_platform.data_api.service import DataError
from workbench_platform.workspaces.routes import _loopback_host
from .service import MultimodalService

router=APIRouter(prefix='/api/platform/v1',tags=['manual-multimodal'])

def configure_multimodal(app):
    app.state.multimodal_service=None
    app.state.multimodal_error=None
    if os.environ.get('WORKBENCH_DATA_DIR'):
        try:
            app.state.multimodal_service=MultimodalService(app,Path(os.environ['WORKBENCH_DATA_DIR'])/'manual-analysis')
            if getattr(app.state,'job_service',None):app.state.job_service.manual_service=app.state.multimodal_service
        except (OSError,ValueError,KeyError) as error:app.state.multimodal_error=f'多模态服务配置不可用：{error}'

def service(request,workspace=None,write=False):
    if workspace:library(request,workspace,write=write)
    elif not _loopback_host(request):raise DataError('仅接受本机请求',403)
    value=getattr(request.app.state,'multimodal_service',None)
    if value is None:raise DataError(getattr(request.app.state,'multimodal_error',None) or '多模态服务尚未配置',503)
    return value

@router.get('/multimodal/capabilities')
def capabilities(request:Request):return service(request).capabilities()

BASE='/workspaces/{workspace_id}/multimodal'

@router.get(BASE+'/jobs')
def listing(workspace_id:str,request:Request,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),kind:str|None=Query(None,pattern='^(face|audio|seal|catnet)$')):
    return service(request,workspace_id).list(workspace_id,offset,limit,kind)

@router.post(BASE+'/jobs',status_code=202)
async def create(workspace_id:str,request:Request):
    value=service(request,workspace_id,True);payload=await json_body(request,32768)
    return await run_in_threadpool(value.create,workspace_id,payload)

@router.get(BASE+'/jobs/{job_id}')
def detail(workspace_id:str,job_id:str,request:Request):return service(request,workspace_id).get(workspace_id,job_id)

@router.post(BASE+'/jobs/{job_id}/review')
async def review(workspace_id:str,job_id:str,request:Request):
    value=service(request,workspace_id,True);payload=await json_body(request,16*1024*1024)
    return await run_in_threadpool(value.review,workspace_id,job_id,payload)

@router.post(BASE+'/jobs/{job_id}/control/{action}')
async def control(workspace_id:str,job_id:str,action:str,request:Request):
    value=service(request,workspace_id,True)
    payload=await json_body(request,8192) if await request.body() else None
    return await run_in_threadpool(value.control,workspace_id,job_id,action,payload)

@router.get(BASE+'/operations/{operation_type}/{request_id}')
def operation(workspace_id:str,operation_type:str,request_id:str,request:Request):
    return service(request,workspace_id).lookup_operation(workspace_id,operation_type,request_id)

@router.get(BASE+'/jobs/{job_id}/snapshot')
def current_snapshot(workspace_id:str,job_id:str,request:Request):
    return service(request,workspace_id).snapshot(workspace_id,job_id)

@router.get(BASE+'/jobs/{job_id}/snapshots/{snapshot_id}')
def frozen_snapshot(workspace_id:str,job_id:str,snapshot_id:str,request:Request):
    return service(request,workspace_id).snapshot(workspace_id,job_id,snapshot_id)

@router.get(BASE+'/jobs/{job_id}/snapshots/{snapshot_id}/files/{name}')
def frozen_file(workspace_id:str,job_id:str,snapshot_id:str,name:str,request:Request):
    path,media=service(request,workspace_id).snapshot_output(workspace_id,job_id,snapshot_id,name)
    return FileResponse(path,media_type=media,headers={'X-Content-Type-Options':'nosniff','Content-Security-Policy':'sandbox'})

@router.get(BASE+'/jobs/{job_id}/snapshots/{snapshot_id}/download')
def frozen_download(workspace_id:str,job_id:str,snapshot_id:str,request:Request):
    path,_=service(request,workspace_id).snapshot_export(workspace_id,job_id,snapshot_id)
    return FileResponse(path,media_type='application/zip',filename=f'{job_id}-{snapshot_id}.zip',headers={'Cache-Control':'no-store'})

@router.get(BASE+'/jobs/{job_id}/files/{name}')
def file(workspace_id:str,job_id:str,name:str,request:Request):
    path,media=service(request,workspace_id).output(workspace_id,job_id,name)
    return FileResponse(path,media_type=media,headers={'X-Content-Type-Options':'nosniff','Content-Security-Policy':'sandbox'})

@router.get(BASE+'/jobs/{job_id}/download')
def download(workspace_id:str,job_id:str,request:Request):
    path,_=service(request,workspace_id).export(workspace_id,job_id)
    return FileResponse(path,media_type='application/zip',filename=f'{job_id}.zip',headers={'Cache-Control':'no-store'})

@router.post(BASE+'/jobs/{job_id}/collect')
async def collect(workspace_id:str,job_id:str,request:Request):
    value=service(request,workspace_id,True)
    return await run_in_threadpool(value.collect,workspace_id,job_id)

@router.get(BASE+'/face-references')
def references(workspace_id:str,request:Request):return service(request,workspace_id).references(workspace_id)

@router.post(BASE+'/jobs/{job_id}/to-data')
async def to_data(workspace_id:str,job_id:str,request:Request):
    value=service(request,workspace_id,True)
    return await run_in_threadpool(value.to_data,workspace_id,job_id)

@router.post(BASE+'/jobs/{job_id}/draft-to-data')
async def draft_to_data(workspace_id:str,job_id:str,request:Request):
    value=service(request,workspace_id,True)
    payload=await json_body(request,8192) if await request.body() else None
    return await run_in_threadpool(value.draft_to_data,workspace_id,job_id,payload)

@router.post(BASE+'/face-references',status_code=201)
async def add_reference(workspace_id:str,request:Request):
    value=service(request,workspace_id,True);payload=await json_body(request,8192)
    return value.add_reference(workspace_id,payload)

@router.delete(BASE+'/face-references/{reference_id}')
async def delete_reference(workspace_id:str,reference_id:str,request:Request):
    value=service(request,workspace_id,True)
    payload=await json_body(request,8192) if await request.body() else None
    return await run_in_threadpool(value.delete_reference,workspace_id,reference_id,payload)
