"""Native upload acceptance journal, loaded by the private native launchers.

Only stdlib and the existing native FastAPI runtime are required. Acceptance
IDs commit with each original-file transaction, including partial uploads.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime,timezone
import hashlib
import json
import re
from fastapi.responses import JSONResponse,Response
from starlette.routing import Mount

_invocation=ContextVar('clear_native_import',default=None)


def install(module,application):
    original=module.create_app
    def factory(*args,**kwargs):
        app=original(*args,**kwargs);register(app,application);return app
    module.create_app=factory


def register(app,application):
    store=app.state.store;original=store.transaction if application=='ocr' else store.connect
    def context():return original() if application=='ocr' else original(write=True)
    with context() as db:
        db.execute('CREATE TABLE IF NOT EXISTS central_import_requests(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,state TEXT NOT NULL,response_status INTEGER,response_body TEXT,created_at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS central_import_acceptances(request_id TEXT NOT NULL,kind TEXT NOT NULL,native_id TEXT NOT NULL,project_id TEXT NOT NULL,PRIMARY KEY(request_id,kind,native_id))')
    tables=('images','documents') if application=='ocr' else ('assets',)
    @contextmanager
    def tracked(*args,**kwargs):
        invocation=_invocation.get()
        with original(*args,**kwargs) as db:
            track=invocation and (application=='ocr' or kwargs.get('write') or args and args[0])
            before={table:db.execute(f'SELECT COALESCE(MAX(rowid),0) FROM {table}').fetchone()[0] for table in tables} if track else {}
            yield db
            for table,maximum in before.items():
                for row in db.execute(f'SELECT id,project_id FROM {table} WHERE rowid>?',(maximum,)).fetchall():
                    db.execute('INSERT OR IGNORE INTO central_import_acceptances VALUES(?,?,?,?)',(invocation['id'],table,row['id'],row['project_id']))
    if application=='ocr':store.transaction=tracked
    else:store.connect=tracked
    active=set()
    @app.middleware('http')
    async def track_import(request,call_next):
        identifier=request.headers.get('idempotency-key')
        selected=request.method=='POST' and re.fullmatch(r'/api/(?:v1/)?projects/[A-Za-z0-9_-]+/(?:images|documents|uploads)',request.url.path)
        if not identifier or not selected:return await call_next(request)
        raw=await request.body();query=request.url.query
        signature=hashlib.sha256((request.url.path+('?' + query if query else '')).encode()+b'\0'+raw).hexdigest()
        with context() as db:
            previous=db.execute('SELECT payload_hash,state,response_status,response_body FROM central_import_requests WHERE request_id=?',(identifier,)).fetchone()
            if previous:
                if previous['payload_hash']!=signature:return JSONResponse({'detail':'导入请求键已绑定其他原件'},status_code=409)
                if previous['state']=='complete' and previous['response_body'] is not None:
                    return Response(previous['response_body'].encode(),status_code=previous['response_status'],media_type='application/json')
                return JSONResponse({'detail':'原导入尚未确认结束，请查询平台任务中心'},status_code=409)
            db.execute('INSERT INTO central_import_requests VALUES(?,?,?,NULL,NULL,?)',(identifier,signature,'started',datetime.now(timezone.utc).isoformat()))
        active.add(identifier);token=_invocation.set({'id':identifier})
        try:
            response=await call_next(request);body=b''.join([chunk async for chunk in response.body_iterator])
            with context() as db:db.execute("UPDATE central_import_requests SET state='complete',response_status=?,response_body=? WHERE request_id=?",(response.status_code,body.decode('utf-8'),identifier))
            headers={name:value for name,value in response.headers.items() if name.lower() not in {'content-length','content-encoding'}}
            return Response(body,status_code=response.status_code,headers=headers)
        except Exception:
            with context() as db:db.execute("UPDATE central_import_requests SET state='interrupted' WHERE request_id=?",(identifier,))
            raise
        finally:_invocation.reset(token);active.discard(identifier)
    @app.get('/api/platform/v2/import-requests/{identifier}')
    def status(identifier:str):
        with context() as db:
            row=db.execute('SELECT * FROM central_import_requests WHERE request_id=?',(identifier,)).fetchone()
            if not row:return JSONResponse({'detail':'导入接收记录不存在'},status_code=404)
            values=[dict(item) for item in db.execute('SELECT kind,native_id,project_id FROM central_import_acceptances WHERE request_id=?',(identifier,))]
            return {**dict(row),'active':identifier in active,'acceptances':values}
    route=app.router.routes.pop();index=next((i for i,item in enumerate(app.router.routes) if isinstance(item,Mount) and item.path==''),len(app.router.routes));app.router.routes.insert(index,route)
