import asyncio
import sqlite3
import httpx
from workbench_platform.tasks import journal
from workbench_platform.tasks.worker import ExecutionHost
from .conftest import submission


def test_native_commit_without_acceptance_receipt_is_recovered_without_reposting(task_case,tmp_path):
    ledger,admin,editor,viewer,stranger,workspace=task_case
    command={'tool_id':'data','action':'import','method':'POST','path':'/api/platform/v1/data/sessions/sessionA/actions','payload':{},'groups':['sessionA']}
    task=ledger.submit(editor,submission(workspace,'lost-acceptance'),command)
    dispatch=ledger.claim('old-worker')
    path=tmp_path/'data-workbench/sessions.sqlite3';path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sessions(id TEXT,workspace_id TEXT)')
        db.execute('CREATE TABLE operation_requests(request_key TEXT,session_id TEXT,job_id TEXT)')
        db.execute('CREATE TABLE jobs(id TEXT,session_id TEXT,status TEXT)')
        db.execute('INSERT INTO sessions VALUES(?,?)',('sessionA',workspace))
        db.execute('INSERT INTO operation_requests VALUES(?,?,?)',(dispatch['run_id'],'sessionA','native-accepted'))
    journal.install(path,[('data.jobs','jobs','id',None)])
    with sqlite3.connect(path) as db:db.execute("INSERT INTO jobs VALUES('native-accepted','sessionA','succeeded')")
    ledger.observe(dispatch,'needs_verification',identity='lost-'+task['id'])
    async def exercise():
        calls=[]
        def respond(request):
            calls.append(request.method)
            assert request.method=='GET','Recovery sent a second execution request'
            return httpx.Response(200,json={'id':'native-accepted','status':'succeeded'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),base_url='http://127.0.0.1:8890') as client:
            host=ExecutionHost(ledger.engine,tmp_path,client);host.save_receipt(dispatch['id'],'sending')
            current=ledger.detail(editor,task['id'])['task'];ledger.control(editor,task['id'],'verify',current['revision'],'verify-'+task['id'])
            await host.execute(ledger.claim('new-worker'))
            await host.publish([path]);await host.close()
            assert calls and all(method=='GET' for method in calls)
    asyncio.run(exercise())
    result=ledger.detail(viewer,task['id'])
    assert result['task']['status']=='succeeded' and len(result['runs'])==1 and len(result['steps'])==1
