"""Real CSV failure/retry/readback against the isolated demo workspace."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import httpx
import argparse

parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='http://127.0.0.1:8889');parser.add_argument('--workspace-root',type=Path);parser.add_argument('--report',default='live-data.json');parser.add_argument('--ca-file',type=Path)
args=parser.parse_args()
root=Path(os.environ['CLEAR_TASK_ROOT']).resolve();workspace_root=(args.workspace_root or root/'runs/workspace').resolve()
if workspace_root.drive.upper()!='F:':raise ValueError('Use F storage')
private=json.loads((root/'runs/demo-login-private.json').read_text('utf-8'))
base=args.base_url
with httpx.Client(base_url=base,timeout=180,headers={'Origin':base},verify=str(args.ca_file) if args.ca_file else True) as client:
    response=client.post('/api/platform/v2/auth/login',json={'username':private['username'],'password':private['password']});response.raise_for_status()
    client.headers['X-Clear-CSRF']=response.json()['csrf_token']
    workspace=private['workspace_id']
    response=client.post('/api/platform/v1/data/sessions',json={'workspace_id':workspace,'name':'真实 CSV 失败与恢复验证'});response.raise_for_status();session=response.json()
    sid=session['id'];content='日期,金额\n2026-10-15,128.50\n2026-10-16,99.00\n'.encode('utf-8-sig')
    response=client.post(f'/api/platform/v1/data/sessions/{sid}/uploads',params={'name':'验证.csv'},content=content,headers={'Content-Type':'application/octet-stream'});response.raise_for_status();upload=response.json()
    with sqlite3.connect(workspace_root/'data-workbench/sessions.sqlite3') as db:
        relative=db.execute('SELECT relative FROM uploads WHERE id=?',(upload['id'],)).fetchone()[0]
    original=(workspace_root/'data-workbench/sessions'/sid/relative).resolve();held=original.with_suffix('.held')
    assert original.is_relative_to(workspace_root.resolve()) and held.is_relative_to(workspace_root.resolve()) and original.drive.upper()=='F:'
    original.rename(held)
    def wait_task(identifier,statuses):
        for _ in range(180):
            detail=client.get('/api/platform/v2/tasks/'+identifier);detail.raise_for_status();value=detail.json()
            if value['task']['status'] in statuses:return value
            if value['task']['status'] in {'succeeded','failed','cancelled','partial'}:raise AssertionError('Unexpected terminal state: '+value['task']['status'])
            time.sleep(.5)
        raise AssertionError('Task did not reach the expected state')
    request_key='live-'+sid
    payload={'action':'import','payload':{'side':'left','upload_id':upload['id'],'options':{'header_row':1}},'version':session['version']}
    try:
        response=client.post(f'/api/platform/v1/data/sessions/{sid}/actions',json=payload,headers={'Idempotency-Key':request_key});response.raise_for_status()
        task_id=response.headers['x-clear-task-id'];failed=wait_task(task_id,{'failed'})
    finally:held.rename(original)
    duplicate=client.post(f'/api/platform/v1/data/sessions/{sid}/actions',json=payload,headers={'Idempotency-Key':request_key});duplicate.raise_for_status()
    assert duplicate.headers['x-clear-task-id']==task_id
    response=client.post(f'/api/platform/v2/tasks/{task_id}/controls',json={'operation':'retry','expected_revision':failed['task']['revision'],'idempotency_key':'retry-'+sid});response.raise_for_status()
    success=wait_task(task_id,{'succeeded'})
    for _ in range(80):
        if len(success['steps'])>=2:break
        time.sleep(.25);success=client.get('/api/platform/v2/tasks/'+task_id).json()
    assert len(success['steps'])>=2,'Native outbox did not publish both attempts'
    assert len(success['runs'])==2 and success['runs'][0]['status']=='failed' and success['runs'][0]['error']
    assert success['task']['input_total_bytes']==len(content) and success['task']['file_count']==1
    final=client.get(f'/api/platform/v1/data/sessions/{sid}').json()
    assert final['version']>session['version']
    report={'task_id':task_id,'session_id':sid,'first_attempt':success['runs'][0]['status'],'second_attempt':success['runs'][1]['status'],
        'input_bytes':success['task']['input_total_bytes'],'input_sha256':hashlib.sha256(content).hexdigest(),'native_version':final['version'],'steps':len(success['steps'])}
    (root/'evidence'/args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
