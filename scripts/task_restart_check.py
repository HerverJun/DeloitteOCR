"""Submit a real CSV job, then verify it after restarting only the portal."""
import argparse
import json
import os
from pathlib import Path
import secrets
import time
import httpx

parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['submit','verify']);args=parser.parse_args()
root=Path(os.environ['CLEAR_TASK_ROOT']);private=json.loads((root/'runs/demo-login-private.json').read_text('utf-8'));service=json.loads((root/'runs/runtime-private.json').read_text('utf-8'));base='https://localhost:8889'
record=root/'evidence/portal-restart-pending.json'
worker=httpx.get('http://127.0.0.1:8890/internal/status',headers={'Authorization':'Bearer '+service['worker_token']},timeout=10);worker.raise_for_status()
with httpx.Client(base_url=base,headers={'Origin':base},verify=str(root/'runs/tls/server.crt'),timeout=180) as client:
    login=client.post('/api/platform/v2/auth/login',json={'username':private['username'],'password':private['password']});login.raise_for_status();client.headers['X-Clear-CSRF']=login.json()['csrf_token']
    if args.phase=='submit':
        workspace=private['workspace_id'];session=client.post('/api/platform/v1/data/sessions',json={'workspace_id':workspace,'name':'门户重启连续执行验证'});session.raise_for_status();session=session.json();sid=session['id']
        content=('编号,甲,乙,丙,丁,戊,己,金额\n'+''.join(','.join([str(number),*([str(number%1000)]*6),str(number%1000)+'.25'])+'\n' for number in range(100000))).encode('utf-8-sig')
        upload=client.post(f'/api/platform/v1/data/sessions/{sid}/uploads',params={'name':'持续执行.csv'},content=content,headers={'Content-Type':'application/octet-stream'});upload.raise_for_status()
        response=client.post(f'/api/platform/v1/data/sessions/{sid}/actions',json={'action':'import','payload':{'side':'left','upload_id':upload.json()['id'],'options':{'header_row':1}},'version':session['version']},headers={'Idempotency-Key':'restart-'+secrets.token_hex(10)});response.raise_for_status();task=response.headers['X-Clear-Task-ID']
        for _ in range(40):
            detail=client.get('/api/platform/v2/tasks/'+task).json()
            if detail['task']['status']=='running':break
            if detail['task']['status'] in {'failed','succeeded'}:raise AssertionError('The restart fixture finished before interruption')
            time.sleep(.1)
        value={'task_id':task,'session_id':sid,'worker_id':worker.json()['worker_id'],'status_before':'running','rows':100000,'input_bytes':len(content)}
        record.write_text(json.dumps(value,indent=2),'utf-8');print(json.dumps(value))
    else:
        value=json.loads(record.read_text('utf-8'));assert worker.json()['worker_id']==value['worker_id'],'Portal restart replaced the execution service'
        for _ in range(240):
            response=client.get('/api/platform/v2/tasks/'+value['task_id']);response.raise_for_status();detail=response.json()
            if detail['task']['status']=='succeeded':break
            if detail['task']['status'] in {'failed','needs_verification'}:raise AssertionError(detail['runs'][-1].get('error') or detail['task']['status'])
            time.sleep(.5)
        else:raise AssertionError('CSV execution did not finish')
        assert len(detail['runs'])==1
        value.update(passed=True,status_after=detail['task']['status'],attempts=len(detail['runs']),same_worker=True)
        (root/'evidence/portal-restart.json').write_text(json.dumps(value,indent=2),'utf-8');print(json.dumps(value))
