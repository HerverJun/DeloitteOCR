"""Isolated million-task/attempt API benchmark; never seeds a business database."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import secrets
import statistics
import time
from sqlalchemy import create_engine,text,insert,select
from .database import migrate
from .store import Ledger,token_hash,now
from . import schema_v1 as s

def run(source_engine,root,count=1_000_000):
    if not 1<=count<=2_000_000:raise ValueError('Benchmark count must be between 1 and 2,000,000')
    root.mkdir(parents=True,exist_ok=True)
    import shutil
    if shutil.disk_usage(root).free<4*1024**3:raise RuntimeError('Benchmark requires at least 4 GiB free on the configured local disk')
    database='clear_benchmark_'+secrets.token_hex(6)
    admin=create_engine(source_engine.url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    with admin.connect() as db:db.execute(text(f'CREATE DATABASE "{database}"'))
    engine=create_engine(source_engine.url.set(database=database),pool_size=25,max_overflow=15)
    started=time.monotonic()
    try:
        migrate(engine);ledger=Ledger(engine)
        owner=ledger.create_user('benchmark-owner','容量验证管理员',secrets.token_urlsafe(24),admin=True)
        workspace=ledger.create_workspace(owner,'容量验证项目')['workspace_id']
        with engine.begin() as db:
            db.execute(text('''INSERT INTO tasks(id,task_code,workspace_id,created_by,creator_name_snapshot,project_name_snapshot,
                name,tool_id,action,source,status,current_run_id,idempotency_key,request_hash,created_at)
                SELECT 'bt'||n,'BENCH-'||lpad(n::text,10,'0'),:workspace,:user,'容量验证用户','容量验证项目',
                '容量样本 '||n,CASE WHEN n%2=0 THEN 'data' ELSE 'ocr' END,'analyze','manual',
                CASE WHEN n%5=0 THEN 'failed' ELSE 'succeeded' END,'br'||n,'benchmark-'||n,repeat('a',64),
                CURRENT_TIMESTAMP-n*INTERVAL '1 second' FROM generate_series(1,:count) n'''),{'workspace':workspace,'user':owner['id'],'count':count})
            db.execute(text('''INSERT INTO task_runs(id,task_id,attempt,status,generation,queued_at,finished_at,elapsed_ms)
                SELECT 'br'||n,'bt'||n,1,CASE WHEN n%5=0 THEN 'failed' ELSE 'succeeded' END,1,
                CURRENT_TIMESTAMP-n*INTERVAL '1 second',CURRENT_TIMESTAMP-n*INTERVAL '1 second'+INTERVAL '2 seconds',2000
                FROM generate_series(1,:count) n'''),{'count':count})
            db.execute(text('ANALYZE tasks'));db.execute(text('ANALYZE task_runs'))
        seeded=time.monotonic()-started
        # Twenty independent authenticated users exercise the actual FastAPI endpoint.
        sessions=[]
        for n in range(20):
            user=ledger.create_user('benchmark-user-'+str(n),'容量用户 '+str(n),secrets.token_urlsafe(24))
            ledger.set_member(owner,workspace,user['id'],'viewer')
            token=secrets.token_urlsafe(40)
            from datetime import timedelta
            with engine.begin() as db:db.execute(insert(s.user_sessions).values(id='benchmark-session-'+str(n),token_hash=token_hash(token),csrf_hash=token_hash('not-used-for-get'),user_id=user['id'],expires_at=now()+timedelta(hours=1)))
            sessions.append(token)
        with engine.begin() as db:db.execute(text('ANALYZE workspace_members'))
        from fastapi.testclient import TestClient
        from .server import create_app
        old_origin=os.environ.get('CLEAR_PUBLIC_ORIGIN');os.environ['CLEAR_PUBLIC_ORIGIN']='https://localhost:8889'
        samples=[];cases={}
        try:
            with TestClient(create_app(engine),base_url='https://localhost:8889') as client:
                queries={'latest':f'workspace_id={workspace}&limit=50',
                    'filtered':f'workspace_id={workspace}&status=failed&tool_id=data&limit=50',
                    'search':f'workspace_id={workspace}&keyword=10000&limit=50'}
                for name,query in queries.items():
                    client.get('/api/platform/v2/tasks?'+query,headers={'Cookie':'clear_session='+sessions[0]}).raise_for_status()
                    def query_once(n):
                        begin=time.perf_counter()
                        response=client.get('/api/platform/v2/tasks?'+query,headers={'Cookie':'clear_session='+sessions[n%20]})
                        response.raise_for_status();body=response.json()
                        if len(body['items'])>50:raise AssertionError('Pagination returned unbounded rows')
                        return (time.perf_counter()-begin)*1000
                    with ThreadPoolExecutor(max_workers=20) as pool:values=list(pool.map(query_once,range(100)))
                    cases[name]={'requests':len(values),'p95_ms':sorted(values)[math.ceil(.95*len(values))-1],'max_ms':max(values),'median_ms':statistics.median(values)}
                    samples.extend(values)
        finally:
            if old_origin is None:os.environ.pop('CLEAR_PUBLIC_ORIGIN',None)
            else:os.environ['CLEAR_PUBLIC_ORIGIN']=old_origin
        with engine.connect() as db:
            actual_tasks=db.execute(select(func_count(s.tasks))).scalar_one()
            actual_runs=db.execute(select(func_count(s.task_runs))).scalar_one()
            size=db.execute(text('SELECT pg_database_size(current_database())')).scalar_one()
        result={'tasks':actual_tasks,'runs':actual_runs,'concurrent_users':20,'seed_seconds':seeded,'database_bytes':size,'cases':cases,
            'passed':all(case['p95_ms']<=1000 for case in cases.values())}
        (root/'million-task-benchmark.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        if not result['passed']:raise RuntimeError('API P95 exceeded 1 second; see the saved benchmark evidence')
        return result
    finally:
        engine.dispose()
        with admin.connect() as db:db.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()

def func_count(table):
    from sqlalchemy import func
    return func.count().label('count') if table is None else func.count(table.c.id)
