"""Read durable native acceptance journals; recovery never resubmits work."""
from contextlib import closing
import json
import re
import sqlite3
import hashlib
from sqlalchemy import select
from . import schema_v1 as s
from .private_payloads import unseal
from .store import digest
from .database import f_path


def native_body(root,native):
    source=f_path(native['body_file'])
    if not source.is_relative_to(root/'native-requests'):return None
    raw=source.read_bytes()
    if native.get('body_encoding')=='dpapi':
        from workbench_platform.agent.credentials import CredentialVault
        raw=CredentialVault._crypt(raw,decrypt=True)
    return raw if hashlib.sha256(raw).hexdigest()==native['body_hash'] else None


def one(root,relative,sql,args):
    path=root/relative
    if not path.is_file():return None
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        try:row=db.execute(sql,args).fetchone()
        except sqlite3.OperationalError:return None
        return dict(row) if row else None


async def binding(host,dispatch):
    command=unseal(host.root,dict(dispatch['command']));payload=command.get('payload',{})
    with host.engine.connect() as db:
        workspace=db.execute(select(s.tasks.c.workspace_id).where(s.tasks.c.id==dispatch['task_id'])).scalar_one()
        previous=db.execute(select(s.task_dispatches.c.receipt).where(s.task_dispatches.c.run_id==command.get('retry_of'),s.task_dispatches.c.kind=='execute')).scalar_one_or_none() if command.get('retry_of') else None
    path=command['path'];tool=command['tool_id'];prefix='/api/platform/v1';request_id=payload.get('request_id')
    if tool=='documents':
        row=one(host.root,'documents/documents.sqlite3','SELECT document_id FROM documents WHERE workspace_id=? AND request_id=?',(workspace,request_id))
        if row:return ('documents.documents',row['document_id'],f'{prefix}/workspaces/{workspace}/documents/{row["document_id"]}')
    elif tool=='data':
        session=re.search(r'/data/sessions/([A-Za-z0-9_-]+)/',path)
        row=one(host.root,'data-workbench/sessions.sqlite3','SELECT r.job_id,r.session_id FROM operation_requests r JOIN sessions s ON s.id=r.session_id WHERE r.request_key=? AND s.workspace_id=?',(dispatch['run_id'],workspace))
        if row and session and row['session_id']==session[1]:return ('data.jobs',row['job_id'],f'{prefix}/data/sessions/{row["session_id"]}/jobs/{row["job_id"]}')
    elif tool in {'face','audio','seal','catnet','document_forensics','aigc_image'}:
        row=one(host.root,'jobs.sqlite3','SELECT job_id FROM manual_analysis WHERE workspace_id=? AND request_id=?',(workspace,request_id))
        if row:return ('manual.manual_analysis',row['job_id'],f'{prefix}/workspaces/{workspace}/multimodal/jobs/{row["job_id"]}')
    elif tool=='forensics':
        row=one(host.root,'forensics/forensics.sqlite3','SELECT inspection_id FROM inspections WHERE workspace_id=? AND request_id=?',(workspace,dispatch['run_id'] if command.get('retry_of') else request_id))
        if row:return ('forensics.inspections',row['inspection_id'],f'{prefix}/workspaces/{workspace}/forensics/{row["inspection_id"]}')
    elif tool=='batch':
        row=one(host.root,'batches/batches.sqlite3','SELECT batch_id FROM batches WHERE workspace_id=? AND request_key=?',(workspace,payload.get('request_key')))
        if row:return ('batches.batches',row['batch_id'],f'{prefix}/batches/{row["batch_id"]}')
    elif tool=='agent':
        row=one(host.root,'agent/operations.sqlite3','SELECT id FROM runs WHERE workspace_id=? AND request_id=?',(workspace,request_id))
        if row:return ('agent.runs',row['id'],f'{prefix}/agent/runs/{row["id"]}')
    elif tool in {'ocr','classification'} and not command.get('native'):
        response=await host.call('GET',path.rsplit('/',1)[0])
        if response.is_success:
            from .registry import binding as recorded_binding
            result=recorded_binding(command,response.json())
            if result:return result
    native=command.get('native')
    if native:
        application=native['application'];key=native.get('idempotency_key') or dispatch['run_id']
        with host.engine.connect() as db:project=db.execute(select(s.module_project_links.c.module_project_id).where(s.module_project_links.c.workspace_id==workspace,s.module_project_links.c.link_id==native['link_id'],s.module_project_links.c.app_id==application)).scalar_one_or_none()
        if not project:return None
        ids=[]
        if command['action']=='import' and re.fullmatch(r'(?:/)?api/(?:v1/)?projects/[A-Za-z0-9_-]+/(images|documents|uploads)',native['path']):
            from urllib.parse import quote
            from .native import perform_native
            from .files import response_bytes
            raw=native_body(host.root,native)
            if raw is None:return None
            status=await perform_native(host.legacy,host.root,{'workspace_id':workspace,'application':application,'link_id':native['link_id'],'method':'GET','path':'api/platform/v2/import-requests/'+quote(key,safe=''),'query':'','trusted_task':True})
            if status['status']!=200:return None
            value=json.loads(response_bytes(host.root,status))
            query=native.get('query','');signature=hashlib.sha256(('/'+native['path'].lstrip('/')+('?' + query if query else '')).encode()+b'\0'+raw).hexdigest()
            if value['payload_hash']!=signature or value['active'] or any(item['project_id']!=project for item in value['acceptances']):return None
            if value.get('response_body'):
                body=json.loads(value['response_body'])
            else:
                field='asset_ids' if application=='classification' else 'documents' if native['path'].endswith('/documents') else 'images'
                kind='assets' if application=='classification' else field;accepted=[item for item in value['acceptances'] if item['kind']==kind]
                records=[one(host.root,'native/'+application+('/workbench.sqlite3' if application=='ocr' else '/platform.sqlite3'),f'SELECT * FROM {kind} WHERE id=?',(item['native_id'],)) for item in accepted]
                records=[row for row in records if row]
                from .multipart import input_records
                remaining=input_records(raw,native['content_type'])
                for row in records:
                    found=next((item for item in remaining if item['blob_hash']==row['sha256'] and item['name']==row['name']),None)
                    if found:remaining.remove(found)
                body={field:[row['id'] for row in records] if field=='asset_ids' else records,'errors':[{'name':item['name'],'message':'原导入已中断，此文件未接收'} for item in remaining]}
            accepted=any(body.get(name) for name in ('documents','images','asset_ids'))
            errors=body.get('errors',[]) or ([{'message':body['detail']}] if body.get('detail') else [])
            body={**body,'errors':errors,'status':('partial' if accepted else 'failed') if errors else 'succeeded','recovered_import':True}
            return ('native.imports',digest([application,project,key]),body)
        if application=='ocr':
            raw=native_body(host.root,native)
            if raw is None:return None
            def stages_in_project(ids):
                if not ids:return False
                query='SELECT COUNT(*) AS total FROM document_stages s JOIN pages p ON p.id=s.page_id JOIN documents d ON d.id=p.document_id WHERE d.project_id=? AND s.id IN ('+','.join('?' for _ in ids)+')'
                count=one(host.root,'native/ocr/workbench.sqlite3',query,[project,*ids])
                return count and count['total']==len(ids)
            if command.get('retry_of'):
                row=one(host.root,'native/ocr/workbench.sqlite3','SELECT receipt FROM central_retry_requests WHERE request_id=?',(dispatch['run_id'],))
                if row:
                    receipt=json.loads(row['receipt'])
                    if receipt.get('stage_ids') and stages_in_project(receipt['stage_ids']):return ('ocr.document_stages',digest(receipt['stage_ids']),receipt['stage_ids'])
                    ids=receipt.get('task_ids',[])
            if not ids:
                row=one(host.root,'native/ocr/workbench.sqlite3','SELECT payload_hash,stage_ids FROM central_stage_requests WHERE request_id=?',(key,))
                signature=hashlib.sha256(('/'+native['path'].lstrip('/')).encode()+b'\0'+raw).hexdigest()
                if row and row['payload_hash']==signature and stages_in_project(json.loads(row['stage_ids'])):return ('ocr.document_stages',digest(json.loads(row['stage_ids'])),json.loads(row['stage_ids']))
                try:body=json.loads(raw)
                except (ValueError,UnicodeError):body={}
                table='fusion_submissions' if native['path'].rstrip('/').endswith('/fusion') else 'ocr_submissions'
                row=one(host.root,'native/ocr/workbench.sqlite3',f'SELECT task_ids FROM {table} WHERE project_id=? AND request_id=?',(project,body.get('request_id') or key))
                if row:ids=json.loads(row['task_ids'])
        else:
            kind='train' if native['path'].endswith('/experiments') else 'import' if native['path'].endswith('/imports') else 'inference'
            row=one(host.root,'native/classification/platform.sqlite3','SELECT job_id FROM idempotency WHERE scope=? AND key=?',(project+':'+kind,key))
            if row:ids=[row['job_id']]
        if ids:
            reference=host.legacy.state.job_service.register(workspace,native['link_id'],application,ids)
            identifier=reference['platform_job_id'];return ('projection',identifier,f'{prefix}/workspaces/{workspace}/jobs/{identifier}')
    return tuple(previous['binding']) if previous and previous.get('binding') else None
