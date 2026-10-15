"""Private execution host and durable native outbox publisher."""
import asyncio
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import uuid
import logging
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, insert, update, and_, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from .database import check_schema,f_path
from .store import Ledger,LedgerError,key,now,wire,safe,digest,TERMINAL
from . import schema_v1 as s
from . import journal,registry,scope

def normalized(value):
    native=value.get('status') or value.get('state') or 'queued'
    aliases={'completed':'succeeded','completed_with_errors':'partial','waiting_job':'running',
             'interrupted':'needs_verification','unknown':'needs_verification','waiting_input':'waiting_user',
             'pending':'queued','stopping':'cancelling','waiting_gpu':'waiting_resource'}
    result=aliases.get(native,native)
    if native=='completed_with_errors' and value.get('done',value.get('counts',{}).get('succeeded'))==0:result='failed'
    return result if result in {'queued','waiting_resource','running','waiting_user','pausing','paused','cancelling','needs_verification',*TERMINAL} else 'needs_verification'

def observation(body):
    return {'native_status':str(body.get('native_status') or body.get('status') or body.get('state') or '')[:256],
            'stage':str(body.get('stage') or body.get('phase') or '')[:256],
            'progress_done':body.get('progress_done',body.get('done')),
            'progress_total':body.get('progress_total',body.get('total')),
            'error':str(body.get('error'))[:4000] if body.get('error') else None,
            'availability':body.get('availability','ready'),
            'summary':{'native_capabilities':body.get('capabilities',[]),'counts':body.get('counts',{}),
                       'result_hash':body.get('result_hash') or body.get('report_hash')}}

def scoped_observation(body,namespace):
    value=observation(body);state=normalized(body)
    if namespace in {'data.jobs','manual.manual_analysis','agent.runs','batches.batches'} and state not in TERMINAL|{'needs_verification'}:
        value['summary']['native_capabilities']=['cancel']+(['resume'] if namespace=='batches.batches' and state=='waiting_user' else [])
    return value

def as_date(value):
    if not value:return None
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:return None

def native_event_summary(payload):
    fields={'id','job_id','inspection_id','run_id','session_id','page_id','image_id','version_id','result_id','operation_id','generation','status','state','phase','stage',
        'action','kind','tool','engine','engine_package','actual_device','done','total','created','created_at','started','started_at','finished','finished_at','updated','updated_at','result_hash','report_hash'}
    result={name:item for name,item in payload.items() if name in fields and (item is None or isinstance(item,(str,int,float,bool)))}
    if payload.get('error'):result['error']=str(payload['error'])[:4000]
    if isinstance(payload.get('progress'),dict):result['progress']={name:item for name,item in payload['progress'].items() if item is None or isinstance(item,(int,float,bool))}
    if isinstance(payload.get('files'),list):result['file_count']=len(payload['files'])
    return safe(result)

