"""Real native PDF stage failure/retry through the authenticated gateway."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import httpx

parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='https://localhost:8889');parser.add_argument('--ca-file',type=Path);args=parser.parse_args()
root=Path(os.environ['CLEAR_TASK_ROOT']);private=json.loads((root/'runs/demo-login-private.json').read_text('utf-8'));data=root/'runs/workspace'
objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
stream=b'BT /F1 16 Tf 30 140 Td (Clear task history verification) Tj ET';objects.append(b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream')
pdf=bytearray(b'%PDF-1.4\n');offsets=[]
for number,body in enumerate(objects,1):offsets.append(len(pdf));pdf.extend(f'{number} 0 obj\n'.encode()+body+b'\nendobj\n')
xref=len(pdf);pdf.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
for offset in offsets:pdf.extend(f'{offset:010d} 00000 n \n'.encode())
pdf.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
with httpx.Client(base_url=args.base_url,headers={'Origin':args.base_url},verify=str(args.ca_file) if args.ca_file else True,timeout=180) as client:
    login=client.post('/api/platform/v2/auth/login',json={'username':private['username'],'password':private['password']});login.raise_for_status();client.headers['X-Clear-CSRF']=login.json()['csrf_token'];workspace=private['workspace_id'];client.headers['X-Clear-Workspace']=workspace
    launch='l'+secrets.token_hex(12);response=client.post('/api/platform/v1/apps/ocr/launches/'+launch);response.raise_for_status()
    print('Native OCR launch accepted.',flush=True)
    for _ in range(240):
        launch_state=client.get('/api/platform/v1/apps/ocr/launches/'+launch).json()
        if launch_state.get('pid') and launch_state.get('base_url'):break
        if launch_state.get('status') in {'failed','stopped','error'}:raise AssertionError('Native startup failed: '+str(launch_state.get('status')))
        time.sleep(.5)
    else:raise AssertionError('Native OCR startup timed out')
    print('Native OCR ready.',flush=True)
    prefix=f'/api/platform/v1/workspaces/{workspace}/ocr';response=client.post(prefix+'/projects',json={'name':'PDF 阶段重试验证','request_id':'project-'+secrets.token_hex(10)});response.raise_for_status();project=response.json()
    native=f'/native/{workspace}/ocr/{project["link_id"]}'
    response=client.post(native+f'/api/projects/{project["project_id"]}/documents',files={'files':('阶段验证.pdf',bytes(pdf),'application/pdf')});response.raise_for_status();document=response.json()['documents'][0];import_task=response.headers['X-Clear-Task-ID']
    pages=client.get(native+f'/api/documents/{document["id"]}/pages');pages.raise_for_status();page=pages.json()['pages'][0]
    source=(data/'native/ocr'/document['original_path']).resolve()
    assert source.is_relative_to((data/'native/ocr').resolve()) and source.is_file()
    held=source.with_suffix('.held');source.rename(held)
    def wait(identifier,status):
        for _ in range(240):
            detail=client.get('/api/platform/v2/tasks/'+identifier);detail.raise_for_status();value=detail.json()
            if value['task']['status']==status:return value
            if value['task']['status'] in {'failed','succeeded','partial','needs_verification'}:raise AssertionError('Unexpected PDF state: '+str(value['runs'][-1].get('error') or value['task']['status']))
            time.sleep(.5)
        raise AssertionError('PDF stage did not finish')
    try:
        response=client.post(native+f'/api/pages/{page["id"]}/render',json={'dpi':72});response.raise_for_status();task_id=response.headers['X-Clear-Task-ID'];old_stage=response.json()['stage_id'];failed=wait(task_id,'failed')
    finally:held.rename(source)
    response=client.post('/api/platform/v2/tasks/'+task_id+'/controls',json={'operation':'retry','expected_revision':failed['task']['revision'],'idempotency_key':'retry-'+secrets.token_hex(10)});response.raise_for_status()
    success=wait(task_id,'succeeded')
    for _ in range(40):
        if len(success['steps'])>=2 and success['artifacts']:break
        time.sleep(.25);success=client.get('/api/platform/v2/tasks/'+task_id).json()
    assert len(success['runs'])==2 and len(success['steps'])>=2 and success['artifacts']
    assert success['task']['input_total_bytes']==len(pdf)
    with sqlite3.connect(data/'native/ocr/workbench.sqlite3') as db:
        assert db.execute('SELECT status,error FROM document_stages WHERE id=?',(old_stage,)).fetchone()[0]=='failed'
    artifact=success['artifacts'][0];response=client.get(f'/api/platform/v2/tasks/{task_id}/artifacts/{artifact["id"]}/download');response.raise_for_status();assert response.content.startswith(b'\x89PNG')
    result={'passed':True,'import_task':import_task,'task_id':task_id,'old_stage':old_stage,'runs':len(success['runs']),'steps':len(success['steps']),'artifacts':len(success['artifacts']),'input_bytes':len(pdf)}
    (root/'evidence/live-pdf-stages.json').write_text(json.dumps(result,indent=2),'utf-8');print(json.dumps(result))
