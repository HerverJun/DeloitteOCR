from contextlib import closing
from pathlib import Path
import sqlite3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from workbench_platform.tasks.native_import_extension import register


def test_native_acceptances_commit_with_originals_even_when_the_reply_is_lost(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2].parent/'ocr/src'))
    from ocr_workbench.store import Store
    store=Store(tmp_path)
    with store.transaction() as db:db.execute("INSERT INTO projects(id,name) VALUES('project','事务接收测试')")
    app=FastAPI();app.state.store=store;calls=[]
    @app.post('/api/projects/{project_id}/documents')
    def upload(project_id:str,body:dict):
        calls.append(body['id'])
        with store.transaction() as db:
            db.execute('INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',(body['id'],project_id,body['id']+'.pdf','pdf','original.pdf','a'*64,1,'2026-10-15','2026-10-15'))
            if body.get('fail_before_commit'):raise RuntimeError('rollback')
        if body.get('lose_reply'):raise RuntimeError('reply lost after commit')
        return {'documents':[{'id':body['id']}],'errors':[]}
    register(app,'ocr')
    with TestClient(app) as client:
        first=client.post('/api/projects/project/documents',json={'id':'once'},headers={'Idempotency-Key':'accepted-once'})
        again=client.post('/api/projects/project/documents',json={'id':'once'},headers={'Idempotency-Key':'accepted-once'})
        assert first.status_code==200 and again.json()==first.json() and calls==['once']
        with pytest.raises(RuntimeError,match='reply lost'):
            client.post('/api/projects/project/documents',json={'id':'committed','lose_reply':True},headers={'Idempotency-Key':'lost-reply'})
        status=client.get('/api/platform/v2/import-requests/lost-reply').json()
        assert status['state']=='interrupted' and status['active'] is False
        assert status['acceptances']==[{'kind':'documents','native_id':'committed','project_id':'project'}]
        with pytest.raises(RuntimeError,match='rollback'):
            client.post('/api/projects/project/documents',json={'id':'rolled-back','fail_before_commit':True},headers={'Idempotency-Key':'rolled-back'})
        assert client.get('/api/platform/v2/import-requests/rolled-back').json()['acceptances']==[]
    # The native DB remains usable by the original Store without the extension.
    original=Store(tmp_path)
    with original.transaction() as db:db.execute("UPDATE projects SET name='兼容原生事务' WHERE id='project'")
