"""Authenticated public portal; native services remain behind a private worker."""
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import secrets
import time
import httpx
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, insert, update, func
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from starlette.concurrency import run_in_threadpool
from packages.runtime import require_path
from packages.contracts.tasks import TaskCreate, TaskSummary, TaskDetail, TaskPage, TaskControl, TaskEdit
from .database import check_schema, f_path
from .store import Ledger, LedgerError, token_hash, wire, key, digest, safe
from . import schema_v1 as s
from .registry import match, command_for,task_label
from .transport import private_worker_url, private_worker_headers, public_endpoint

COOKIE='clear_session'
class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=1,max_length=100)
    password:str=Field(min_length=1,max_length=512)
class UserCreate(Login):
    display_name:str=Field(min_length=1,max_length=200)
    is_admin:bool=False
class Member(BaseModel):
    user_id:str
    role:str

def create_app(engine,worker_client=None):
    ledger=Ledger(engine); attempts=defaultdict(deque)
    endpoint=public_endpoint();public_origin=endpoint.origin;allow_http=endpoint.insecure_http
    worker_origin=private_worker_url();service_headers=private_worker_headers()
    worker_token=os.environ.get('CLEAR_WORKER_TOKEN','')
    if len(worker_token)<32: raise RuntimeError('An explicit worker service token is required')
    @asynccontextmanager
    async def lifespan(app):
        check_schema(engine)
        app.state.worker=worker_client or httpx.AsyncClient(base_url=worker_origin,timeout=httpx.Timeout(180,connect=10),follow_redirects=False)
        scheduler=None
        root=os.environ.get('WORKBENCH_DATA_DIR')
        if root and os.environ.get('CLEAR_DAILY_BACKUP','1')=='1':
            from .scheduler import daily
            scheduler=asyncio.create_task(daily(engine,f_path(root)))
        try:yield
        finally:
            if scheduler:scheduler.cancel();await asyncio.gather(scheduler,return_exceptions=True)
            if worker_client is None: await app.state.worker.aclose()
    app=FastAPI(title='Clear multi-user task portal',version='2.0.0',lifespan=lifespan)
    app.state.ledger=ledger

    @app.exception_handler(LedgerError)
    async def ledger_error(request,error):return JSONResponse({'detail':str(error)},status_code=error.status)
    @app.exception_handler(SQLAlchemyError)
    async def database_error(request,error):return JSONResponse({'detail':'中央数据库暂不可用；请保留请求键后重试'},status_code=503)

    @app.middleware('http')
    async def authenticate(request,call_next):
        if request.headers.get('host','')!=endpoint.authority:
            return JSONResponse({'detail':'门户访问地址与已配置地址不一致'},status_code=400)
        path=request.url.path
        mutates=request.method not in {'GET','HEAD','OPTIONS'} or request.method=='GET' and bool(re.search(r'/(export|download|report)(?:/|$)',path))
        if path.startswith(('/api/','/native/')) and path not in {'/api/platform/v2/auth/login','/api/platform/v2/mode','/api/platform/v2/health'}:
            try:
                actor=await run_in_threadpool(ledger.authenticate,request.cookies.get(COOKIE))
                request.state.actor=actor
                data_root=os.environ.get('WORKBENCH_DATA_DIR')
                if data_root and (Path(data_root)/'central-maintenance.json').is_file() and mutates:
                    raise LedgerError('平台正在一致性备份，暂不接受写入，请稍后重试',503)
                if request.method not in {'GET','HEAD','OPTIONS'}:
                    csrf=request.headers.get('x-clear-csrf','')
                    if request.headers.get('origin')!=public_origin or not csrf or not secrets.compare_digest(token_hash(csrf),actor['csrf_hash']):
                        raise LedgerError('页面身份校验失败，请刷新后重试',403)
            except LedgerError as error:return JSONResponse({'detail':str(error)},status_code=error.status)
            except SQLAlchemyError:return JSONResponse({'detail':'中央数据库暂不可用'},status_code=503)
        if path.startswith(('/api/','/native/')) and mutates:
            from .maintenance import shared_write_guard
            with shared_write_guard(engine):response=await call_next(request)
        else:response=await call_next(request)
        if path.startswith(('/api/','/native/')):response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        if not allow_http:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    def actor(request):return request.state.actor
    def worker_headers(user,workspace=None,step=None):
        headers={'Authorization':'Bearer '+worker_token,**service_headers,
                 'X-Clear-Actor':user['id'],'X-Clear-Display-Name':user['display_name'].encode('utf-8').hex()}
        if workspace:headers['X-Clear-Workspace']=workspace
        if step:headers['X-Clear-Step']=step
        return headers

    @app.get('/api/platform/v2/mode')
    def mode():return {'multiuser':True,'contract_version':'2.0','login_required':True}
    @app.get('/api/platform/v2/health')
    def health():return {'status':'ok','service':'clear-task-portal'}
    @app.post('/api/platform/v2/auth/login')
    async def login(value:Login,request:Request):
        if request.headers.get('origin')!=public_origin:raise LedgerError('登录来源无效',403)
        address=request.client.host if request.client else 'unknown'; bucket=attempts[address]; current=time.monotonic()
        while bucket and bucket[0]<current-60:bucket.popleft()
        if len(bucket)>=10:raise LedgerError('登录尝试过于频繁，请稍后重试',429)
        bucket.append(current)
        user,token,csrf=await run_in_threadpool(ledger.login,value.username,value.password)
        bucket.clear()
        response=JSONResponse({'user':user,'csrf_token':csrf})
        response.set_cookie(COOKIE,token,httponly=True,secure=not allow_http,samesite='strict',max_age=43200,path='/')
        # A readable CSRF cookie survives reload; authentication itself remains HttpOnly.
        response.set_cookie('clear_csrf',csrf,httponly=False,secure=not allow_http,samesite='strict',max_age=43200,path='/')
        return response
    @app.get('/api/platform/v2/auth/me')
    def me(request:Request):return {'user':ledger.public_user(actor(request))}
    @app.post('/api/platform/v2/auth/logout')
    def logout(request:Request):
        ledger.logout(request.cookies.get(COOKIE,'')); response=JSONResponse({'ok':True})
        response.delete_cookie(COOKIE);response.delete_cookie('clear_csrf');return response

    @app.get('/api/platform/v2/users')
    def users(request:Request):
        if not actor(request)['is_admin']:raise LedgerError('需要平台管理员权限',403)
        with engine.connect() as db:return {'items':[ledger.public_user(r) for r in db.execute(select(s.users).order_by(s.users.c.username)).mappings()]}
    @app.get('/api/platform/v2/ai-services')
    def ai_services(request:Request):
        with engine.connect() as db:
            return {'items':[dict(r) for r in db.execute(select(s.ai_service_profiles.c.id,s.ai_service_profiles.c.name,s.ai_service_profiles.c.provider,s.ai_service_profiles.c.model,s.ai_service_profiles.c.enabled).order_by(s.ai_service_profiles.c.name)).mappings()]}
    @app.get('/api/platform/v2/visible-users')
    def visible_users(request:Request):
        user=actor(request);query=select(s.users.c.id,s.users.c.display_name,s.users.c.username)
        if not user['is_admin']:
            projects=select(s.workspace_members.c.workspace_id).where(s.workspace_members.c.user_id==user['id'])
            query=query.where(s.users.c.id.in_(select(s.tasks.c.created_by).where(s.tasks.c.workspace_id.in_(projects)).union(select(s.workspace_members.c.user_id).where(s.workspace_members.c.workspace_id.in_(projects)))))
        with engine.connect() as db:return {'items':[dict(row) for row in db.execute(query.order_by(s.users.c.display_name)).mappings()]}
    @app.post('/api/platform/v2/users',status_code=201)
    def create_user(value:UserCreate,request:Request):return ledger.create_user(value.username,value.display_name,value.password,admin=value.is_admin,actor=actor(request))
    @app.patch('/api/platform/v2/users/{user_id}')
    async def disable_user(user_id:str,request:Request):
        user=actor(request)
        if not user['is_admin']:raise LedgerError('需要平台管理员权限',403)
        value=await request.json()
        if set(value)!={'disabled'} or type(value['disabled']) is not bool or user_id==user['id']:raise LedgerError('账号停用参数无效')
        with engine.begin() as db:
            db.execute(update(s.users).where(s.users.c.id==user_id).values(disabled=value['disabled']))
            if value['disabled']:
                from sqlalchemy import delete
                db.execute(delete(s.user_sessions).where(s.user_sessions.c.user_id==user_id))
            ledger.audit(db,user,'user.disable',resource=user_id,details=value)
        return {'ok':True}

    @app.get('/api/platform/v2/workspaces')
    def workspaces(request:Request):return {'items':ledger.list_workspaces(actor(request))}
    @app.post('/api/platform/v2/workspaces',status_code=201)
    async def create_workspace(request:Request):
        value=await request.json()
        if set(value) not in ({'name'},{'name','idempotency_key'}) or not isinstance(value['name'],str):raise LedgerError('项目参数无效')
        request_key=value.get('idempotency_key')
        if request_key is not None and (not isinstance(request_key,str) or not 1<=len(request_key)<=200):raise LedgerError('项目请求键无效')
        return ledger.create_workspace(actor(request),value['name'],request_key=request_key)
    @app.get('/api/platform/v2/workspaces/{workspace}/member-options')
    def member_options(workspace:str,request:Request):
        with engine.connect() as db:
            ledger.role(db,actor(request),workspace,owner=True)
            return {'items':[dict(row) for row in db.execute(select(s.users.c.id,s.users.c.display_name,s.users.c.username).where(s.users.c.disabled.is_(False)).order_by(s.users.c.display_name)).mappings()]}
    @app.get('/api/platform/v2/workspaces/{workspace}/members')
    def members(workspace:str,request:Request):
        with engine.connect() as db:
            ledger.role(db,actor(request),workspace)
            rows=db.execute(select(s.workspace_members.c.user_id,s.workspace_members.c.role,s.users.c.display_name,s.users.c.username).join(s.users,s.users.c.id==s.workspace_members.c.user_id).where(s.workspace_members.c.workspace_id==workspace)).mappings()
            return {'items':[dict(r) for r in rows]}
    @app.put('/api/platform/v2/workspaces/{workspace}/members')
    def set_member(workspace:str,value:Member,request:Request):ledger.set_member(actor(request),workspace,value.user_id,value.role);return {'ok':True}
    @app.delete('/api/platform/v2/workspaces/{workspace}/members/{user_id}')
    def revoke_member(workspace:str,user_id:str,request:Request):
        ledger.remove_member(actor(request),workspace,user_id);return {'ok':True}

    async def resolve_scope(request,path,payload):
        response=await request.app.state.worker.post('/internal/scope',json={'path':path,'payload':payload,'query':request.url.query},headers=worker_headers(actor(request)))
        if response.status_code!=200:raise LedgerError('无法核实资源所属项目',response.status_code if response.status_code<500 else 503)
        return response.json().get('workspace_id')

    async def validate_inputs(request,workspace,inputs):
        if not inputs:return []
        response=await request.app.state.worker.post('/internal/inputs',json={'workspace_id':workspace,'inputs':inputs},headers=worker_headers(actor(request),workspace))
        if response.status_code!=200:raise LedgerError('资料版本或项目归属无效',409)
        return response.json()['items']

    async def session_inputs(request,workspace,command,inputs):
        if command['tool_id']!='agent' or inputs:return inputs
        session=command['path'].rsplit('/',2)[1]
        response=await request.app.state.worker.post('/internal/inputs',json={'workspace_id':workspace,'inputs':[],
            'agent_session_id':session},headers=worker_headers(actor(request),workspace))
        if not response.is_success:raise LedgerError('无法固定 Agent 会话的输入资料',409)
        return response.json()['items']

    async def resolve_ai_profile(request,command,profile_id=None):
        if command['tool_id']!='agent':return command
        if profile_id:
            with engine.connect() as db:profile=db.execute(select(s.ai_service_profiles).where(s.ai_service_profiles.c.id==profile_id,s.ai_service_profiles.c.enabled.is_(True))).mappings().first()
            if not profile:raise LedgerError('AI 服务不存在或已停用',409)
            value={name:profile[name] for name in ('id','name','provider','model')}
        else:
            response=await request.app.state.worker.post('/internal/ai/default',headers=worker_headers(actor(request)))
            if not response.is_success:raise LedgerError('无法读取默认 AI 服务',503)
            value=response.json()
            if not value:raise LedgerError('请先由管理员配置 AI 服务',409)
        return {**command,'resolved_ai_service':value}

    async def active_agent_task(request,command,workspace):
        if command['tool_id']!='agent':return None
        session_path=command['path'].rsplit('/',1)[0]
        response=await request.app.state.worker.get(session_path,headers=worker_headers(actor(request),workspace))
        if not response.is_success:return None
        active=next((run for run in response.json().get('runs',[]) if run['status'] in {'running','waiting_job','waiting_user','paused'}),None)
        if not active:return None
        with engine.connect() as db:
            linked=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace=='agent.runs',s.task_source_links.c.source_id==active['id'],s.task_source_links.c.source_generation<=active['generation']).order_by(s.task_source_links.c.source_generation.desc()).limit(1)).mappings().first()
            if not linked:raise LedgerError('原 Agent 运行未绑定任务，请由管理员核实',409)
            ledger._task(db,actor(request),linked['task_id'],control=True)
        return dict(linked)

    @app.post('/api/platform/v2/tasks',response_model=TaskSummary,status_code=202)
    async def submit_task(value:TaskCreate,request:Request):
        user=actor(request);data=value.model_dump()
        existing=await run_in_threadpool(ledger.existing_submission,user,data)
        if existing:return existing
        ledger.workspace(user,value.workspace_id,write=True)
        command=command_for(data)
        resolved=await resolve_scope(request,command['path'],command['payload'])
        if resolved!=value.workspace_id:raise LedgerError('任务和执行资源所属项目不一致',403)
        inputs=await validate_inputs(request,value.workspace_id,value.inputs)
        inputs=await session_inputs(request,value.workspace_id,command,inputs)
        active=await active_agent_task(request,command,value.workspace_id)
        if active:
            ledger.steer(user,active['task_id'],value.idempotency_key,command['path'],command['payload'])
            return ledger.detail(user,active['task_id'])['task']
        command=await resolve_ai_profile(request,command,value.ai_service_profile_id)
        from .private_payloads import seal
        command=seal(f_path(os.environ['WORKBENCH_DATA_DIR']) if os.environ.get('WORKBENCH_DATA_DIR') else None,command)
        data['source']='agent' if value.tool_id=='agent' else 'batch' if value.tool_id=='batch' else 'manual'
        return await run_in_threadpool(ledger.submit,user,data,command,inputs,request_identity=value.model_dump())

    @app.get('/api/platform/v2/tasks',response_model=TaskPage)
    def list_tasks(request:Request,limit:int=Query(50,ge=1,le=100),cursor:str|None=Query(None,max_length=2048),workspace_id:str|None=None,
                   created_by:str|None=None,tool_id:str|None=None,source:str|None=None,status:str|None=None,ai_service:str|None=None,keyword:str=Query('',max_length=200),
                   created_from:str|None=None,created_to:str|None=None,archived:bool=False):
        filters={k:v for k,v in locals().items() if k in {'workspace_id','created_by','tool_id','source','status','ai_service','keyword','created_from','created_to','archived'}}
        return ledger.list(actor(request),filters,limit,cursor)
    @app.get('/api/platform/v2/tasks/{task_id}',response_model=TaskDetail)
    def task_detail(task_id:str,request:Request):return ledger.detail(actor(request),task_id)
    @app.get('/api/platform/v2/tasks/{task_id}/collections/{name}')
    def task_collection(task_id:str,name:str,request:Request,limit:int=Query(200,ge=1,le=1000),cursor:str|None=Query(None,max_length=2048)):
        return ledger.collection(actor(request),task_id,name,limit,cursor)
    @app.get('/api/platform/v2/tasks/{task_id}/artifacts/{artifact_id}/download')
    async def task_artifact(task_id:str,artifact_id:str,request:Request):
        ledger.detail(actor(request),task_id)
        with engine.connect() as db:row=db.execute(select(s.task_artifact_links).where(s.task_artifact_links.c.id==artifact_id,s.task_artifact_links.c.task_id==task_id)).mappings().first()
        if not row or row['missing']:raise LedgerError('成果文件已不存在',404)
        if row['source_path']:
            path=f_path(row['source_path']);root=f_path(os.environ['WORKBENCH_DATA_DIR'])
            if not path.is_relative_to(root) or not path.is_file():raise LedgerError('成果文件不可用',404)
            from .files import hash_file
            if hash_file(path)!=row['blob_hash']:raise LedgerError('成果文件校验失败',409)
            with engine.begin() as db:ledger.audit(db,actor(request),'artifact.download',resource=artifact_id)
            return FileResponse(path,filename=row['name'])
        if not row['download_route'] or not row['download_route'].startswith('/api/platform/v1/'):raise LedgerError('成果下载映射尚未建立',409)
        result=await request.app.state.worker.get(row['download_route'],headers=worker_headers(actor(request)))
        if result.is_success:
            import hashlib
            if row['blob_hash'] and hashlib.sha256(result.content).hexdigest()!=row['blob_hash']:raise LedgerError('成果下载内容校验失败',409)
            with engine.begin() as db:ledger.audit(db,actor(request),'artifact.download',resource=artifact_id)
        from fastapi.responses import Response
        return Response(result.content,status_code=result.status_code,headers={'content-type':result.headers.get('content-type','application/octet-stream'),'content-disposition':result.headers.get('content-disposition','attachment')})
    @app.patch('/api/platform/v2/tasks/{task_id}',response_model=TaskSummary)
    def edit_task(task_id:str,value:TaskEdit,request:Request):return ledger.edit(actor(request),task_id,value.expected_revision,value.model_dump(exclude_none=True,exclude={'expected_revision'}))
    @app.post('/api/platform/v2/tasks/{task_id}/controls',status_code=202)
    def control(task_id:str,value:TaskControl,request:Request):return ledger.control(actor(request),task_id,value.operation,value.expected_revision,value.idempotency_key,value.scope)
    @app.get('/api/platform/v2/tasks/{task_id}/events')
    async def events(task_id:str,request:Request,after:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=500)):
        user=actor(request);ledger.detail(user,task_id)
        if 'text/event-stream' not in request.headers.get('accept',''):return ledger.events(user,task_id,after,limit)
        if request.headers.get('last-event-id'):
            try:after=max(after,int(request.headers['last-event-id']))
            except ValueError:raise LedgerError('事件游标无效')
        async def stream():
            cursor=after
            while not await request.is_disconnected():
                try:
                    current=await run_in_threadpool(ledger.authenticate,request.cookies.get(COOKIE))
                    page=await run_in_threadpool(ledger.events,current,task_id,cursor,limit)
                except LedgerError:break
                for event in page['items']:
                    cursor=event['seq'];yield f"id: {cursor}\nevent: task\ndata: {json.dumps(event,ensure_ascii=False)}\n\n"
                if not page['items']:yield ': keepalive\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.get('/api/platform/v2/metrics')
    def metrics(request:Request):
        if not actor(request)['is_admin']:raise LedgerError('需要平台管理员权限',403)
        with engine.connect() as db:
            queue=dict(db.execute(select(s.task_dispatches.c.state,func.count()).group_by(s.task_dispatches.c.state)).all())
            from .schema_v3 import workers
            heartbeats=wire([dict(r) for r in db.execute(select(workers)).mappings()])
        result={'dispatches':queue,'workers':heartbeats}
        root=os.environ.get('WORKBENCH_DATA_DIR')
        if root:
            import shutil
            result['disk_free_bytes']=shutil.disk_usage(root).free
            marker=Path(root)/'central-last-backup.json'
            result['last_backup']=json.loads(marker.read_text('utf-8')) if marker.is_file() else None
            error=Path(root)/'central-backup-error.json'
            result['last_backup_error']=json.loads(error.read_text('utf-8')) if error.is_file() else None
            sync_error=Path(root)/'central-event-sync-error.json'
            result['last_event_sync_error']=json.loads(sync_error.read_text('utf-8')) if sync_error.is_file() else None
            sync_metrics=Path(root)/'central-event-sync-metrics.json'
            result['event_sync']=json.loads(sync_metrics.read_text('utf-8')) if sync_metrics.is_file() else None
        return result

    @app.api_route('/api/platform/v1/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE','HEAD'])
    async def legacy_gateway(path:str,request:Request):
        user=actor(request);target='/api/platform/v1/'+path
        raw=b'';payload={}
        if 'application/json' in request.headers.get('content-type',''):
            raw=await request.body()
            if len(raw)>16*1024*1024:raise LedgerError('请求参数过大',413)
            try:payload=json.loads(raw) if raw else {}
            except ValueError:raise LedgerError('JSON 参数无效')
            if not isinstance(payload,dict):raise LedgerError('JSON 参数须为对象')
        if path=='workspaces' and request.method=='GET':
            rows=ledger.list_workspaces(user);limit=min(int(request.query_params.get('limit',200)),200)
            return {'items':[{k:r[k] for k in ('workspace_id','name','revision','created_at','updated_at')}|{'schema_version':'1.0'} for r in rows[:limit]],'next_cursor':None,'snapshot_revision':1}
        if path=='workspaces' and request.method=='POST':
            row=ledger.create_workspace(user,payload.get('name',''),request_key=payload.get('request_id') or request.headers.get('idempotency-key'))
            return JSONResponse({k:row[k] for k in ('workspace_id','name','revision','created_at','updated_at')}|{'schema_version':'1.0'},status_code=201)
        workspace=await resolve_scope(request,target,payload)
        hint=request.headers.get('x-clear-workspace')
        if workspace and hint and workspace!=hint:raise LedgerError('项目上下文与资源归属不一致',403)
        workspace=workspace or hint
        public_read=bool(re.fullmatch(r'(health|catalog(?:/.*)?|apps(?:/.*)?|data/health|multimodal/capabilities|agent/health)',path)) and request.method in {'GET','HEAD'}
        if workspace:ledger.workspace(user,workspace,write=request.method not in {'GET','HEAD'})
        elif not public_read and not user['is_admin']:raise LedgerError('此接口需要明确项目范围或管理员权限',403)
        if path.startswith('agent/connection') and not user['is_admin']:raise LedgerError('AI 服务由管理员管理',403)
        agent_session=re.match(r'agent/sessions/([A-Za-z0-9_-]+)(?:/|$)',path)
        if agent_session and workspace and request.method not in {'GET','HEAD'}:
            with engine.connect() as db:
                role=ledger.role(db,user,workspace,write=True)
                owner=db.execute(select(s.audit_events.c.actor_id).where(s.audit_events.c.action=='agent.session.create',s.audit_events.c.resource_id==agent_session[1]).order_by(s.audit_events.c.created_at).limit(1)).scalar_one_or_none()
                if role!='owner' and owner!=user['id']:raise LedgerError('仅会话创建者或项目管理员可修改 Agent 会话',403)
        if request.method not in {'GET','HEAD'} and re.search(r'/(cancel|resume|control|actions)(?:/|$)',path) and not path.endswith('/actions'):
            identifiers=re.findall(r'/(?:jobs|runs|batches)/([A-Za-z0-9_-]+)',path)
            if workspace:ledger.authorize_native_control(user,workspace,identifiers)
        if workspace and re.search(r'/(links|projects)(?:/|$)',path) and request.method not in {'GET','HEAD'}:
            with engine.connect() as db:ledger.role(db,user,workspace,owner=True)
        native_link=re.fullmatch(r'workspaces/[A-Za-z0-9_-]+/(ocr|classification)/links',path)
        if native_link and request.method=='POST' and not user['is_admin']:
            with engine.connect() as db:
                linked=db.execute(select(s.module_project_links.c.link_id).where(s.module_project_links.c.workspace_id==workspace,s.module_project_links.c.app_id==native_link[1],s.module_project_links.c.module_project_id==payload.get('project_id'))).first()
            if not linked:raise LedgerError('原生旧项目关联由平台管理员整理；可在当前项目创建新的原生项目',403)
        if 'actor' in payload:
            payload['actor']={'id':user['id'],'display_name':user['display_name'],'source':'local'} if isinstance(payload['actor'],dict) else user['display_name']
            raw=json.dumps(payload,ensure_ascii=False).encode()
        controls=[(r'/documents/([A-Za-z0-9_-]+)/control/(cancel|resume)','documents.documents'),
                  (r'/multimodal/jobs/([A-Za-z0-9_-]+)/control/(cancel|retry|retry_cpu)','manual.manual_analysis'),
                  (r'/forensics/([A-Za-z0-9_-]+)/(resume)','forensics.inspections'),
                  (r'/jobs/([A-Za-z0-9_-]+)/actions/(cancel|pause|resume|retry_failed)','projection'),
                  (r'/agent/runs/([A-Za-z0-9_-]+)/(cancel|pause|resume)','agent.runs'),
                  (r'/data/sessions/[A-Za-z0-9_-]+/jobs/([A-Za-z0-9_-]+)/(cancel)','data.jobs')]
        control_match=None
        for pattern,namespace in controls:
            found=re.search(pattern,target)
            if found:control_match=(namespace,found[1],found[2]);break
        if re.fullmatch(r'batches/[A-Za-z0-9_-]+/control',path):control_match=('batches.batches',path.split('/')[1],payload.get('operation'))
        if control_match and request.method=='POST':
            namespace,identifier,operation=control_match
            with engine.connect() as db:
                linked=db.execute(select(s.tasks).join(s.task_source_links,s.tasks.c.id==s.task_source_links.c.task_id).where(s.task_source_links.c.namespace==namespace,
                    s.task_source_links.c.source_id==identifier,s.tasks.c.workspace_id==workspace).order_by(s.task_source_links.c.source_generation.desc()).limit(1)).mappings().first()
            if linked:
                operation='retry' if operation in {'retry','retry_cpu','retry_failed'} or operation=='resume' and namespace=='forensics.inspections' else operation
                if operation=='resume' and namespace=='agent.runs' and linked['status']=='needs_verification':operation='verify'
                ledger.control(user,linked['id'],operation,linked['revision'],request.headers.get('idempotency-key') or payload.get('request_id') or key('control'))
                response=await request.app.state.worker.get(target.rsplit('/control/',1)[0] if '/control/' in target else target.rsplit('/actions/',1)[0] if '/actions/' in target else target.rsplit('/',1)[0],headers=worker_headers(user,workspace))
                value=response.json() if response.status_code==200 else {'request_state':'recorded'}
                return JSONResponse(value,status_code=200,headers={'X-Clear-Task-ID':linked['id']})
            if namespace=='agent.runs':raise LedgerError('Agent 运行未绑定任务，请由管理员核实',409)
        if request.method=='POST' and re.fullmatch(r'agent/sessions/[A-Za-z0-9_-]+/compact',path):
            session_response=await request.app.state.worker.get(target.rsplit('/',1)[0],headers=worker_headers(user,workspace))
            if not session_response.is_success:raise LedgerError('Agent 会话不可核实',409)
            candidate=next(iter(session_response.json().get('runs',[])),None)
            if candidate:
                with engine.connect() as db:
                    linked=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace=='agent.runs',
                        s.task_source_links.c.source_id==candidate['id'],s.task_source_links.c.source_generation<=candidate['generation'])
                        .order_by(s.task_source_links.c.source_generation.desc()).limit(1)).mappings().first()
                    if not linked:raise LedgerError('Agent 运行未绑定任务，请由管理员核实',409)
                    ledger._task(db,user,linked['task_id'],control=True)
        command=match(request.method,target,payload)
        headers=worker_headers(user,workspace)
        if command and command['tool_id']=='agent':
            active=await active_agent_task(request,command,workspace)
            if active:
                delivered=ledger.steer(user,active['task_id'],payload.get('request_id') or request.headers.get('idempotency-key') or key('message'),target,payload)
                for _ in range(300):
                    with engine.connect() as db:receipt=db.execute(select(s.task_dispatches.c.receipt).where(s.task_dispatches.c.id==delivered['dispatch_id'])).scalar_one()
                    if receipt.get('accepted'):return JSONResponse(receipt['body'],status_code=receipt['http_status'],headers={'X-Clear-Task-ID':active['task_id']})
                    await asyncio.sleep(.1)
                raise LedgerError('消息已记录，执行反馈待确认',503)
        upload=bool(request.method=='POST' and (re.fullmatch(r'workspaces/[A-Za-z0-9_-]+/assets/uploads(?:/[A-Za-z0-9_-]+)?',path) or re.fullmatch(r'data/sessions/[A-Za-z0-9_-]+/uploads',path)))
        staged_inputs=None
        if upload:
            import hashlib
            import shutil
            folder=f_path(os.environ['WORKBENCH_DATA_DIR'])/'task-upload-inputs';folder.mkdir(exist_ok=True)
            body_file=folder/key('upload');size=0;content_hash=hashlib.sha256()
            try:
                with body_file.open('xb') as stream:
                    async for chunk in request.stream():
                        if shutil.disk_usage(folder).free<256*1024**2:raise LedgerError('资料磁盘剩余空间不足，请联系管理员',507)
                        size+=len(chunk)
                        if size>128*1024**2:raise LedgerError('文件超过 128 MB',413)
                        stream.write(chunk);content_hash.update(chunk)
                    stream.flush();os.fsync(stream.fileno())
                if not size:raise LedgerError('文件为空')
            except BaseException:
                body_file.unlink(missing_ok=True);raise
            command={'tool_id':'assets','action':'import','method':'POST','path':target+('?' + request.url.query if request.url.query else ''),'payload':{},
                'transport':'raw','body_file':str(body_file),'body_hash':content_hash.hexdigest(),'content_type':request.headers.get('content-type','application/octet-stream'),'resource_class':'io'}
            staged_inputs=[{'asset_id':'upload'+content_hash.hexdigest()[:32],'asset_revision':1,'blob_hash':content_hash.hexdigest(),'name':request.query_params.get('name','上传文件'),'size_bytes':size}]
        if command and workspace:
            request_key=payload.get('request_id') or payload.get('request_key') or request.headers.get('idempotency-key') or ('upload:'+path if upload and '/assets/uploads/' in path else key('request'))
            identity={'workspace_id':workspace,'tool_id':command['tool_id'],'action':command['action'],'idempotency_key':request_key,
                'options':payload if not upload else {'query':dict(request.query_params),'sha256':command['body_hash'],'size_bytes':staged_inputs[0]['size_bytes']}}
            existing=await run_in_threadpool(ledger.existing_submission,user,identity)
            supplied_inputs=payload.get('inputs') or ([payload] if 'asset_id' in payload else [])
            inputs=[] if existing else staged_inputs if staged_inputs is not None else await validate_inputs(request,workspace,supplied_inputs)
            if not existing:inputs=await session_inputs(request,workspace,command,inputs)
            if not existing and command['tool_id']=='data':
                resolved_inputs=await request.app.state.worker.post('/internal/inputs',headers=headers,json={'workspace_id':workspace,'inputs':[],
                    'data_session_id':command['groups'][0],'upload_id':payload.get('payload',{}).get('upload_id')})
                if resolved_inputs.status_code==200:inputs=resolved_inputs.json()['items']
            submission={**identity,
                'source':'agent' if command['tool_id']=='agent' else 'batch' if command['tool_id']=='batch' else 'manual',
                'name':(task_label(command['tool_id'],command['action'])+(f" · {inputs[0]['name']}" if len(inputs)==1 else f" · {len(inputs)} 份资料" if inputs else ''))[:200],
                'idempotency_key':request_key}
            from .private_payloads import seal
            if existing:
                summary=existing
                if upload:body_file.unlink(missing_ok=True)
            else:
                command=await resolve_ai_profile(request,command)
                command=seal(f_path(os.environ['WORKBENCH_DATA_DIR']) if os.environ.get('WORKBENCH_DATA_DIR') else None,command)
                summary=await run_in_threadpool(ledger.submit,user,submission,command,inputs,request_identity=identity)
            # The compatibility endpoint returns the real native acceptance receipt.
            # The durable queue continues independently if this browser disconnects.
            receipt_run=summary['current_run_id']
            if summary['run_count']>1:
                with engine.connect() as db:receipt_run=db.execute(select(s.task_runs.c.id).where(s.task_runs.c.task_id==summary['id']).order_by(s.task_runs.c.attempt).limit(1)).scalar_one()
            for _ in range(300):
                with engine.connect() as db:
                    record=db.execute(select(s.task_dispatches.c.receipt,s.task_runs.c.status,s.task_runs.c.error).join(s.task_runs,s.task_dispatches.c.run_id==s.task_runs.c.id).where(s.task_dispatches.c.run_id==receipt_run,s.task_dispatches.c.kind=='execute')).first();receipt=record.receipt
                if receipt.get('accepted'):
                    if receipt.get('body',{}).get('export_file'):
                        target=f_path(receipt['body']['export_file']);root=f_path(os.environ['WORKBENCH_DATA_DIR'])
                        if not target.is_relative_to(root/'task-files') or not target.is_file():raise LedgerError('导出文件不可用',404)
                        with engine.begin() as db:ledger.audit(db,user,'task.export.download',workspace,summary['id'])
                        return FileResponse(target,filename=target.name,media_type=receipt['body'].get('media_type'),headers={'X-Clear-Task-ID':summary['id']})
                    response=JSONResponse(receipt.get('body',{}),status_code=receipt.get('http_status',200))
                    response.headers['X-Clear-Task-ID']=summary['id'];return response
                if record.status in {'failed','cancelled'}:return JSONResponse({'detail':record.error or '任务在提交到工具前已结束','task_id':summary['id']},status_code=409,headers={'X-Clear-Task-ID':summary['id']})
                await asyncio.sleep(.1)
            return JSONResponse({'detail':'任务已记录，执行端尚未返回接收回执，请到任务中心查看','task_id':summary['id']},status_code=503,headers={'X-Clear-Task-ID':summary['id']})
        headers['Content-Type']=request.headers.get('content-type','application/json')
        if request.method not in {'GET','HEAD'}:
            with engine.begin() as db:ledger.audit(db,user,'legacy.'+request.method.lower(),workspace,target,{'keys':list(payload)})
        content=raw if raw else request.stream() if request.method not in {'GET','HEAD'} else None
        query=request.url.query
        if workspace and request.method=='GET' and path in {'batches','data/sessions'}:
            from urllib.parse import urlencode,parse_qsl
            pairs=[(name,item) for name,item in parse_qsl(query,keep_blank_values=True) if name!='workspace_id'];pairs.append(('workspace_id',workspace));query=urlencode(pairs)
        outgoing=request.app.state.worker.build_request(request.method,target+('?' + query if query else ''),headers=headers,content=content)
        response=await request.app.state.worker.send(outgoing,stream=True)
        native_listing=re.fullmatch(r'workspaces/[A-Za-z0-9_-]+/(ocr|classification)/projects',path) if request.method=='GET' else None
        if response.headers.get('content-type','').startswith('application/json') and ('/launches/' in path or path.endswith('/navigation') or path=='recent-work' or path.endswith('/agent/sessions') and request.method=='POST' or native_listing):
            raw_response=await response.aread();await response.aclose()
            try:value=json.loads(raw_response)
            except ValueError:value={}
            if native_listing and not user['is_admin']:
                with engine.connect() as db:allowed=set(db.execute(select(s.module_project_links.c.module_project_id).where(s.module_project_links.c.workspace_id==workspace,s.module_project_links.c.app_id==native_listing[1])).scalars())
                value={**value,'items':[row for row in value.get('items',[]) if row.get('project_id') in allowed]}
            if path=='recent-work' and not user['is_admin']:
                value={**value,'items':[row for row in value.get('items',[]) if row.get('workspace_id')==workspace or isinstance(row.get('context'),dict) and row['context'].get('workspace_id')==workspace]}
            if path.endswith('/agent/sessions') and request.method=='POST' and response.is_success:
                with engine.begin() as db:ledger.audit(db,user,'agent.session.create',workspace,value.get('id'))
            application=next((part for part in path.split('/') if part in {'ocr','classification'}),None)
            if workspace and application:
                with engine.connect() as db:link=db.execute(select(s.module_project_links.c.link_id).where(s.module_project_links.c.workspace_id==workspace,s.module_project_links.c.app_id==application).limit(1)).scalar_one_or_none()
                if link:
                    address=public_origin+f'/native/{workspace}/{application}/{link}/'
                    def replace_urls(node):
                        if isinstance(node,dict):
                            return {name:address if name in {'url','base_url'} and isinstance(item,str) and item.startswith('http://127.0.0.1:') else replace_urls(item) for name,item in node.items()}
                        if isinstance(node,list):return [replace_urls(item) for item in node]
                        return node
                    value=replace_urls(value)
            return JSONResponse(value,status_code=response.status_code)
        async def stream_response():
            try:
                async for chunk in response.aiter_bytes():yield chunk
            finally:await response.aclose()
        forwarded={k:v for k,v in response.headers.items() if k.lower() in {'content-type','content-disposition','content-security-policy','x-content-type-options'}}
        return StreamingResponse(stream_response(),status_code=response.status_code,headers=forwarded)

    @app.api_route('/native/{workspace}/{application}/{link}/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE'])
    async def native_gateway(workspace:str,application:str,link:str,path:str,request:Request):
        # Native HTML and its APIs share the same authenticated origin and scope.
        user=await run_in_threadpool(ledger.authenticate,request.cookies.get(COOKIE))
        ledger.workspace(user,workspace,write=request.method!='GET')
        if request.method!='GET':
            csrf=request.headers.get('x-clear-csrf','')
            if request.headers.get('origin')!=public_origin or not secrets.compare_digest(token_hash(csrf),user['csrf_hash']):raise LedgerError('原生请求身份校验失败',403)
        request.state.actor=user
        envelope={
            'workspace_id':workspace,'application':application,'link_id':link,'path':path,'method':request.method,
            'query':request.url.query,'content_hex':'','content_type':request.headers.get('content-type',''),'idempotency_key':request.headers.get('idempotency-key')}
        staged=None
        try:
            if request.method not in {'GET','HEAD'}:
                import shutil
                folder=f_path(os.environ['WORKBENCH_DATA_DIR'])/'native-request-staging';folder.mkdir(exist_ok=True);staged=folder/key('request');size=0
                with staged.open('xb') as stream:
                    async for chunk in request.stream():
                        size+=len(chunk)
                        if size>128*1024**2:raise LedgerError('单次上传超过 128 MB，请分批上传',413)
                        if shutil.disk_usage(folder).free<256*1024**2:raise LedgerError('资料磁盘剩余空间不足，请联系管理员',507)
                        stream.write(chunk)
                envelope.pop('content_hex');envelope['staged_body_file']=str(staged)
            response=await request.app.state.worker.post('/internal/native',headers=worker_headers(user,workspace),json=envelope)
        finally:
            if staged:staged.unlink(missing_ok=True)
        if response.status_code!=200:raise LedgerError('原生工具请求不可用',response.status_code)
        value=response.json()
        from .files import public_response
        return public_response(f_path(os.environ['WORKBENCH_DATA_DIR']),value)

    @app.get('/{path:path}')
    def portal(path:str):
        root=require_path(os.environ.get('WORKBENCH_PORTAL_DIST',Path(__file__).resolve().parents[3]/'apps/portal/dist'),
                          'Portal static files',role='read')
        if '\\' in path or ':' in path or any(part in {'.','..'} for part in path.split('/')):raise LedgerError('页面路径无效',404)
        target=(root/path).resolve()
        if not target.is_relative_to(root):raise LedgerError('页面路径无效',404)
        if target.is_file():return FileResponse(target)
        index=root/'index.html'
        if not index.is_file():raise LedgerError('门户尚未构建',503)
        return FileResponse(index)
    return app
