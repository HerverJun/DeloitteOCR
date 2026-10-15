"""Loaded inside the existing OCR runtime by its private platform launcher.

Retry creates new native task IDs. Original results, review proposals and
terminal projections remain immutable. All clones commit in one SQLite txn.
This module deliberately depends only on the native runtime and stdlib.
"""
import copy
import hashlib
import json
import uuid
from datetime import datetime,timezone
from fastapi import HTTPException
from starlette.routing import Mount
from contextvars import ContextVar
import re

_stage_invocation=ContextVar('clear_stage_invocation',default=None)

def install(service):
    original=service.create_app
    def factory(*args,**kwargs):
        app=original(*args,**kwargs);register(app);return app
    service.create_app=factory

def register(app):
    store=app.state.store
    with store.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS central_retry_requests(request_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,payload_hash TEXT NOT NULL,receipt TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS central_stage_requests(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,stage_ids TEXT NOT NULL)')
    original_stages=store._enqueue_document_stages
    def tracked_stages(db,*args,**kwargs):
        invocation=_stage_invocation.get()
        if invocation:
            previous=db.execute('SELECT payload_hash,stage_ids FROM central_stage_requests WHERE request_id=?',(invocation['id'],)).fetchone()
            if previous:
                if previous['payload_hash']!=invocation['hash']:raise HTTPException(409,'处理请求键已绑定其他参数')
                return json.loads(previous['stage_ids'])
        result=original_stages(db,*args,**kwargs)
        if invocation:db.execute('INSERT INTO central_stage_requests VALUES(?,?,?)',(invocation['id'],invocation['hash'],json.dumps(result)))
        return result
    store._enqueue_document_stages=tracked_stages
    @app.middleware('http')
    async def stage_identity(request,call_next):
        identifier=request.headers.get('idempotency-key')
        selected=request.method=='POST' and re.fullmatch(r'/api/(?:(?:pages|documents)/[A-Za-z0-9_-]+/(?:render|process)|results/[A-Za-z0-9_-]+/structure/tool-retry)',request.url.path)
        token=None
        if selected and identifier:
            if not 1<=len(identifier)<=200:raise HTTPException(400,'处理请求键无效')
            signature=hashlib.sha256(request.url.path.encode()+b'\0'+await request.body()).hexdigest()
            token=_stage_invocation.set({'id':identifier,'hash':signature})
        try:return await call_next(request)
        finally:
            if token is not None:_stage_invocation.reset(token)
    @app.post('/api/platform/v2/task-retries')
    def retry(body:dict):
        if set(body)!={'project_id','task_ids','request_id'} or not isinstance(body['task_ids'],list) or not 1<=len(body['task_ids'])<=16000:
            raise HTTPException(400,'重试选择无效')
        if not isinstance(body['request_id'],str) or not 8<=len(body['request_id'])<=120 or len(set(body['task_ids']))!=len(body['task_ids']):
            raise HTTPException(400,'重试请求标识无效')
        signature=hashlib.sha256(json.dumps({'project_id':body['project_id'],'task_ids':sorted(body['task_ids'])},sort_keys=True).encode()).hexdigest()
        with store.transaction() as db:
            previous=db.execute('SELECT payload_hash,receipt FROM central_retry_requests WHERE request_id=?',(body['request_id'],)).fetchone()
            if previous:
                if previous['payload_hash']!=signature:raise HTTPException(409,'重试标识已绑定其他输入')
                return json.loads(previous['receipt'])
            selected=[]
            for identifier in body['task_ids']:
                row=db.execute('SELECT * FROM tasks WHERE id=? AND project_id=?',(identifier,body['project_id'])).fetchone()
                if not row:raise HTTPException(403,'原任务不属于当前项目')
                selected.append(dict(row))
        failed=[row for row in selected if row['status'] in {'failed','cancelled','interrupted'}]
        if not failed:raise HTTPException(409,'没有可重试的失败项')
        services=app.state.application_services
        if any(row['kind'] not in {'fusion','multimodal'} for row in failed):services.require_recognition()
        packages={name:info['package_id'] for name,info in services.registry.engines().items()}
        for row in failed:
            if row['kind']=='ocr' and packages.get(row['engine'])!=row['engine_package']:raise HTTPException(409,'引擎包版本已变化，请重新发起识别')
            if row['kind']=='multimodal':
                from ocr_workbench.multimodal_store import prepare_review
                snapshot=prepare_review(store,row['id'],require_running=False)
                services.visual_parameters(snapshot['model_id'])
        created=[];stamp=datetime.now(timezone.utc).isoformat()
        with store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT payload_hash,receipt FROM central_retry_requests WHERE request_id=?',(body['request_id'],)).fetchone()
            if previous:
                if previous['payload_hash']!=signature:raise HTTPException(409,'重试标识冲突')
                return json.loads(previous['receipt'])
            for old in failed:
                current=db.execute('SELECT status FROM tasks WHERE id=?',(old['id'],)).fetchone()
                if current['status']!=old['status']:raise HTTPException(409,'原任务状态已变化')
                identifier=uuid.uuid4().hex
                clone={**old,'id':identifier,'status':'queued','phase':'等待重试','created':stamp,'started':None,'finished':None,'error':None,'result_id':None,'batch':stamp+'-'+body['request_id']}
                if old['kind']=='ocr' and old.get('input_version_id'):clone['version_id']=old['input_version_id']
                columns=list(clone)
                db.execute('INSERT INTO tasks('+','.join('"'+name+'"' for name in columns)+') VALUES('+','.join('?' for _ in columns)+')',[clone[name] for name in columns])
                if old['kind']=='multimodal':
                    from ocr_workbench.store import history_decoded,history_encoded
                    record=dict(db.execute('SELECT * FROM multimodal_requests WHERE task_id=?',(old['id'],)).fetchone())
                    snapshot=json.loads(history_decoded(record['snapshot']));snapshot['task_id']=identifier
                    record.update(task_id=identifier,request_id=body['request_id']+'-'+str(len(created)),snapshot=history_encoded(snapshot),raw_response=None,created=stamp)
                    names=list(record)
                    db.execute('INSERT INTO multimodal_requests('+','.join('"'+name+'"' for name in names)+') VALUES('+','.join('?' for _ in names)+')',[record[name] for name in names])
                elif old['kind']=='fusion':
                    row=db.execute('SELECT snapshot,fingerprint FROM fusion_inputs WHERE task_id=?',(old['id'],)).fetchone()
                    if row:db.execute('INSERT INTO fusion_inputs VALUES(?,?,?)',(identifier,row['snapshot'],row['fingerprint']))
                    for dependency in db.execute('SELECT parent_task_id FROM fusion_dependencies WHERE task_id=?',(old['id'],)).fetchall():
                        db.execute('INSERT INTO fusion_dependencies VALUES(?,?)',(identifier,dependency['parent_task_id']))
                elif old['kind']=='geometry':
                    record=dict(db.execute('SELECT * FROM geometry_requests WHERE task_id=?',(old['id'],)).fetchone())
                    record['task_id']=identifier
                    names=list(record)
                    db.execute('INSERT INTO geometry_requests('+','.join('"'+name+'"' for name in names)+') VALUES('+','.join('?' for _ in names)+')',[record[name] for name in names])
                created.append(identifier)
            receipt={'task_ids':created,'reused_task_ids':[row['id'] for row in selected if row['status']=='succeeded']}
            db.execute('INSERT INTO central_retry_requests VALUES(?,?,?,?)',(body['request_id'],body['project_id'],signature,json.dumps(receipt)))
        for queue in (app.state.queue,app.state.fusion_queue,app.state.external_queue):queue.wake.set()
        return receipt
    route=app.router.routes.pop()
    index=next((n for n,item in enumerate(app.router.routes) if isinstance(item,Mount) and item.path==''),len(app.router.routes))
    app.router.routes.insert(index,route)

    @app.post('/api/platform/v2/document-stage-retries')
    def retry_stages(body:dict):
        if set(body)!={'stage_ids','request_id'} or not isinstance(body['stage_ids'],list) or not 1<=len(body['stage_ids'])<=16000:raise HTTPException(400,'页面阶段重试选择无效')
        signature=hashlib.sha256(json.dumps(body['stage_ids'],sort_keys=True).encode()).hexdigest()
        with store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT payload_hash,receipt FROM central_retry_requests WHERE request_id=?',(body['request_id'],)).fetchone()
            if previous:
                if previous['payload_hash']!=signature:raise HTTPException(409,'重试标识冲突')
                return json.loads(previous['receipt'])
            created=[];reused=[]
            for identifier in body['stage_ids']:
                row=db.execute('SELECT * FROM document_stages WHERE id=?',(identifier,)).fetchone()
                if not row:raise HTTPException(404,'页面阶段不存在')
                if row['status']=='succeeded':reused.append(identifier);continue
                if row['status'] not in {'failed','cancelled','interrupted'}:raise HTTPException(409,'页面阶段尚未核实结束')
                clone=dict(row);clone.update(id=uuid.uuid4().hex,request_key=body['request_id']+'-'+str(len(created)),status='queued',phase='等待重试',attempt=0,error=None,output=None,created=datetime.now(timezone.utc).isoformat(),started=None,finished=None)
                names=list(clone)
                db.execute('INSERT INTO document_stages('+','.join('"'+name+'"' for name in names)+') VALUES('+','.join('?' for _ in names)+')',[clone[name] for name in names]);created.append(clone['id'])
            if not created:raise HTTPException(409,'没有可重试的页面阶段')
            receipt={'stage_ids':created,'reused_stage_ids':reused}
            db.execute('INSERT INTO central_retry_requests VALUES(?,?,?,?)',(body['request_id'],'document-stages',signature,json.dumps(receipt)))
        app.state.documents.wake.set()
        return receipt
    route=app.router.routes.pop();app.router.routes.insert(index,route)
