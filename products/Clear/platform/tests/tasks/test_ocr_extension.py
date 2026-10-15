import sys
from pathlib import Path
from types import SimpleNamespace
import threading
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from workbench_platform.tasks.ocr_extension import register


def test_native_retry_clones_atomically_and_keeps_original(tmp_path,monkeypatch):
    source=Path(__file__).resolve().parents[2].parent/'ocr/src'
    monkeypatch.syspath_prepend(str(source))
    from ocr_workbench.store import Store
    store=Store(tmp_path)
    with store.transaction() as db:
        db.execute("INSERT INTO projects(id,name) VALUES('project','重试项目')")
        db.execute("INSERT INTO images(id,project_id) VALUES('image','project')")
        for version in ('original','prepared'):db.execute('INSERT INTO versions(id,image_id) VALUES(?,?)',(version,'image'))
        for identifier,status in [('failed','failed'),('success','succeeded')]:
            db.execute("INSERT INTO tasks(id,project_id,image_id,version_id,input_version_id,engine,engine_package,status,kind,error) VALUES(?,?,?,?,?,?,?,?,?,?)",(identifier,'project','image','prepared','original','ppocr','builtin',status,'ocr','原错误' if status=='failed' else None))
    app=FastAPI();app.state.store=store
    app.state.application_services=SimpleNamespace(require_recognition=lambda:None,registry=SimpleNamespace(engines=lambda:{'ppocr':{'package_id':'builtin'}}))
    for name in ('queue','fusion_queue','external_queue'):setattr(app.state,name,SimpleNamespace(wake=threading.Event()))
    register(app)
    @app.post('/api/pages/{page_id}/render')
    def render(page_id:str,body:dict):return {'stage_id':store.enqueue_document_stage(page_id,'render',body)}
    with TestClient(app) as client:
        payload={'project_id':'project','task_ids':['failed','success'],'request_id':'retry-0001'}
        response=client.post('/api/platform/v2/task-retries',json=payload)
        assert response.status_code==200,response.text
        receipt=response.json();assert receipt['reused_task_ids']==['success']
        assert client.post('/api/platform/v2/task-retries',json=payload).json()==receipt
        with store.transaction() as db:
            old=dict(db.execute("SELECT * FROM tasks WHERE id='failed'").fetchone())
            new=dict(db.execute('SELECT * FROM tasks WHERE id=?',(receipt['task_ids'][0],)).fetchone())
            assert old['status']=='failed' and old['error']=='原错误'
            assert new['id']!=old['id'] and new['status']=='queued' and new['error'] is None and new['version_id']=='original'
        bad={**payload,'request_id':'retry-0002','task_ids':['failed','missing']}
        assert client.post('/api/platform/v2/task-retries',json=bad).status_code==403
        with store.transaction() as db:assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==3
        app.state.documents=SimpleNamespace(wake=threading.Event())
        with store.transaction() as db:
            db.execute("INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,created,updated) VALUES('doc','project','测试.pdf','pdf','original.pdf',?,1,'2026-10-15','2026-10-15')",('a'*64,))
            db.execute("INSERT INTO pages(id,document_id,page_number,created,updated) VALUES('page','doc',1,'2026-10-15','2026-10-15')")
            db.execute("INSERT INTO document_stages(id,page_id,kind,request_key,parameters,status,created,error) VALUES('stage','page','render','old','{}','failed','2026-10-15','原阶段错误')")
        response=client.post('/api/platform/v2/document-stage-retries',json={'stage_ids':['stage'],'request_id':'stage-retry-1'})
        assert response.status_code==200,response.text
        new_id=response.json()['stage_ids'][0]
        with store.transaction() as db:
            assert db.execute("SELECT error FROM document_stages WHERE id='stage'").fetchone()[0]=='原阶段错误'
            assert db.execute('SELECT status FROM document_stages WHERE id=?',(new_id,)).fetchone()[0]=='queued'
        with store.transaction() as db:db.execute("UPDATE document_stages SET status='failed' WHERE status='queued'")
        first=client.post('/api/pages/page/render',json={'dpi':72},headers={'Idempotency-Key':'stage-acceptance-1'})
        second=client.post('/api/pages/page/render',json={'dpi':72},headers={'Idempotency-Key':'stage-acceptance-1'})
        assert first.status_code==200 and second.json()==first.json()
        assert client.post('/api/pages/page/render',json={'dpi':96},headers={'Idempotency-Key':'stage-acceptance-1'}).status_code==409
        with store.transaction() as db:assert json.loads(db.execute("SELECT stage_ids FROM central_stage_requests WHERE request_id='stage-acceptance-1'").fetchone()[0])==[first.json()['stage_id']]