class ExecutionHost:
    def __init__(self,engine,root,client):
        self.engine=engine;self.ledger=Ledger(engine);self.root=root;self.client=client
        self.worker_id='worker-'+uuid.uuid4().hex;self.closed=asyncio.Event();self.active={};self.publish_offsets={};self.last_loop_error=0
        from .source_identity import fingerprint
        self.source_root=Path(__file__).resolve().parents[3]
        self.source_version=fingerprint(self.source_root)
        self.native_sources={}
        for application in ('ocr','classification'):
            binding=root/'agent-native/bundles'/application/'binding.json'
            if binding.is_file():
                source=f_path(json.loads(binding.read_text('utf-8'))['source'])
                self.native_sources[application]={'path':source,'version':fingerprint(source)}
        self.command_db=root/'worker-commands.sqlite3';self.last_install=0
        from .schema_v3 import workers
        import socket
        with self.engine.begin() as db:
            if not self.command_db.exists() and db.execute(select(workers.c.id).where(workers.c.root_identity==digest(str(root))).limit(1)).first():
                raise RuntimeError('执行回执库缺失；请从一致性备份恢复，不能假定原执行从未启动')
            db.execute(insert(workers).values(id=self.worker_id,hostname=socket.gethostname(),root_identity=digest(str(root)),started_at=now(),heartbeat_at=now(),concurrency=int(os.environ.get('CLEAR_TASK_CONCURRENCY','1'))))
        with closing(sqlite3.connect(self.command_db)) as db,db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS command_receipts(dispatch_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,receipt TEXT,updated_at TEXT NOT NULL)''')

    def command_receipt(self,identifier):
        with closing(sqlite3.connect(self.command_db)) as db:
            row=db.execute('SELECT stage,receipt FROM command_receipts WHERE dispatch_id=?',(identifier,)).fetchone()
        return (row[0],json.loads(row[1]) if row[1] else {}) if row else None

    def save_receipt(self,identifier,stage,receipt=None):
        with closing(sqlite3.connect(self.command_db)) as db,db:
            db.execute('INSERT INTO command_receipts VALUES(?,?,?,?) ON CONFLICT(dispatch_id) DO UPDATE SET stage=excluded.stage,receipt=excluded.receipt,updated_at=excluded.updated_at',
                (identifier,stage,json.dumps(safe(receipt or {}),ensure_ascii=False),wire(now())))

    async def loop(self):
        while not self.closed.is_set():
            try:
                from .schema_v3 import workers
                if asyncio.get_running_loop().time()-self.last_install>10:
                    with self.engine.begin() as db:db.execute(update(workers).where(workers.c.id==self.worker_id).values(heartbeat_at=now()))
                    metrics={**await asyncio.to_thread(journal.backlog,self.root),'sampled_at':wire(now()),'worker_id':self.worker_id}
                    temporary=self.root/'central-event-sync-metrics.new'
                    temporary.write_text(json.dumps(metrics),'utf-8');os.replace(temporary,self.root/'central-event-sync-metrics.json')
                    self.last_install=asyncio.get_running_loop().time()
                flag=self.root/'central-maintenance.json'
                try:phase=json.loads(flag.read_text('utf-8')).get('phase') if flag.is_file() else None
                except (ValueError,OSError):phase='draining' if flag.exists() else None
                if phase=='snapshot':
                    await asyncio.sleep(.5);continue
                paths=await asyncio.to_thread(journal.install_all,self.root)
                await self.publish(paths)
                self.ledger.expire_leases()
                self.resume_accepted()
                for identifier,item in list(self.active.items()):
                    if item.done():
                        try:item.result()
                        except Exception:logging.getLogger(__name__).exception('Task command failed: %s',identifier)
                        self.active.pop(identifier,None)
                dispatch=None if phase else self.ledger.claim(self.worker_id,heavy_limit=int(os.environ.get('CLEAR_TASK_CONCURRENCY','1')))
                if dispatch:self.active[dispatch['id']]=asyncio.create_task(self.execute(dispatch))
            except Exception as error:
                stamp=asyncio.get_running_loop().time()
                if stamp-self.last_loop_error>=30:
                    logging.getLogger(__name__).warning('Execution service will retry after %s; native outboxes remain durable',type(error).__name__)
                    self.last_loop_error=stamp
            try:await asyncio.wait_for(self.closed.wait(),.5)
            except TimeoutError:pass

    def claim_control(self):
        with self.engine.begin() as db:
            row=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.state=='queued',s.task_dispatches.c.kind=='control').order_by(s.task_dispatches.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not row:return None
            from datetime import timedelta
            lease=key('lease')
            db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==row['id']).values(state='leased',worker_id=self.worker_id,lease_token=lease,lease_until=now()+timedelta(seconds=60),heartbeat_at=now()))
            return {**row,'lease_token':lease,'worker_id':self.worker_id}

    def resume_accepted(self):
        """Reattach a persisted acceptance receipt; never resend an uncertain call."""
        from datetime import timedelta
        with self.engine.begin() as db:
            rows=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.kind=='execute',s.task_dispatches.c.state=='needs_verification',s.task_dispatches.c.updated_at<now()-timedelta(seconds=10)).with_for_update(skip_locked=True).limit(20)).mappings().all()
            for row in rows:
                if row['id'] in self.active:continue
                if row['receipt'].get('native_requires_verification'):continue
                record=self.command_receipt(row['id'])
                if not record or record[0]!='accepted' or not record[1].get('binding'):continue
                lease=key('lease');dispatch={**row,'worker_id':self.worker_id,'lease_token':lease}
                db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==row['id']).values(state='observing',worker_id=self.worker_id,lease_token=lease,lease_until=now()+timedelta(seconds=60)))
                self.active[row['id']]=asyncio.create_task(self.execute(dispatch))

    async def heartbeats(self,dispatch):
        while not self.closed.is_set():
            await asyncio.sleep(10)
            if self.snapshot_active():continue
            try:
                if not self.ledger.heartbeat(dispatch):return
            except SQLAlchemyError:pass

    async def call(self,method,path,payload=None,step=None):
        headers={'Origin':'http://127.0.0.1:8890','Host':'127.0.0.1:8890'}
        if step:headers['X-Clear-Step']=step
        return await self.client.request(method,path,json=payload if method not in {'GET','HEAD'} else None,headers=headers)

    async def execute(self,dispatch):
        heartbeat=asyncio.create_task(self.heartbeats(dispatch))
        try:
            if dispatch['kind']=='control':await self.control(dispatch);return
            previous=self.command_receipt(dispatch['id'])
            if previous and previous[0]=='sending':
                self.ledger.observe(dispatch,'needs_verification',identity=key('uncertain'),observation={'availability':'unreachable','error':'执行请求回执不明，需核实后恢复'})
                return
            if previous and previous[0]=='accepted':receipt=previous[1]
            else:
                from .private_payloads import unseal
                command=unseal(self.root,dict(dispatch['command']));payload=dict(command.get('payload',{}))
                from .source_identity import fingerprint
                if fingerprint(self.source_root)!=self.source_version:raise LedgerError('程序版本已变化，请重新启动执行服务后发起任务',409)
                application=command.get('native',{}).get('application') or ('ocr' if command['tool_id']=='visual_review' else command['tool_id'])
                native_source=self.native_sources.get(application)
                if native_source and fingerprint(native_source['path'])!=native_source['version']:raise LedgerError('原生工具版本已变化，请重新启动执行服务后发起任务',409)
                tool_version=native_source['version'] if native_source else self.source_version
                path=command['path'];method=command['method']
                with self.engine.connect() as db:
                    task=db.execute(select(s.tasks).where(s.tasks.c.id==dispatch['task_id'])).mappings().one()
                    run=db.execute(select(s.task_runs).where(s.task_runs.c.id==dispatch['run_id'])).mappings().one()
                    user=db.execute(select(s.users).where(s.users.c.id==(run['requested_by'] or task['created_by']))).mappings().one()
                    self.ledger._task(db,user,task['id'],control=True)
                    if user['disabled']:raise LedgerError('提交账号已停用',403)
                    request_snapshot=db.execute(select(s.task_runs.c.request_snapshot).where(s.task_runs.c.id==dispatch['run_id'])).scalar_one()
                if task['status']=='cancelling':
                    self.ledger.observe(dispatch,'cancelled',identity=dispatch['id']+':cancelled-before-send',terminal=True)
                    with self.engine.begin() as db:db.execute(update(s.task_dispatches).where(s.task_dispatches.c.run_id==dispatch['run_id'],s.task_dispatches.c.kind=='control',s.task_dispatches.c.state=='queued').values(state='done',receipt={'cancelled_before_send':True},updated_at=now()))
                    return
                request_snapshot={**request_snapshot,'tool_version':tool_version,'execution_host_version':self.source_version}
                with self.engine.begin() as db:
                    db.execute(update(s.tasks).where(s.tasks.c.id==dispatch['task_id']).values(tool_version=tool_version))
                    db.execute(update(s.task_runs).where(s.task_runs.c.id==dispatch['run_id']).values(request_snapshot=request_snapshot))
                if command['tool_id']=='agent':
                    from .ai_services import capture_current,activate
                    profile_id=request_snapshot.get('ai_service_profile_id') or command.get('resolved_ai_service',{}).get('id')
                    profile=activate(self.engine,self.legacy,self.root,profile_id) if profile_id else capture_current(self.engine,self.legacy,self.root)
                    if profile:
                        snapshot={name:profile.get(name) for name in ('id','name','provider','model')}
                        with self.engine.begin() as db:db.execute(update(s.task_runs).where(s.task_runs.c.id==dispatch['run_id']).values(request_snapshot={**request_snapshot,'resolved_ai_service':snapshot}))
                if command.get('retry_of'):
                    method,path,payload=await self.retry_command(dispatch,command)
                if 'actor' in payload:payload['actor']={'id':user['id'],'display_name':user['display_name'],'source':'local'} if isinstance(payload['actor'],dict) else user['display_name']
                journal.install_all(self.root)
                if not self.ledger.heartbeat(dispatch):return
                self.save_receipt(dispatch['id'],'sending')
                native_response=None
                if method=='INTERNAL_OCR_RETRY':
                    ref,reused=await self.retry_ocr(dispatch,payload)
                    response=httpx.Response(202,json={'job':ref,'reused_task_ids':reused})
                elif method=='INTERNAL_DOCUMENT_RETRY':
                    from .native import perform_native
                    from .files import response_bytes
                    native={**command['native'],'path':'api/platform/v2/document-stage-retries','method':'POST','query':'','trusted_task':True,'content_type':'application/json',
                        'content_hex':json.dumps({'stage_ids':payload['stage_ids'],'request_id':dispatch['run_id']}).encode().hex()}
                    native.pop('body_file',None)
                    native_response=await perform_native(self.legacy,self.root,native)
                    response=httpx.Response(native_response['status'],content=response_bytes(self.root,native_response),headers=native_response['headers'])
                elif command.get('transport')=='raw':
                    from .files import hash_file
                    source=f_path(command['body_file'])
                    if not source.is_relative_to(self.root/'task-upload-inputs') or hash_file(source)!=command['body_hash']:raise LedgerError('上传输入校验失败',409)
                    async def chunks():
                        with source.open('rb') as stream:
                            while chunk:=stream.read(1024*1024):yield chunk
                    response=await self.client.request(method,path,content=chunks(),headers={'Content-Type':command['content_type'],'Origin':'http://127.0.0.1:8890','Host':'127.0.0.1:8890','X-Clear-Step':dispatch['run_id']})
                elif command.get('transport')=='native' and path=='/internal/native':
                    from .native import perform_native
                    from .files import response_bytes
                    native_response=await perform_native(self.legacy,self.root,command['native']|{'idempotency_key':command['native'].get('idempotency_key') or dispatch['run_id']})
                    response=httpx.Response(native_response['status'],content=response_bytes(self.root,native_response),headers=native_response['headers'])
                else:response=await self.call(method,path,payload,dispatch['run_id'])
                try:body=response.json()
                except ValueError:
                    export=self.root/'task-files'/dispatch['task_id']/dispatch['run_id']
                    export.mkdir(parents=True,exist_ok=True)
                    from .files import response_name
                    target=export/response_name(response.headers);target.write_bytes(response.content)
                    body={'export_file':str(target),'size_bytes':len(response.content),'media_type':response.headers.get('content-type')}
                native_binding=('projection',body['job']['platform_job_id'],f"/api/platform/v1/workspaces/{task['workspace_id']}/jobs/{body['job']['platform_job_id']}") if method=='INTERNAL_OCR_RETRY' and response.is_success else None
                if native_response and isinstance(body,dict) and response.is_success:
                    if command['action']=='import':self.record_native_import(dispatch,command,body)
                    ids=body.get('task_ids') or ([body.get('job_id') or body.get('task_id')] if body.get('job_id') or body.get('task_id') else [])
                    if ids:
                        native=command['native']
                        ref=await asyncio.to_thread(self.legacy.state.job_service.register,native['workspace_id'],native['link_id'],native['application'],ids)
                        native_binding=('projection',ref['platform_job_id'],f"/api/platform/v1/workspaces/{native['workspace_id']}/jobs/{ref['platform_job_id']}")
                        for identifier in ids:self.native_generation('ocr.tasks' if native['application']=='ocr' else 'classification.jobs',identifier,dispatch['generation'])
                    stage_ids=body.get('stage_ids') or ([body['stage_id']] if body.get('stage_id') else [])
                    if stage_ids:
                        for identifier in stage_ids:
                            with self.engine.connect() as db:existing=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace=='ocr.document_stages',s.task_source_links.c.source_id==identifier).limit(1)).mappings().first()
                            if not existing:self.bind(dispatch,'ocr.document_stages',identifier,generation=1)
                            elif existing['run_id']!=dispatch['run_id']:
                                with self.engine.begin() as db:db.execute(pg_insert(s.task_step_runs).values(id='reuse'+digest([dispatch['run_id'],identifier])[:32],task_id=dispatch['task_id'],run_id=dispatch['run_id'],step_key='reuse:'+identifier,attempt=1,tool_id='ocr',action='reuse',status='succeeded',native_id=identifier,details={'reused':True,'compute_performed':False}).on_conflict_do_nothing(index_elements=['id']))
                        native_binding=('ocr.document_stages',digest(stage_ids),stage_ids)
                receipt={'accepted':True,'http_status':response.status_code,'body':body,'binding':native_binding or (registry.binding(command,body) if isinstance(body,dict) else None)}
                if native_response:receipt['native_response']=native_response
                self.save_receipt(dispatch['id'],'accepted',receipt)
            body=receipt['body']
            if receipt['http_status']>=400:
                if receipt['http_status']>=500 and receipt.get('native_response'):
                    self.ledger.observe(dispatch,'needs_verification',identity=dispatch['id']+':native-error',observation={'error':'原生服务返回异常，需核实接收记录'},receipt=receipt);return
                self.ledger.observe(dispatch,'failed',identity=dispatch['id']+':rejected',observation={'error':str(body.get('detail','工具拒绝执行'))[:4000]},receipt=receipt,terminal=True);return
            if receipt.get('binding'):
                if receipt['binding'][0]!='ocr.document_stages':self.bind(dispatch,*receipt['binding'][:2],generation=body.get('generation') or self.current_native_generation(*receipt['binding'][:2]))
                self.expand_projection(dispatch,receipt['binding'])
                self.ledger.observe(dispatch,normalized(body),identity=dispatch['id']+':accepted',observation=scoped_observation(body,receipt['binding'][0]),receipt=receipt,
                    terminal=normalized(body) in TERMINAL)
                if normalized(body) not in TERMINAL:await self.poll(dispatch,receipt)
            else:
                if isinstance(body,dict):self.record_export(dispatch,body)
                errors=body.get('errors',[]) if isinstance(body,dict) else []
                imported=any(body.get(name) for name in ('documents','images','asset_ids','imported')) if isinstance(body,dict) else False
                if errors and not imported:
                    with self.engine.connect() as db:imported=bool(db.execute(select(s.task_step_runs.c.id).where(s.task_step_runs.c.run_id==dispatch['run_id'],s.task_step_runs.c.action=='reuse',s.task_step_runs.c.status=='succeeded').limit(1)).first())
                status=('partial' if imported else 'failed') if errors else 'succeeded'
                self.ledger.observe(dispatch,status,identity=dispatch['id']+':complete',observation={'error':'；'.join(str(item.get('message') or item.get('error') or item) for item in errors[:10])[:4000]} if errors else {},receipt=receipt,terminal=True)
        except Exception as error:
            logging.getLogger(__name__).exception('Execution failed: %s',dispatch['id'])
            if dispatch['kind']=='control':
                try:
                    with self.engine.begin() as db:
                        db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id'],s.task_dispatches.c.lease_token==dispatch['lease_token']).values(state='needs_verification',receipt={'error':'控制请求未确认，请核实'},updated_at=now()))
                        task=db.execute(select(s.tasks).where(s.tasks.c.id==dispatch['task_id']).with_for_update()).mappings().one()
                        if task['current_run_id']==dispatch['run_id']:db.execute(update(s.tasks).where(s.tasks.c.id==task['id']).values(summary={**task['summary'],'control_verification_required':True},revision=task['revision']+1))
                        self.ledger._event(db,dispatch['task_id'],dispatch['run_id'],'control_failed',{'action':dispatch['command'].get('action'),'error':str(error)[:1000] if isinstance(error,LedgerError) else '控制请求未确认'})
                except SQLAlchemyError:pass
                return
            record=self.command_receipt(dispatch['id'])
            known_unstarted=record is None
            if not known_unstarted:
                with self.engine.begin() as db:
                    existing=db.execute(select(s.tasks.c.status,s.tasks.c.availability).where(s.tasks.c.id==dispatch['task_id'])).first()
                    if existing and existing.status=='needs_verification' and existing.availability=='unreachable':
                        db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id'],s.task_dispatches.c.lease_token==dispatch['lease_token']).values(state='needs_verification',updated_at=now()))
                        return
            try:self.ledger.observe(dispatch,'failed' if known_unstarted else 'needs_verification',identity=key('uncertain'),
                observation={'error':str(error)[:4000] if isinstance(error,LedgerError) else '执行或同步中断，待核实','availability':'unreachable'},terminal=known_unstarted)
            except (SQLAlchemyError,LedgerError):pass
        finally:heartbeat.cancel()

    def bind(self,dispatch,namespace,identifier,generation=1):
        with self.engine.begin() as db:
            previous=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace==namespace,s.task_source_links.c.source_id==identifier,s.task_source_links.c.source_generation==generation)).mappings().first()
            if previous:
                if previous['run_id']!=dispatch['run_id']:raise LedgerError('原生记录已绑定另一执行轮次',409)
                return
            db.execute(insert(s.task_source_links).values(id=key('src'),task_id=dispatch['task_id'],run_id=dispatch['run_id'],namespace=namespace,source_id=identifier,source_generation=generation,metadata={'worker_id':self.worker_id}))

    def expand_projection(self,dispatch,binding):
        if binding[0]!='projection':return
        path=self.root/'jobs.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            row=db.execute('SELECT app_id,native_ids FROM projected_jobs WHERE platform_job_id=?',(binding[1],)).fetchone()
        if row:
            namespace='ocr.tasks' if row[0]=='ocr' else 'classification.jobs'
            for identifier in json.loads(row[1]):
                with self.engine.connect() as central:previous=central.execute(select(s.task_source_links).where(s.task_source_links.c.namespace==namespace,s.task_source_links.c.source_id==identifier).limit(1)).mappings().first()
                generation=dispatch['generation'] if previous and previous['run_id']!=dispatch['run_id'] else 1
                self.bind(dispatch,namespace,identifier,generation)
                self.native_generation(namespace,identifier,generation)

    async def poll(self,dispatch,receipt):
        last=None
        while not self.closed.is_set():
            if self.snapshot_active():await asyncio.sleep(.5);continue
            body=await self.native_snapshot(receipt['binding']);state=normalized(body);observed=scoped_observation(body,receipt['binding'][0]);fingerprint=digest(observed|{'status':state})
            if fingerprint!=last:
                changed=self.ledger.observe(dispatch,state,identity=dispatch['id']+':'+fingerprint,observation=observed,receipt={**receipt,**({'native_requires_verification':True} if state=='needs_verification' else {})},terminal=state in TERMINAL)
                last=fingerprint
            if state in TERMINAL or state=='needs_verification':return
            await asyncio.sleep(1)

    def snapshot_active(self):
        path=self.root/'central-maintenance.json'
        try:return path.is_file() and json.loads(path.read_text('utf-8')).get('phase')=='snapshot'
        except (ValueError,OSError):return path.exists()

    async def native_snapshot(self,binding):
        if binding[0]=='native.imports':return dict(binding[2])
        if binding[0]=='ocr.document_stages':
            ids=binding[2]
            with closing(sqlite3.connect(self.root/'native/ocr/workbench.sqlite3')) as db:
                db.row_factory=sqlite3.Row
                rows=[dict(r) for r in db.execute('SELECT * FROM document_stages WHERE id IN ('+','.join('?' for _ in ids)+')',ids)]
            if len(rows)!=len(ids):raise LedgerError('页面处理阶段记录不完整',409)
            states=[normalized(row) for row in rows];finished=sum(state in TERMINAL for state in states)
            state=('succeeded' if all(x=='succeeded' for x in states) else 'partial' if 'succeeded' in states else 'failed') if finished==len(ids) else 'needs_verification' if 'needs_verification' in states else 'running' if 'running' in states else 'paused' if all(x=='paused' for x in states) else 'queued'
            return {'status':state,'done':finished,'total':len(ids),'error':next((row['error'] for row in rows if row.get('error')),None)}
        response=await self.call('GET',binding[2])
        if response.status_code>=400:raise LedgerError('无法核实原生执行状态',503)
        return response.json()

    async def retry_command(self,dispatch,command):
        with self.engine.connect() as db:
            old=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.run_id==command['retry_of'],s.task_dispatches.c.kind=='execute')).mappings().one()
        binding=old['receipt'].get('binding')
        if not binding:
            errors=old['receipt'].get('body',{}).get('errors',[]) if isinstance(old['receipt'].get('body'),dict) else []
            if errors and command.get('transport')=='native' and command['action']=='import':
                from .multipart import failed_files
                from workbench_platform.agent.credentials import CredentialVault
                native=command['native'];source=f_path(native['body_file']);raw=source.read_bytes()
                if native.get('body_encoding')=='dpapi':raw=CredentialVault._crypt(raw,decrypt=True)
                raw,content_type=failed_files(raw,native['content_type'],[item['name'] for item in errors],dispatch['run_id'])
                target=self.root/'native-requests'/(key('retry')+'.bin');target.write_bytes(CredentialVault._crypt(raw))
                import hashlib
                command['native']={**native,'body_file':str(target),'body_encoding':'dpapi','body_hash':hashlib.sha256(raw).hexdigest(),'content_type':content_type,'idempotency_key':dispatch['run_id']}
                with self.engine.begin() as db:db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id']).values(command=safe(command)))
                self.reuse_successful_steps(dispatch,command['retry_of'])
            return command['method'],command['path'],command.get('payload',{})
        namespace,identifier,path=binding
        if namespace=='ocr.document_stages':return 'INTERNAL_DOCUMENT_RETRY','',{'stage_ids':path}
        if namespace=='manual.manual_analysis':
            return 'POST',path+'/control/retry',{}
        if namespace=='forensics.inspections':
            payload=dict(command.get('payload',{}));payload['request_id']=dispatch['run_id']
            return 'POST',command['path'],payload
        if namespace=='projection':
            with closing(sqlite3.connect(self.root/'jobs.sqlite3')) as db:row=db.execute('SELECT app_id FROM projected_jobs WHERE platform_job_id=?',(identifier,)).fetchone()
            if row and row[0]=='ocr':return 'INTERNAL_OCR_RETRY','',{'binding':binding,'original_run_id':command['retry_of']}
            self.reuse_successful_steps(dispatch,command['retry_of'])
            return 'POST',path+'/actions/retry_failed',{}
        if namespace=='batches.batches':
            self.native_generation(namespace,identifier,dispatch['generation'])
            return 'POST',path+'/control',{'operation':'retry'}
        if namespace=='agent.runs':
            current=(await self.call('GET',path)).json()
            if current.get('connection_revision')!=self.legacy.state.agent.connection._read().get('revision'):raise LedgerError('Agent 连接版本已变化，请从工作台发起新的任务以使用新连接',409)
            return 'POST',path+'/resume',{}
        payload=dict(command.get('payload',{}))
        if namespace=='data.jobs':
            session_path=command['path'].rsplit('/',1)[0]
            current=(await self.call('GET',session_path)).json()
            if current['version']!=payload.get('version'):raise LedgerError('Data 作业版本已变化，请从工作台基于新版本发起任务',409)
        return command['method'],command['path'],payload

    def reuse_successful_steps(self,dispatch,original_run):
        with self.engine.begin() as db:
            rows=db.execute(select(s.task_step_runs).where(s.task_step_runs.c.run_id==original_run,s.task_step_runs.c.status=='succeeded')).mappings().all()
            for row in rows:
                values={name:row[name] for name in ('tool_id','native_id','input_id')}
                db.execute(pg_insert(s.task_step_runs).values(id='reuse'+digest([dispatch['run_id'],row['id']])[:32],task_id=dispatch['task_id'],run_id=dispatch['run_id'],step_key='reuse:'+row['id'],attempt=1,action='reuse',status='succeeded',reused_from_step_run_id=row['id'],details={'reused':True,'compute_performed':False},**values).on_conflict_do_nothing(index_elements=['id']))

    def record_native_import(self,dispatch,command,body):
        from .native_inputs import resolve
        native=command['native'];inputs=[]
        if native['application']=='ocr':
            for document in body.get('documents',[]):
                if isinstance(document,dict) and document.get('id'):inputs.extend(resolve(self.root,'ocr',{},'/api/documents/'+document['id']))
            versions=[image.get('active_version') for image in body.get('images',[]) if isinstance(image,dict) and image.get('active_version')]
            if versions:inputs.extend(resolve(self.root,'ocr',{'version_ids':versions},''))
        else:inputs=resolve(self.root,'classification',{'asset_ids':body.get('asset_ids',[])},'')
        with self.engine.begin() as db:
            for item in inputs:
                existing=db.execute(select(s.task_inputs.c.id,s.task_inputs.c.source_locator).where(s.task_inputs.c.task_id==dispatch['task_id'],s.task_inputs.c.blob_hash==item['blob_hash'],s.task_inputs.c.name==item['name'],func.coalesce(s.task_inputs.c.source_locator['original_upload'].as_boolean(),False).is_(False)).limit(1)).first()
                if existing:db.execute(update(s.task_inputs).where(s.task_inputs.c.id==existing.id).values(source_locator={**existing.source_locator,**item['source_locator'],'original_upload':True}))
                else:db.execute(pg_insert(s.task_inputs).values(id='input'+digest([dispatch['task_id'],item['asset_id']])[:32],task_id=dispatch['task_id'],**item).on_conflict_do_nothing())
                identifier='import'+digest([dispatch['run_id'],item['asset_id']])[:32]
                db.execute(pg_insert(s.task_step_runs).values(id=identifier,task_id=dispatch['task_id'],run_id=dispatch['run_id'],step_key='import:'+item['asset_id'],attempt=1,tool_id=native['application'],action='import',status='succeeded',native_id=item['asset_id'],details={'name':item['name'],'blob_hash':item['blob_hash']}).on_conflict_do_nothing(index_elements=['id']))
            for number,error in enumerate(body.get('errors',[])):
                db.execute(pg_insert(s.task_step_runs).values(id='import'+digest([dispatch['run_id'],'error',number])[:32],task_id=dispatch['task_id'],run_id=dispatch['run_id'],step_key='import-error:'+str(number),attempt=1,tool_id=native['application'],action='import',status='failed',error=str(error.get('message') or error.get('error'))[:4000],details={'name':error.get('name')}).on_conflict_do_nothing(index_elements=['id']))
            rows=db.execute(select(s.task_inputs).where(s.task_inputs.c.task_id==dispatch['task_id'])).mappings().all()
            total=sum(row['size_bytes'] for row in rows) if rows and all(row['size_bytes'] is not None for row in rows) else None
            db.execute(update(s.tasks).where(s.tasks.c.id==dispatch['task_id']).values(file_count=len(rows) or None,input_total_bytes=total))

    async def retry_ocr(self,dispatch,value):
        from .native import perform_native
        with closing(sqlite3.connect(self.root/'jobs.sqlite3')) as db:
            db.row_factory=sqlite3.Row
            projection=dict(db.execute('SELECT * FROM projected_jobs WHERE platform_job_id=?',(value['binding'][1],)).fetchone())
        native={'workspace_id':projection['workspace_id'],'application':'ocr','link_id':projection['link_id'],'method':'POST',
            'path':'api/platform/v2/task-retries','trusted_task':True,'query':'','content_type':'application/json',
            'content_hex':json.dumps({'project_id':projection['module_project_id'],'task_ids':json.loads(projection['native_ids']),'request_id':dispatch['run_id']}).encode().hex()}
        result=await perform_native(self.legacy,self.root,native)
        from .files import response_bytes
        body=json.loads(response_bytes(self.root,result))
        if result['status']>=400:raise LedgerError(body.get('detail') or body.get('message') or '原生重试未通过验证',409)
        ref=await asyncio.to_thread(self.legacy.state.job_service.register,projection['workspace_id'],projection['link_id'],'ocr',body['task_ids'])
        with self.engine.begin() as db:
            for native_id in body['reused_task_ids']:
                previous=db.execute(select(s.task_step_runs).where(s.task_step_runs.c.run_id==value['original_run_id'],s.task_step_runs.c.native_id==native_id,s.task_step_runs.c.status=='succeeded').order_by(s.task_step_runs.c.attempt.desc()).limit(1)).mappings().first()
                db.execute(pg_insert(s.task_step_runs).values(id='reuse'+digest([dispatch['run_id'],native_id])[:32],task_id=dispatch['task_id'],run_id=dispatch['run_id'],step_key='reuse:'+native_id,
                    attempt=1,tool_id='ocr',action='reuse',status='succeeded',native_id=native_id,reused_from_step_run_id=previous['id'] if previous else None,details={'reused':True,'compute_performed':False}).on_conflict_do_nothing(index_elements=['id']))
            db.execute(update(s.task_runs).where(s.task_runs.c.id==dispatch['run_id']).values(scope={'failed_only':True,'reused_native_ids':body['reused_task_ids'],'new_native_ids':body['task_ids']}))
        return ref,body['reused_task_ids']

    def native_generation(self,namespace,identifier,generation):
        sources={**journal.SOURCES,'jobs.sqlite3':[*journal.SOURCES.get('jobs.sqlite3',[]),('projection',None,None,None)]}
        for relative,definitions in sources.items():
            if any(item[0]==namespace for item in definitions):
                path=self.root/relative
                if path.is_file():
                    with closing(sqlite3.connect(path)) as db,db:
                        db.execute('CREATE TABLE IF NOT EXISTS central_task_generations(namespace TEXT,source_id TEXT,generation INTEGER,PRIMARY KEY(namespace,source_id))')
                        db.execute('INSERT INTO central_task_generations VALUES(?,?,?) ON CONFLICT(namespace,source_id) DO UPDATE SET generation=excluded.generation',(namespace,identifier,generation))

    def current_native_generation(self,namespace,identifier):
        sources={**journal.SOURCES,'jobs.sqlite3':[*journal.SOURCES.get('jobs.sqlite3',[]),('projection',None,None,None)]}
        for relative,definitions in sources.items():
            if any(item[0]==namespace for item in definitions):
                path=self.root/relative
                if path.is_file():
                    with closing(sqlite3.connect(path)) as db:
                        row=db.execute('SELECT generation FROM central_task_generations WHERE namespace=? AND source_id=?',(namespace,identifier)).fetchone()
                        if row:return row[0]
        return 1

    async def control(self,dispatch):
        with self.engine.begin() as db:
            current=db.execute(select(s.tasks.c.current_run_id).where(s.tasks.c.id==dispatch['task_id'])).scalar_one()
            if current!=dispatch['run_id']:
                db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id']).values(state='obsolete',updated_at=now()))
                self.ledger._event(db,dispatch['task_id'],dispatch['run_id'],'control_superseded',{'action':dispatch['command'].get('action')});return
        if dispatch['command'].get('actor_id'):
            with self.engine.connect() as db:
                actor=db.execute(select(s.users).where(s.users.c.id==dispatch['command']['actor_id'],s.users.c.disabled.is_(False))).mappings().first()
                if not actor:raise LedgerError('控制请求账号已停用',403)
                self.ledger._task(db,actor,dispatch['task_id'],control=True)
        with self.engine.connect() as db:original=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.run_id==dispatch['run_id'],s.task_dispatches.c.kind=='execute')).mappings().one()
        binding=original['receipt'].get('binding');action=dispatch['command']['action']
        if action=='steer':
            response=await self.call('POST',dispatch['command']['path'],dispatch['command']['payload'],dispatch['id'])
            with self.engine.begin() as db:
                db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id'],s.task_dispatches.c.lease_token==dispatch['lease_token']).values(state='done',receipt={'accepted':True,'http_status':response.status_code,'body':response.json()},updated_at=now()))
                self.ledger._event(db,dispatch['task_id'],dispatch['run_id'],'steering_delivered',{'accepted':response.is_success})
            return
        if action=='verify':
            await self.verify_execution(dispatch,dict(original));return
        if not binding:raise LedgerError('原生任务尚未返回可控制的身份',409)
        namespace,identifier,path=binding
        if namespace=='manual.manual_analysis':target=path+'/control/'+action;payload={}
        elif namespace=='projection':target=path+'/actions/'+action;payload={}
        elif namespace=='data.jobs':target=path+'/'+action;payload={}
        elif namespace=='batches.batches':target=path+'/control';payload={'operation':'retry' if action=='resume' else action}
        elif namespace=='agent.runs':target=path+'/'+action;payload={'schema_version':'1.0','cancel_owned_jobs':True} if action=='cancel' else {}
        else:raise LedgerError('此工具不支持该控制操作',409)
        response=await self.call('POST',target,payload,dispatch['id'])
        with self.engine.begin() as db:
            db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id'],s.task_dispatches.c.lease_token==dispatch['lease_token']).values(state='done',receipt={'http_status':response.status_code,'accepted':response.is_success},updated_at=now()))
            self.ledger._event(db,dispatch['task_id'],dispatch['run_id'],'control_delivered',{'action':action,'accepted':response.status_code<400})
        if not response.is_success and action=='resume':
            body=await self.native_snapshot(binding)
            self.ledger.observe(dict(original),normalized(body),identity=dispatch['id']+':resume-rejected',observation=scoped_observation(body,binding[0]),terminal=normalized(body) in TERMINAL)

    def install_native_hooks(self):
        data=getattr(self.legacy.state,'data_workbench',None)
        if not data:return
        original=data.resume_operation
        def resume(request_key):
            with closing(data.db()) as native:
                row=native.execute('SELECT j.id,j.status FROM operation_requests r JOIN jobs j ON j.id=r.job_id WHERE r.request_key=?',(request_key,)).fetchone()
            if row and row['status'] in {'failed','interrupted'}:self.track_child_retry('data.jobs',row['id'])
            return original(request_key)
        data.resume_operation=resume

    def track_child_retry(self,namespace,identifier):
        with self.engine.begin() as db:
            source=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace==namespace,s.task_source_links.c.source_id==identifier).order_by(s.task_source_links.c.source_generation.desc()).limit(1)).mappings().first()
            if not source:return
            task=db.execute(select(s.tasks).where(s.tasks.c.id==source['task_id']).with_for_update()).mappings().one()
            if not task['parent_task_id'] or task['status'] not in {'failed','needs_verification','partial'}:return
            prior=db.execute(select(s.task_runs).where(s.task_runs.c.id==task['current_run_id'])).mappings().one()
            parent_run=db.execute(select(s.tasks.c.current_run_id).where(s.tasks.c.id==task['parent_task_id'])).scalar_one()
            run=key('r');generation=source['source_generation']+1
            requester=db.execute(select(s.task_runs.c.requested_by).where(s.task_runs.c.id==parent_run)).scalar_one()
            db.execute(insert(s.task_runs).values(id=run,task_id=task['id'],attempt=task['run_count']+1,generation=prior['generation']+1,status='queued',requested_by=requester,request_snapshot=prior['request_snapshot'],scope={'parent_run_id':parent_run,'native_retry':True}))
            db.execute(insert(s.task_source_links).values(id=key('src'),task_id=task['id'],run_id=run,namespace=namespace,source_id=identifier,source_generation=generation,metadata={'parent_run_id':parent_run}))
            db.execute(update(s.tasks).where(s.tasks.c.id==task['id']).values(current_run_id=run,run_count=task['run_count']+1,status='queued',parent_run_id=parent_run,revision=task['revision']+1,updated_at=now()))
            self.ledger._event(db,task['id'],run,'native_retry_started',{'source_id':identifier,'parent_run_id':parent_run})
        self.native_generation(namespace,identifier,generation)

    async def verify_execution(self,control,original):
        record=self.command_receipt(original['id']);binding=original['receipt'].get('binding') or (record[1].get('binding') if record else None)
        if record and not binding and (record[0]=='sending' or record[1].get('http_status',0)>=500):
            from .recover import binding as recover_binding
            binding=await recover_binding(self,original)
            if binding:
                from datetime import timedelta
                body=await self.native_snapshot(binding)
                receipt={'accepted':True,'http_status':202,'body':body,'binding':binding,'recovered_from_native_journal':True}
                if binding[0]=='native.imports':
                    from .files import envelope
                    from .private_payloads import unseal
                    self.record_native_import(original,unseal(self.root,dict(original['command'])),body)
                    receipt['native_response']=envelope(self.root,200,{'content-type':'application/json'},json.dumps(body,ensure_ascii=False).encode())
                self.save_receipt(original['id'],'accepted',receipt)
                original={**original,'receipt':receipt,'worker_id':self.worker_id,'lease_token':key('lease')}
                with self.engine.begin() as db:db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==original['id']).values(state='observing',worker_id=self.worker_id,lease_token=original['lease_token'],receipt=receipt,lease_until=now()+timedelta(seconds=60),updated_at=now()))
                if binding[0]=='ocr.document_stages':
                    for identifier in binding[2]:self.bind(original,'ocr.document_stages',identifier,generation=self.current_native_generation('ocr.document_stages',identifier))
                else:self.bind(original,*binding[:2],generation=body.get('generation') or self.current_native_generation(*binding[:2]))
                self.expand_projection(original,binding)
                self.ledger.observe(original,normalized(body),identity='recovered:'+control['id'],observation=scoped_observation(body,binding[0]),receipt=receipt,terminal=normalized(body) in TERMINAL)
                if normalized(body) not in TERMINAL|{'needs_verification'}:self.active[original['id']]=asyncio.create_task(self.execute(original))
        verified=False;body=None
        if record is None:
            # The intent is persisted before any native call. Its absence proves
            # this workspace's execution host never dispatched that command.
            verified=True
        elif binding:
            body=await self.native_snapshot(binding)
            if body:
                status=normalized(body)
                if status in TERMINAL:
                    self.ledger.observe(original,status,identity='verify:'+control['id'],observation=observation(body),terminal=True,receipt=original['receipt'])
                    verified=True
                elif status=='needs_verification' and binding[0] in {'data.jobs','manual.manual_analysis','forensics.inspections'}:
                    service={'data.jobs':self.legacy.state.data_workbench,'manual.manual_analysis':getattr(self.legacy.state,'multimodal_service',None),
                        'forensics.inspections':getattr(self.legacy.state,'forensic_service',None)}[binding[0]]
                    processes=getattr(service,'processes',{}) if service else {binding[1]:True}
                    coordinator=getattr(service,'coordinator',None)
                    gpu_owner=coordinator.snapshot().owner_request_id if coordinator else None
                    verified=binding[1] not in processes and not gpu_owner
        with self.engine.begin() as db:
            if verified:
                task=db.execute(select(s.tasks).where(s.tasks.c.id==control['task_id']).with_for_update()).mappings().one()
                if task['current_run_id']==control['run_id']:
                    db.execute(update(s.tasks).where(s.tasks.c.id==task['id']).values(summary={**task['summary'],'retry_blocked':False,'control_verification_required':False},revision=task['revision']+1,updated_at=now()))
                    db.execute(update(s.task_dispatches).where(s.task_dispatches.c.task_id==task['id'],s.task_dispatches.c.run_id==control['run_id'],s.task_dispatches.c.kind=='control',s.task_dispatches.c.state=='needs_verification').values(state='verified_obsolete',updated_at=now()))
                    db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==original['id'],s.task_dispatches.c.state!='done').values(state='verified_interrupted'))
            db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==control['id']).values(state='done',receipt={'verified':verified,'native_status':body.get('status') if body else None},updated_at=now()))
            self.ledger._event(db,control['task_id'],control['run_id'],'verification_completed',{'verified':verified,'retry_allowed':verified})

    async def publish(self,paths):
        for path in paths:
            events=journal.pending(path,after=self.publish_offsets.get(path,0))
            if not events:
                self.publish_offsets[path]=0;events=journal.pending(path)
            for event in events:
                from .maintenance import shared_write_guard
                try:
                    with shared_write_guard(self.engine):
                        if self.publish_one(path,event):journal.acknowledge(path,event['seq'])
                except (ValueError,KeyError,TypeError,LedgerError) as error:
                    record={'failed_at':wire(now()),'source':path.relative_to(self.root).as_posix(),'seq':event['seq'],'error_type':type(error).__name__}
                    failures=getattr(self,'sync_error_times',{});identity=(str(path),event['seq']);stamp=asyncio.get_running_loop().time()
                    if stamp-failures.get(identity,-300)>=300:
                        (self.root/'central-event-sync-error.json').write_text(json.dumps(record),'utf-8')
                        logging.getLogger(__name__).error('Native event retained for repair: %s #%s (%s)',record['source'],event['seq'],type(error).__name__)
                        if len(failures)>=1000:failures.pop(next(iter(failures)))
                        failures[identity]=stamp;self.sync_error_times=failures
                self.publish_offsets[path]=event['seq']

    def publish_one(self,path,event):
        payload=json.loads(event['payload']);namespace=event['namespace']
        # Child rows refer to their parent native job/run identity.
        source_namespace={'classification.items':'classification.jobs','agent.operations':'agent.runs','agent.model_requests':'agent.runs'}.get(namespace,namespace)
        identity=f"native:{path.relative_to(self.root).as_posix()}:{event['seq']}"
        with self.engine.begin() as db:
            link=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace==source_namespace,s.task_source_links.c.source_id==event['source_id'],s.task_source_links.c.source_generation==event['generation'])).mappings().first()
            if not link:return False
            if db.execute(select(s.task_events.c.seq).where(s.task_events.c.identity==identity)).first():return True
            self.ledger._event(db,link['task_id'],link['run_id'],'native_snapshot',{'namespace':namespace,'source_id':event['source_id'],'snapshot':native_event_summary(payload)},identity=identity)
            self.record_step(db,link,namespace,payload,event)
            if namespace=='agent.model_requests':self.record_ai(db,link,payload,identity,event['created_at'])
            elif namespace in {'manual.manual_analysis','ocr.tasks','classification.jobs'}:self.record_local_ai(db,link,payload,event)
            if namespace in {'agent.operations','batches.batches'}:self.adopt_related(db,link,payload)
            task=db.execute(select(s.tasks).where(s.tasks.c.id==link['task_id'])).mappings().one()
            if task['parent_task_id'] and namespace in {'data.jobs','classification.jobs','ocr.tasks','manual.manual_analysis','forensics.inspections'}:
                status=normalized(payload);stamp=as_date(event['created_at']) or now()
                updates={'status':status,'last_verified_at':stamp,'updated_at':stamp,'revision':task['revision']+1}
                db.execute(update(s.tasks).where(s.tasks.c.id==task['id'],s.tasks.c.current_run_id==link['run_id']).values(**updates))
                run=db.execute(select(s.task_runs).where(s.task_runs.c.id==link['run_id'])).mappings().one()
                run_updates={'status':status}
                if status=='running' and not run['started_at']:run_updates['started_at']=stamp
                if status in TERMINAL:
                    run_updates.update(finished_at=stamp,error=payload.get('error'),elapsed_ms=int((stamp-run['queued_at']).total_seconds()*1000) if run['queued_at'] else None)
                db.execute(update(s.task_runs).where(s.task_runs.c.id==link['run_id']).values(**run_updates))
            self.collect_file_refs(db,link,payload)
        return True

    def adopt_related(self,db,parent,payload):
        candidates=set()
        def walk(value):
            if isinstance(value,str) and value[:1] in {'{','['}:
                try:walk(json.loads(value))
                except ValueError:pass
            elif isinstance(value,dict):
                if value.get('session_id') and value.get('job_id'):candidates.add(('data.jobs',value['job_id']))
                if value.get('platform_job_id'):candidates.add(('projection',value['platform_job_id']))
                for item in value.values():
                    if isinstance(item,(dict,list)):walk(item)
                if isinstance(value.get('result'),str):walk(value['result'])
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(payload)
        original=db.execute(select(s.tasks).where(s.tasks.c.id==parent['task_id'])).mappings().one()
        parent_run=db.execute(select(s.task_runs).where(s.task_runs.c.id==parent['run_id'])).mappings().one()
        for namespace,identifier in sorted(candidates):
            previous=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace==namespace,s.task_source_links.c.source_id==identifier).order_by(s.task_source_links.c.source_generation.desc()).limit(1)).mappings().first()
            if previous:continue
            relative='data-workbench/sessions.sqlite3' if namespace=='data.jobs' else 'jobs.sqlite3'
            try:
                with closing(sqlite3.connect(self.root/relative)) as native:
                    native.row_factory=sqlite3.Row
                    row=native.execute('SELECT * FROM jobs WHERE id=?' if namespace=='data.jobs' else 'SELECT * FROM projected_jobs WHERE platform_job_id=?',(identifier,)).fetchone()
                    if not row:continue
                    row=dict(row)
            except sqlite3.Error:continue
            native_body=row if namespace=='data.jobs' else json.loads(row['payload'])
            created=as_date(native_body.get('created') or native_body.get('created_at'))
            if created and parent_run['queued_at'] and created<parent_run['queued_at']:continue
            tool='data' if namespace=='data.jobs' else row['app_id'];status=normalized(native_body)
            child=key('t');run=key('r');number=db.execute(select(s.task_numbers.next_value())).scalar_one()
            local_stamp=now().astimezone(ZoneInfo('Asia/Hong_Kong'))
            db.execute(insert(s.tasks).values(id=child,task_code=f'TK-{local_stamp:%Y%m%d}-{number:08d}',workspace_id=original['workspace_id'],project_name_snapshot=original['project_name_snapshot'],
                created_by=original['created_by'],creator_name_snapshot=original['creator_name_snapshot'],name=f"{tool} · {native_body.get('action','子任务')}",tool_id=tool,action=native_body.get('action','execute'),
                source='agent' if original['tool_id']=='agent' else 'batch',parent_task_id=parent['task_id'],parent_run_id=parent['run_id'],status=status,current_run_id=run,
                idempotency_key='child:'+digest([parent['run_id'],namespace,identifier]),request_hash=digest([namespace,identifier]),created_at=created or as_date(payload.get('created_at')) or now(),summary={'replayable':False}))
            db.execute(insert(s.task_runs).values(id=run,task_id=child,attempt=1,generation=parent_run['generation'],status=status,requested_by=parent_run['requested_by'],queued_at=created or parent_run['queued_at'],request_snapshot={'source_namespace':namespace,'source_id':identifier,'requested_by_snapshot':parent_run['request_snapshot'].get('requested_by_snapshot')}))
            db.execute(insert(s.task_source_links).values(id=key('src'),task_id=child,run_id=run,namespace=namespace,source_id=identifier,source_generation=1,metadata={'parent_run_id':parent['run_id']}))
            self.ledger._event(db,child,run,'child_bound',{'source_id':identifier},identity='child:'+digest([parent['run_id'],namespace,identifier]))
            if namespace=='projection':
                native_namespace='ocr.tasks' if tool=='ocr' else 'classification.jobs'
                for native_id in json.loads(row['native_ids']):
                    db.execute(pg_insert(s.task_source_links).values(id=key('src'),task_id=child,run_id=run,namespace=native_namespace,source_id=native_id,source_generation=1,metadata={}).on_conflict_do_nothing(index_elements=['namespace','source_id','source_generation']))

    def record_step(self,db,link,namespace,payload,event):
        if namespace=='agent.model_requests':return
        logical=str(payload.get('id') or payload.get('job_id') or payload.get('inspection_id') or event['source_id'])
        step_key=(namespace+':'+logical)[:200]
        previous=db.execute(select(s.task_step_runs).where(s.task_step_runs.c.run_id==link['run_id'],s.task_step_runs.c.step_key==step_key).order_by(s.task_step_runs.c.attempt.desc()).limit(1)).mappings().first()
        state=normalized(payload)
        if namespace=='classification.items' and payload.get('state')=='pending':state='queued'
        stage=str(payload.get('stage',''))
        retry=stage.startswith('retry_') and state=='queued'
        started=as_date(payload.get('started_at') or payload.get('started'))
        finished=as_date(payload.get('finished_at') or payload.get('finished'))
        if not started:started=(previous['started_at'] if previous else None) or (as_date(event['created_at']) if state=='running' else None)
        if not finished and state in TERMINAL:finished=(previous['finished_at'] if previous and previous['status'] in TERMINAL else None) or as_date(event['created_at'])
        if previous and retry and previous['status']=='running':
            db.execute(update(s.task_step_runs).where(s.task_step_runs.c.id==previous['id']).values(status='failed',finished_at=as_date(event['created_at']),error=stage))
            return
        if previous and previous['status'] in TERMINAL and state not in TERMINAL:
            previous=None
        details={'namespace':namespace,'source_id':event['source_id'],'phase':payload.get('phase') or payload.get('stage'),
                 'actual_device':payload.get('actual_device'),'engine':payload.get('engine'),'engine_package':payload.get('engine_package')}
        values={'status':state,'error':str(payload.get('error'))[:4000] if payload.get('error') else None,'details':safe(details)}
        if started:values['started_at']=started
        if finished:values['finished_at']=finished
        if started and finished:values['elapsed_ms']=max(0,int((finished-started).total_seconds()*1000))
        if previous:
            db.execute(update(s.task_step_runs).where(s.task_step_runs.c.id==previous['id']).values(**values))
        else:
            attempt=db.execute(select(func.coalesce(func.max(s.task_step_runs.c.attempt),0)+1).where(s.task_step_runs.c.run_id==link['run_id'],s.task_step_runs.c.step_key==step_key)).scalar_one()
            db.execute(insert(s.task_step_runs).values(id=key('step'),task_id=link['task_id'],run_id=link['run_id'],step_key=step_key,attempt=attempt,
                tool_id=namespace.split('.')[0],action=str(payload.get('action') or payload.get('kind') or payload.get('tool') or 'execute')[:100],native_id=logical[:200],native_generation=event['generation'],worker_id=self.worker_id,**values))

    def record_ai(self,db,link,payload,identity,created_at=None):
        response=payload.get('response') or {}
        if isinstance(response,str):
            try:response=json.loads(response)
            except ValueError:response={}
        usage=response.get('usage') or {}
        request_snapshot=db.execute(select(s.task_runs.c.request_snapshot).where(s.task_runs.c.id==link['run_id'])).scalar_one()
        profile=request_snapshot.get('resolved_ai_service',{})
        identifier='ai'+digest([link['run_id'],payload.get('id')])[:32]
        values={'task_id':link['task_id'],'run_id':link['run_id'],'status':payload.get('state','unknown'),
            'actual_model':response.get('returned_model') or response.get('model'),'provider':profile.get('provider') or response.get('protocol'),
            'connection_name':profile.get('name'),'profile_id':profile.get('id'),'requested_model':profile.get('model'),
            'input_tokens':usage.get('input_tokens',usage.get('prompt_tokens')),
            'output_tokens':usage.get('output_tokens',usage.get('completion_tokens')),'metadata':{'native_request_id':payload.get('id')}}
        start=as_date(payload.get('created'));finish=as_date(created_at)
        if payload.get('state')=='received' and start and finish:values['elapsed_ms']=max(0,int((finish-start).total_seconds()*1000))
        db.execute(pg_insert(s.ai_calls).values(id=identifier,**values).on_conflict_do_update(index_elements=['id'],set_=values))
        self.model_summary(db,link['task_id'])

    def model_summary(self,db,task_id):
        descendants=select(s.tasks.c.id).where(s.tasks.c.id==task_id).cte(recursive=True)
        descendants=descendants.union_all(select(s.tasks.c.id).join(descendants,s.tasks.c.parent_task_id==descendants.c.id))
        services=db.execute(select(s.ai_calls.c.connection_name,s.ai_calls.c.actual_model,s.ai_calls.c.requested_model,s.ai_calls.c.provider).where(s.ai_calls.c.task_id.in_(select(descendants.c.id))).distinct()).all()
        row=db.execute(select(s.tasks.c.summary).where(s.tasks.c.id==task_id)).scalar_one()
        names=sorted({f'{name} / {actual or requested}' if name and (actual or requested) and name!=(actual or requested) else str(name or actual or requested or provider)
            for name,actual,requested,provider in services if name or actual or requested or provider})
        db.execute(update(s.tasks).where(s.tasks.c.id==task_id).values(summary={**row,'ai_services':names}))
        parent=db.execute(select(s.tasks.c.parent_task_id).where(s.tasks.c.id==task_id)).scalar_one()
        if parent:self.model_summary(db,parent)

    def record_local_ai(self,db,link,payload,event):
        result=payload.get('result') or {}
        if isinstance(result,str):
            try:result=json.loads(result)
            except ValueError:result={}
        if not isinstance(result,dict):result={}
        model=result.get('algorithm') or payload.get('engine')
        ai_metadata={}
        if event['namespace']=='ocr.tasks' and payload.get('kind')=='multimodal':
            import zlib
            with closing(sqlite3.connect(self.root/'native/ocr/workbench.sqlite3')) as native:
                row=native.execute('SELECT snapshot,raw_response FROM multimodal_requests WHERE task_id=?',(payload['id'],)).fetchone()
            def decoded(value):
                if isinstance(value,bytes) and value.startswith(b'OCRZ1\0'):value=zlib.decompress(value[6:])
                return json.loads(value) if value else {}
            if row:
                snapshot=decoded(row[0]);response=decoded(row[1]);config=snapshot.get('config',{});usage=response.get('usage',{})
                model=snapshot.get('model_id') or model
                ai_metadata={'requested_model':config.get('model') or model,'actual_model':response.get('returned_model') or response.get('model'),
                    'connection_name':config.get('name') or model,'provider':config.get('protocol') or ('external' if config.get('runtime')=='external' else 'local'),
                    'input_tokens':usage.get('input_tokens',usage.get('prompt_tokens')),'output_tokens':usage.get('output_tokens',usage.get('completion_tokens'))}
        if not model:return
        step_row=db.execute(select(s.task_step_runs.c.id,s.task_step_runs.c.status).where(s.task_step_runs.c.run_id==link['run_id'],s.task_step_runs.c.native_id==str(payload.get('id') or payload.get('job_id') or event['source_id'])).order_by(s.task_step_runs.c.attempt.desc()).limit(1)).first()
        step=step_row.id if step_row else None
        identifier='ai'+digest([link['run_id'],event['namespace'],event['source_id'],event['generation'],model,step])[:32]
        state=normalized(payload)
        if state=='queued':
            if step_row and step_row.status=='failed':state='failed'
            else:return
        values={'task_id':link['task_id'],'run_id':link['run_id'],'step_run_id':step,'provider':'local','connection_name':'本地模型',
            'actual_model':str(model),'status':state,'metadata':{'model_hashes':result.get('model_hashes',{}),'actual_device':payload.get('actual_device')}}
        values.update(ai_metadata)
        if result.get('elapsed_seconds') is not None:values['elapsed_ms']=int(result['elapsed_seconds']*1000)
        db.execute(pg_insert(s.ai_calls).values(id=identifier,**values).on_conflict_do_update(index_elements=['id'],set_=values))
        self.model_summary(db,link['task_id'])

    def collect_file_refs(self,db,link,payload):
        result=payload.get('result') or {}
        if isinstance(result,str):
            try:result=json.loads(result)
            except ValueError:result={}
        if not isinstance(result,dict):result={}
        items=list(payload.get('files',[])) if isinstance(payload.get('files'),list) else []
        if isinstance(result.get('files'),list):items.extend(result['files'])
        if payload.get('kind')=='render' and payload.get('status')=='succeeded' and payload.get('version_id'):
            with closing(sqlite3.connect(self.root/'native/ocr/workbench.sqlite3')) as native:
                version=native.execute('SELECT path,sha256 FROM versions WHERE id=?',(payload['version_id'],)).fetchone()
            if version:items.append({'name':Path(version[0]).name,'path':str(self.root/'native/ocr'/version[0]),'sha256':version[1]})
        if payload.get('result_id') and payload.get('status')=='succeeded' and payload.get('engine'):
            with closing(sqlite3.connect(self.root/'native/ocr/workbench.sqlite3')) as native:
                original=native.execute('SELECT original FROM results WHERE id=?',(payload['result_id'],)).fetchone()
            if original:
                import zlib
                raw=original[0]
                if isinstance(raw,bytes) and raw.startswith(b'OCRZ1\0'):raw=zlib.decompress(raw[6:])
                if isinstance(raw,str):raw=raw.encode('utf-8')
                target=self.root/'task-files'/link['task_id']/link['run_id']/('ocr-result-'+payload['id']+'.json');target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():target.write_bytes(raw)
                items.append({'name':target.name,'path':str(target)})
        if isinstance(result.get('output_files'),dict):
            for name,metadata in result['output_files'].items():
                items.append({**metadata,'name':name,'path':str(self.root/'manual-analysis'/payload['job_id']/f"attempt-{payload.get('generation',1)}"/name)})
        for item in items:
            if not isinstance(item,dict):continue
            name=item.get('name') or item.get('path')
            if not name:continue
            identifier='file'+digest([link['run_id'],name])[:32]
            source=item.get('path')
            if not source and payload.get('inspection_id'):
                with closing(sqlite3.connect(self.root/'forensics/forensics.sqlite3')) as native:
                    location=native.execute('SELECT path FROM output_locations WHERE inspection_id=?',(payload['inspection_id'],)).fetchone()
                if location:source=str(self.root/'forensics'/location[0]/Path(name).name)
            if item.get('relative') and payload.get('session_id'):source=str(self.root/'data-workbench/sessions'/payload['session_id']/item['relative'])
            source_path=None;blob_hash=item.get('sha256') or item.get('blob_hash');size=item.get('size_bytes',item.get('bytes'))
            if source:
                from .files import hash_file
                import shutil
                candidate=f_path(source)
                if not candidate.is_relative_to(self.root) or not candidate.is_file():continue
                actual=hash_file(candidate)
                if blob_hash and actual!=blob_hash:raise LedgerError('成果文件与原生摘要不一致',409)
                target=self.root/'task-files'/link['task_id']/link['run_id']/(identifier+'-'+Path(name).name)
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():shutil.copy2(candidate,target)
                if hash_file(target)!=actual:raise LedgerError('成果快照校验失败',409)
                source_path=str(target);blob_hash=actual;size=target.stat().st_size
            db.execute(pg_insert(s.task_artifact_links).values(id=identifier,task_id=link['task_id'],run_id=link['run_id'],name=Path(name).name,
                kind='output',blob_hash=blob_hash,size_bytes=size,source_path=source_path,download_route=item.get('download_url')).on_conflict_do_nothing(index_elements=['id']))

    def record_export(self,dispatch,body):
        if body.get('bundle_id') and isinstance(body.get('files'),list):
            from urllib.parse import quote
            with self.engine.begin() as db:
                workspace=db.execute(select(s.tasks.c.workspace_id).where(s.tasks.c.id==dispatch['task_id'])).scalar_one()
                for item in body['files']:
                    identifier='file'+digest([dispatch['run_id'],item['name'],item.get('blob_hash')])[:32]
                    db.execute(pg_insert(s.task_artifact_links).values(id=identifier,task_id=dispatch['task_id'],run_id=dispatch['run_id'],name=item['name'],kind='output',artifact_id=item.get('artifact_id'),blob_hash=item.get('blob_hash'),size_bytes=item.get('size_bytes'),
                        download_route=f'/api/platform/v1/workspaces/{workspace}/artifacts/{body["bundle_id"]}/files/{quote(item["name"],safe="")}').on_conflict_do_nothing(index_elements=['id']))
        if body.get('export_file'):
            target=f_path(body['export_file']);identifier=key('file')
            from .files import hash_file
            with self.engine.begin() as db:
                db.execute(insert(s.task_artifact_links).values(id=identifier,task_id=dispatch['task_id'],run_id=dispatch['run_id'],name=target.name,kind='output',
                    source_path=str(target),size_bytes=target.stat().st_size,blob_hash=hash_file(target)))

    async def close(self):
        self.closed.set()
        for task in self.active.values():task.cancel()
        await asyncio.gather(*self.active.values(),return_exceptions=True)
        from .schema_v3 import workers
        with self.engine.begin() as db:db.execute(update(workers).where(workers.c.id==self.worker_id).values(stopped_at=now()))

def create_worker_app(engine,initializer=None):
    token=os.environ.get('CLEAR_WORKER_TOKEN','')
    if len(token)<32:raise RuntimeError('Worker token required')
    root=f_path(os.environ['WORKBENCH_DATA_DIR']);root.mkdir(parents=True,exist_ok=True)
    os.environ['CLEAR_EXECUTION_MODE']='worker'
    concurrency=int(os.environ.get('CLEAR_TASK_CONCURRENCY','1'))
    if not 1<=concurrency<=4:raise RuntimeError('Heavy task concurrency must be between 1 and 4')
    from workbench_platform.app import app as legacy
    @asynccontextmanager
    async def lifespan(app):
        check_schema(engine)
        from scripts.candidate_bootstrap import _FileLease
        lease=_FileLease(root/'.central-worker.lock')
        if not lease.acquire():raise RuntimeError('An execution host already owns this workspace')
        legacy_lease=_FileLease(root/'.live-preview.lock')
        if not legacy_lease.acquire():
            lease.close();raise RuntimeError('The existing local platform still owns this data directory')
        try:
            from .directory import bind_directory
            bind_directory(engine,root)
            if initializer:initializer()
            async with legacy.router.lifespan_context(legacy):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=legacy),base_url='http://127.0.0.1:8890',timeout=180) as client:
                    host=ExecutionHost(engine,root,client);host.legacy=legacy;host.install_native_hooks();app.state.host=host;app.state.legacy=legacy
                    from .ai_services import capture_current
                    capture_current(engine,legacy,root,make_current=True)
                    loop=asyncio.create_task(host.loop())
                    try:yield
                    finally:await host.close();loop.cancel();await asyncio.gather(loop,return_exceptions=True)
        finally:legacy_lease.close();lease.close()
    app=FastAPI(title='Clear private execution host',lifespan=lifespan)
    @app.middleware('http')
    async def trusted_service(request,call_next):
        if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+token):
            return JSONResponse({'detail':'Service authentication required'},status_code=403)
        response=await call_next(request)
        if request.method=='PUT' and request.url.path=='/api/platform/v1/agent/connection' and response.status_code<400:
            from .ai_services import capture_current
            capture_current(engine,legacy,root,make_current=True)
        if request.method=='DELETE' and request.url.path=='/api/platform/v1/agent/connection' and response.status_code<400:
            with engine.begin() as db:db.execute(update(s.ai_service_profiles).values(enabled=False))
        return response
    @app.exception_handler(LedgerError)
    async def error(request,error):return JSONResponse({'detail':str(error)},status_code=error.status)
    @app.post('/internal/scope')
    async def resolve_scope(request:Request):
        value=await request.json();workspace=scope.resolve(root,value['path'],value.get('payload'),value.get('query',''))
        return {'workspace_id':workspace}
    @app.post('/internal/inputs')
    async def inputs(request:Request):
        value=await request.json();registry_=legacy.state.asset_library[0];rows=[]
        if value.get('agent_session_id'):
            agent=getattr(legacy.state,'agent',None)
            if not agent:raise LedgerError('Agent 执行环境未启用',503)
            session=agent.store.session(value['agent_session_id'])
            if session['workspace_id']!=value['workspace_id']:raise LedgerError('Agent 资料不属于当前项目',403)
            for identifier in session['selected_assets']:
                asset=registry_.get(value['workspace_id'],identifier)
                rows.append({'asset_id':asset.asset_id,'asset_revision':asset.revision,'blob_hash':asset.blob_hash,
                    'name':asset.original_name,'size_bytes':asset.size_bytes,'source_locator':{'agent_session_id':value['agent_session_id']}})
            return {'items':rows}
        if value.get('data_session_id'):
            session=legacy.state.data_workbench.get(value['data_session_id'])
            if session['workspace_id']!=value['workspace_id']:raise LedgerError('Data 资料不属于当前项目',403)
            for upload in session['uploads']:
                if value.get('upload_id') and upload['id']!=value['upload_id']:continue
                rows.append({'asset_id':upload.get('source_asset_id') or upload['id'],'asset_revision':upload.get('source_asset_revision') or 1,
                    'blob_hash':upload['sha256'],'name':upload['name'],'size_bytes':upload['size'],'source_locator':{'data_session_id':value['data_session_id'],'upload_id':upload['id']}})
            return {'items':rows}
        for item in value['inputs']:
            asset=registry_.get(value['workspace_id'],item['asset_id'])
            revision=item.get('asset_revision',item.get('revision'))
            if (asset.revision,asset.blob_hash)!=(revision,item.get('blob_hash')):raise LedgerError('资料版本已变化',409)
            rows.append({'asset_id':asset.asset_id,'asset_revision':asset.revision,'blob_hash':asset.blob_hash,'name':asset.original_name,'size_bytes':asset.size_bytes,'source_locator':item.get('source_locator',{})})
        return {'items':rows}
    @app.post('/internal/ai/default')
    async def ai_default():
        from .ai_services import capture_current
        profile=capture_current(engine,legacy,root,make_current=True)
        return {name:profile[name] for name in ('id','name','provider','model')} if profile else {}
    @app.post('/internal/native')
    async def native(request:Request):
        from .native import proxy_native
        return await proxy_native(engine,legacy,root,request)
    @app.post('/internal/shutdown')
    async def shutdown():
        callback=getattr(app.state,'shutdown_callback',None)
        if callback:callback()
        return {'shutting_down':True}
    @app.get('/internal/status')
    async def status():
        return {'worker_id':app.state.host.worker_id,'root_identity':digest(str(root)),'source_version':app.state.host.source_version,'database':engine.url.database,'database_port':engine.url.port or 5432}
    app.mount('/',legacy)
    return app
