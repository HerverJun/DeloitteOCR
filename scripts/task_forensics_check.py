"""Exercise upload, CPU forensics, immutable artifacts and binary export."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import struct
import time
import zipfile
import zlib
import httpx

parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='https://localhost:8889');parser.add_argument('--ca-file',type=Path)
args=parser.parse_args();root=Path(os.environ['CLEAR_TASK_ROOT']);private=json.loads((root/'runs/demo-login-private.json').read_text('utf-8'))
def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
pixels=b''.join(b'\0'+bytes([(x+y)%256 for x in range(192)]) for y in range(64))
content=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',64,64,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')
with httpx.Client(base_url=args.base_url,headers={'Origin':args.base_url},verify=str(args.ca_file) if args.ca_file else True,timeout=180) as client:
    login=client.post('/api/platform/v2/auth/login',json={'username':private['username'],'password':private['password']});login.raise_for_status();client.headers['X-Clear-CSRF']=login.json()['csrf_token']
    workspace=private['workspace_id'];prefix=f'/api/platform/v1/workspaces/{workspace}'
    uploaded=client.post(prefix+'/assets/uploads',params={'name':'取证验证.png'},headers={'Content-Type':'application/octet-stream','Idempotency-Key':'forensic-upload-'+secrets.token_hex(6)},content=content)
    uploaded.raise_for_status();asset=uploaded.json();upload_task=uploaded.headers['X-Clear-Task-ID']
    result=client.post(prefix+'/forensics',json={'schema_version':'1.0','request_id':'forensic-'+secrets.token_hex(6),'asset_id':asset['asset_id'],'asset_revision':asset['revision'],'blob_hash':asset['blob_hash'],'tools':['metadata','frequency']})
    result.raise_for_status();task_id=result.headers['X-Clear-Task-ID'];native_id=result.json()['inspection_id']
    for _ in range(240):
        detail=client.get('/api/platform/v2/tasks/'+task_id).json()
        if detail['task']['status'] in {'succeeded','partial'} and detail['artifacts']:break
        if detail['task']['status'] in {'failed','needs_verification'}:raise AssertionError(detail['runs'][-1].get('error'))
        time.sleep(.5)
    else:raise AssertionError('Forensics did not finish')
    artifact=next(row for row in detail['artifacts'] if row['name']=='report.json')
    download=client.get(f'/api/platform/v2/tasks/{task_id}/artifacts/{artifact["id"]}/download');download.raise_for_status()
    assert hashlib.sha256(download.content).hexdigest()==artifact['blob_hash']
    exported=client.get(prefix+f'/forensics/{native_id}/download',headers={'Idempotency-Key':'forensic-export-'+secrets.token_hex(6)});exported.raise_for_status()
    assert exported.content[:2]==b'PK' and exported.headers['content-type'].startswith('application/zip')
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:assert 'report.json' in archive.namelist()
    export_task=exported.headers['X-Clear-Task-ID']
    report={'passed':True,'upload_task':upload_task,'task_id':task_id,'inspection_id':native_id,'export_task':export_task,'artifacts':len(detail['artifacts']),'steps':len(detail['steps']),'export_bytes':len(exported.content),'report_sha256':artifact['blob_hash']}
    (root/'evidence/live-forensics.json').write_text(json.dumps(report,indent=2),'utf-8');print(json.dumps(report))
