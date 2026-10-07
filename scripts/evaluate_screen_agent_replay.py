"""Real-controller overhead on frozen OCR receipts, with zero GPU inference.

The loopback replay service implements the production native HTTP contract.
AgentRuntime, OcrAdapter, OcrHandoff, ScreenBatches and artifact publication are
the production implementations. Repeated receipts are execution-scale evidence,
never additional independent quality observations. No external call is made
without --run-controller. All files and process caches stay on F:.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import zipfile

sys.dont_write_bytecode = True


def f_path(value):
    path = Path(value).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('All replay paths must resolve to local F:')
    return path


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def save(path,value):
    f_path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def configure(args):
    root=f_path(args.output)
    root.mkdir(parents=True,exist_ok=True)
    temporary=root/'temp';temporary.mkdir(exist_ok=True)
    for name in ('TEMP','TMP','TMPDIR','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME','MPLCONFIGDIR'):
        os.environ[name]=str(temporary)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    os.environ['WORKBENCH_AGENT_LAYER']=str(f_path(args.agent_layer))
    sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'src')]
    sys.path.append(str(f_path(args.ocr_source)/'src'))
    sys.path.append(str(f_path(args.image_layer)))
    return root


def freeze(args,root):
    from PIL import Image,ImageOps
    from ocr_workbench.screen_quality import assess_screen_result
    inputs=f_path(args.inputs);receipts=f_path(args.receipts)
    original=json.loads(inputs.read_text(encoding='utf-8'))
    binding=json.loads((receipts/'binding.json').read_text(encoding='utf-8'))
    if binding.get('inputs_sha256')!=sha(inputs):
        raise ValueError('Frozen receipt binding does not match this input manifest')
    valid=[]
    for sample in original['samples']:
        image=f_path(sample['image']);record=receipts/sample['id']/'record.json';result=receipts/sample['id']/'result.json'
        if not record.is_file():raise ValueError('A frozen input has no terminal receipt')
        receipt=json.loads(record.read_text(encoding='utf-8'))
        if receipt['image_sha256']!=sample['image_sha256'] or sha(image)!=sample['image_sha256']:
            raise ValueError('A frozen input digest changed')
        if receipt['status']!='success':continue
        if sha(result)!=receipt['result_sha256']:raise ValueError('A frozen result digest changed')
        raw=json.loads(result.read_text(encoding='utf-8'))
        if not raw.get('tables'):continue
        valid.append((sample,record,result,raw))
    if not valid:raise ValueError('No frozen table-bearing receipt is available')
    folder=root/'inputs';folder.mkdir(exist_ok=True)
    mapping=[]
    for index in range(args.count):
        sample,record,result,raw=valid[index%len(valid)]
        target=folder/f'{index+1:04d}-{sample["id"]}.png'
        with Image.open(f_path(sample['image'])) as opened:
            normalized=ImageOps.exif_transpose(opened).convert('RGB')
            pixels=hashlib.sha256(normalized.tobytes()).hexdigest()
            normalized.save(target,format='PNG')
        quality=assess_screen_result(raw)
        # Early whole-image receipts did not execute the new screen pipeline.
        # Keep every real issue and mark this replay as awaiting human review;
        # replay eligibility is not evidence of screen-extraction quality.
        quality.update(review_required=True,automatic_delivery=False,replay_screen_quality_verified=False)
        mapping.append({'position':index,'sample_id':sample['id'],'source_image_sha256':sample['image_sha256'],
            'normalized_input':str(target),'normalized_png_sha256':sha(target),'normalized_pixel_sha256':pixels,
            'record':str(record),'record_sha256':sha(record),'result':str(result),'result_sha256':sha(result),
            'quality':quality,'raw':raw})
    save(root/'receipt-map.json',{'format':'screen-agent-frozen-replay-v1','original_inputs_sha256':sha(inputs),
        'original_sample_count':len(original['samples']),'eligible_table_receipts':len(valid),'scope_inputs':args.count,
        'independent_receipts_used':len({item['sample_id'] for item in mapping}),
        'duplicates_are_not_quality_samples':True,'gpu_calls':0,
        'items':[{k:v for k,v in item.items() if k!='raw'} for item in mapping]})
    return mapping


class ReplayService:
    """Frozen native replies with a deliberate wait, not simulated model time."""
    def __init__(self,root,mapping,hold,early_count=0):
        self.root,self.mapping,self.hold=root,mapping,hold
        self.early_count=early_count
        self.token=secrets.token_urlsafe(36)
        self.guard=threading.RLock()
        self.imports,self.batches,self.requests={},{},{}
        self.calls=[]
        self.asset_map={}
        self.started=time.perf_counter()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),self.handler())
        self.base_url=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def handler(self):
        service=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def perform(self):
                if self.headers.get('Authorization')!='Bearer '+service.token:
                    self.send_error(403);return
                data=self.rfile.read(int(self.headers.get('Content-Length','0')))
                try:
                    with service.guard:
                        status,value,media=service.route(self.command,self.path,data)
                except (ValueError,KeyError,TypeError):
                    status,value,media=400,{'detail':'Unsupported or conflicting frozen replay request'},'application/json'
                content=value if isinstance(value,bytes) else encoded(value).encode('utf-8')
                self.send_response(status);self.send_header('Content-Type',media)
                self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
            do_GET=perform
            do_POST=perform
            do_PUT=perform
        return Handler

    def route(self,method,path,data):
        self.calls.append({'method':method,'path':path,'elapsed_seconds':time.perf_counter()-self.started})
        parsed=urlsplit(path);parts=parsed.path.strip('/').split('/');query=parse_qs(parsed.query)
        body=json.loads(data) if data and method=='POST' else {}
        json_type='application/json'
        if parsed.path=='/api/health':
            return 200,{'status':'ready','review_only':False,'queue':{'healthy':True},'frozen_replay':True},json_type
        if parsed.path=='/api/state':
            return 200,{'projects':[{'id':'replayProject','name':'Frozen OCR workflow replay'}],
                'engines':{'hunyuan':{'name':'HunyuanOCR frozen receipts','ready':True,
                    'route':'frozen-replay-no-inference','capabilities':{'text':True,'tables':True}}}},json_type
        if parsed.path=='/api/multimodal/models':return 200,{'models':[]},json_type
        if parts[:3]==['api','platform','imports']:
            if parts[3]!='replayProject':raise ValueError()
            request=parts[4]
            if method=='POST':
                if request in self.imports and self.imports[request]['manifest']!=body:raise ValueError()
                self.imports.setdefault(request,{'manifest':body,'uploaded':set()})
            if request not in self.imports:return 404,{'detail':'Frozen import request not submitted'},json_type
            imported=self.imports[request]
            if method=='PUT':
                item=next(item for item in imported['manifest']['items'] if item['item_id']==parts[6])
                if hashlib.sha256(data).hexdigest()!=item['blob_hash'] or len(data)!=item['size_bytes']:raise ValueError()
                source=self.asset_map[item['asset_id']]
                if item['blob_hash']!=source['normalized_png_sha256']:raise ValueError()
                imported['uploaded'].add(item['item_id'])
            items=[]
            for item in imported['manifest']['items']:
                source=self.asset_map[item['asset_id']];position=source['position'];done=item['item_id'] in imported['uploaded']
                items.append({'item_id':item['item_id'],'status':'imported' if done else 'pending',
                    'target_asset_id':f'image{position}' if done else None,'target_version_id':f'version{position}' if done else None})
            return 200,{'request_id':request,'payload_hash':hashlib.sha256(encoded(imported['manifest']).encode()).hexdigest(),
                'state':'completed' if len(imported['uploaded'])==len(items) else 'accepted' if imported['uploaded'] else 'prepared',
                'items':items},json_type
        if parts[:3]!=['api','projects','replayProject']:raise ValueError()
        if parts[3]=='screen-batch-requests':
            batch_id=self.requests.get(parts[4]);return 200,{'submitted':bool(batch_id),'batch_id':batch_id},json_type
        if parts[3]!='screen-batches':raise ValueError()
        if len(parts)==4 and method=='POST':
            policy=body['policy']
            if policy.get('layout_evidence',False) or policy.get('max_upgrades',2)!=0 or policy.get('fallback_engines',[]) or policy.get('primary_engine') not in {'auto','hunyuan'}:
                raise ValueError('Replay cannot perform an inference or quality upgrade')
            if body['request_id'] not in self.requests:
                identifier='batch'+str(len(self.batches))
                self.requests[body['request_id']]=identifier
                self.batches[identifier]={'batch_id':identifier,'versions':body['version_ids'],'policy':policy,
                    'request_sha256':hashlib.sha256(encoded(body).encode()).hexdigest(),
                    'submitted_at':time.perf_counter(),'paused':False,'cancelled':False}
            elif self.batches[self.requests[body['request_id']]]['request_sha256']!=hashlib.sha256(encoded(body).encode()).hexdigest():
                raise ValueError('Frozen batch request changed')
            return 200,self.batch(self.requests[body['request_id']],0,10,None),json_type
        identifier=parts[4]
        if len(parts)==5:return 200,self.batch(identifier,int(query.get('offset',['0'])[0]),int(query.get('limit',['30'])[0]),query.get('status',[None])[0]),json_type
        if parts[5]=='export':return 200,self.export(identifier,body),'application/zip'
        if parts[5]=='action':
            batch=self.batches[identifier]
            if body['action'] not in {'pause','resume','cancel'}:raise ValueError()
            batch['paused']=body['action']=='pause'
            batch['cancelled']=body['action']=='cancel'
            return 200,self.batch(identifier,0,30,None),json_type
        if parts[5]=='items':
            item=next(item for item in self.items(identifier) if item['input_version_id']==parts[6])
            raw=deepcopy(self.mapping[int(parts[6].replace('version',''))]['raw'])
            return 200,{**item,'result':{'edited':{'tables':raw['tables'],'text':raw.get('text','')},'original':raw,'revision':0},'preparation':None},json_type
        raise ValueError()

    def items(self,identifier):
        batch=self.batches[identifier]
        waiting=time.perf_counter()-batch['submitted_at']<self.hold or batch['paused']
        items=[]
        for version in batch['versions']:
            index=int(version.replace('version',''));source=self.mapping[index];quality=source['quality']
            item_waiting=waiting and index>=self.early_count
            state='cancelled' if batch['cancelled'] else 'paused' if item_waiting and batch['paused'] else 'running' if item_waiting else 'needs_review' if quality['review_required'] else 'completed'
            items.append({'input_version_id':version,'image_id':f'image{index}','source_name':Path(source['normalized_input']).name,
                'status':state,'stage':'frozen_wait' if item_waiting else 'review' if state=='needs_review' else 'completed',
                'result_id':f'result{index}' if not item_waiting else None,'result_revision':0 if not item_waiting else None,
                'issues':quality['issues'],'quality':quality,'human_review':'not_performed','replayed_receipt_sha256':source['result_sha256']})
        return items

    def batch(self,identifier,offset,limit,status):
        items=self.items(identifier);counts={key:sum(item['status']==key for item in items) for key in ('completed','needs_review','failed','cancelled')}
        counts.update(total=len(items),pending=len(items)-sum(counts.values()))
        state='paused' if self.batches[identifier]['paused'] else 'running' if counts['pending'] else 'needs_review' if counts['needs_review'] else 'cancelled' if counts['cancelled'] else 'completed'
        selected=[item for item in items if status is None or (item['status'] in {'running','paused'} if status=='pending' else item['status']==status)]
        return {'batch_id':identifier,'project_id':'replayProject','status':state,'counts':counts,
            'items':selected[offset:offset+limit],'total_items':len(selected),'policy':self.batches[identifier]['policy']}

    def export(self,identifier,options):
        from ocr_workbench.tables import export_xlsx
        if options.get('numeric_columns'):raise ValueError('Replay baseline keeps values as text')
        output=io.BytesIO();folder=self.root/'exports'/identifier;folder.mkdir(parents=True,exist_ok=True)
        manifest={'format':'screen-batch-v1-export','batch_id':identifier,'project_id':'replayProject',
            'policy':self.batches[identifier]['policy'],'items':[],'success_files':0,'frozen_replay':True}
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for item in self.items(identifier):
                entry=deepcopy(item);entry['file_name']=None
                if item['status']=='completed' or options.get('include_pending_review') and item['status']=='needs_review':
                    index=int(item['input_version_id'].replace('version',''));source=self.mapping[index]
                    name=f'{index+1:04d}-{source["sample_id"]}.xlsx';target=folder/name
                    export_xlsx(source['raw']['tables'],target,source_rows=[{'image':item['source_name'],'result_id':item['result_id'],
                        'engine':'hunyuan-frozen-replay','version_id':item['input_version_id'],'revision':0}])
                    archive.write(target,name);entry['file_name']=name;manifest['success_files']+=1
                manifest['items'].append(entry)
            if not manifest['success_files']:raise ValueError('No replayed table is exportable yet')
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        return output.getvalue()

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)


def services(root,mapping,replay):
    from packages.contracts.models import AssetSetItem,SourceLocator
    from workbench_platform.agent.store import digest
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetStore
    from workbench_platform.artifacts.center import ArtifactCenter
    from workbench_platform.bridge.context import ResolvedContext
    from workbench_platform.jobs.native_ocr import OcrAdapter
    from workbench_platform.jobs.ocr_handoff import OcrHandoff
    from workbench_platform.jobs.screen_batches import ScreenBatches
    from workbench_platform.store import WorkspaceStore
    project_id=getattr(replay,'project_id','replayProject')
    workspace=WorkspaceStore(root/'workspaces.sqlite3')
    created=workspace.create_workspace('Frozen OCR Agent performance replay',workspace_id='wreplay')
    link=workspace.add_link('wreplay','ocr',project_id,role='owner',expected_revision=created.revision)
    blobs=root/'blobs';blobs.mkdir()
    assets=AssetRegistry(root/'assets.sqlite3',blobs,{'replay':root/'inputs'})
    fixed=AssetSetStore(assets)
    members=[]
    # Keep a WAL reader open only in this harness to reduce repeated last-close
    # checkpoints; FULL durability and production schemas are unchanged.
    keeper=assets._connect()
    for source in mapping:
        asset=assets.register('wreplay','replay',Path(source['normalized_input']).name,media_type='image/png',storage_mode='referenced')
        source['asset_id']=asset.asset_id;replay.asset_map[asset.asset_id]=source
        members.append(AssetSetItem(schema_version='1.0',item_id='item'+str(source['position']),asset_id=asset.asset_id,
            asset_revision=asset.revision,blob_hash=asset.blob_hash,source_locator=SourceLocator(schema_version='1.0',kind='file',
                asset_id=asset.asset_id,asset_revision=asset.revision,blob_hash=asset.blob_hash,precision='unknown')))
    asset_set=fixed.create('wreplay',members,asset_set_id='setReplay')
    context=ResolvedContext('wreplay','ocr','replayInstance',link.link_id,project_id,'owner','/apps/ocr/workflow',replay.base_url)
    receipt=SimpleNamespace(instance_id='replayInstance',base_url=replay.base_url)
    launches=SimpleNamespace(current=lambda family:receipt)
    jobs=SimpleNamespace(workspaces=workspace,launches=launches,_token=lambda:replay.token,_live=lambda supplied:supplied==context)
    def bound(w,family,link_id):
        if (w,family,link_id)!=('wreplay','ocr',link.link_id):raise ValueError('Replay scope mismatch')
        return context
    jobs._context=bound;jobs._adapter=lambda supplied:OcrAdapter(supplied,token=jobs._token,live=jobs._live,timeout=10)
    handoff=OcrHandoff(root/'ocr-handoff.sqlite3',jobs,assets,fixed)
    center=ArtifactCenter(root/'artifacts',assets,fixed,None,ocr_handoff=handoff)
    batches=ScreenBatches(root/'screen-batches.sqlite3',handoff,center)
    value=SimpleNamespace(workspace_store=workspace,asset_library=(assets,fixed,None),job_service=jobs,launch_service=launches,
        supervisor=SimpleNamespace(registry=SimpleNamespace(installations={'ocr':SimpleNamespace(installed=True)})),
        ocr_handoff=handoff,artifact_center=center,screen_batches=batches,forensic_service=None,
        data_workbench=SimpleNamespace(list_page=lambda *a,**k:{'items':[]}),batch_store=None)
    return value,asset_set,link,keeper


def copy_controller(runtime,source):
    from workbench_platform.agent.connection import SETTING
    from workbench_platform.agent.credentials import CredentialVault
    source=f_path(source);wal=source.with_name(source.name+'-wal')
    # Read-only source. An immutable read is used when there is no pending WAL.
    options='?mode=ro' if wal.is_file() and wal.stat().st_size else '?mode=ro&immutable=1'
    if options=='?mode=ro' and not source.with_name(source.name+'-shm').exists():
        raise ValueError('Active source WAL has no existing reader sidecar; cannot guarantee read-only boundary')
    with closing(sqlite3.connect(source.as_uri()+options,uri=True)) as db:
        row=db.execute('SELECT value FROM settings WHERE key=?',(SETTING,)).fetchone()
    if not row:raise ValueError('Source controller is not configured')
    setting=json.loads(row[0])
    if setting.get('deleted') or setting.get('model')!='gpt-6.1-sol':raise ValueError('Expected configured controller is unavailable')
    key=CredentialVault(source.parent/'credentials').read(setting['credential_ref'])
    own_ref=runtime.connection.vault.save(key)
    copied={**setting,'credential_ref':own_ref,'revision':1}
    with runtime.store.transaction() as db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(SETTING,encoded(copied)))
    del key
    return {key:copied[key] for key in ('model','protocol','context_cap','max_output_tokens','revision')}


async def evaluate(args,root,mapping,replay,value,fixed,link):
    platform_source=Path(__file__).resolve().parents[1]
    source_files=['agent/context.py','agent/graph.py','agent/providers.py','agent/runtime.py','agent/scope.py',
        'agent/tools/screen_batches.py','agent/tools/context.py','jobs/screen_batches.py','jobs/ocr_handoff.py','jobs/native_ocr.py']
    source_binding={name:sha(platform_source/'src/workbench_platform'/name) for name in source_files}
    save(root/'platform-source-binding.json',source_binding)
    from packages.contracts.agent import AgentRef
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.jobs.screen_batches import ScreenSubmit,ScreenExport
    import workbench_platform.agent.runtime as runtime_module
    runtime=AgentRuntime(root/'agent',value);value.agent=runtime
    if args.check_contract:
        try:
            await check_contract(root,mapping,replay,value,fixed,link,runtime)
        finally:
            await runtime.close()
        return
    timings=[];original_request=runtime_module.request
    async def measured(config,key,payload,request_id,**kwargs):
        started=time.perf_counter();first=None;preview=kwargs.get('on_preview')
        async def on_preview(frame):
            nonlocal first
            if first is None:first=time.perf_counter()-started
            if preview:await preview(frame)
        kwargs['on_preview']=on_preview
        try:return await original_request(config,key,payload,request_id,**kwargs)
        finally:timings.append({'request_id':request_id,'wall_seconds':time.perf_counter()-started,
            'first_visible_preview_seconds':first,'thinking_seconds':None,
            'thinking_measurement':'供应商未提供独立思考计时；首个可见预览同时包含网络与生成等待'})
    controller=copy_controller(runtime,args.controller_store) if args.run_controller else None
    if not args.run_controller:
        save(root/'prepared.json',{'format':'screen-agent-replay-prepared-v1','scope_inputs':len(mapping),
            'independent_receipts':len({item['sample_id'] for item in mapping}),'external_calls':0,'gpu_calls':0,
            'production_tool_handlers_replaced':False,'status':'prepared-not-executed'})
        await runtime.close()
        return
    runtime_module.request=measured
    await runtime.start()
    reference=AgentRef(kind='asset_set',workspace_id='wreplay',object_id=fixed.asset_set_id,revision=fixed.revision,sha256=fixed.manifest_hash)
    session=runtime.store.create_session('wreplay','240 frozen receipt replay',[item.asset_id for item in fixed.items],'replaySession',[reference.model_dump()])
    goal=('请将任务固定范围内的'+str(len(mapping))+'张图片整批转成Excel，每图一个xlsx，同图多表分工作表，最后交付ZIP。'
        '这是已冻结OCR回执的流程性能回放，专业服务为回放器；仅使用hunyuan，max_upgrades=0，fallback_engines=[]，layout_evidence=false，mode=auto。'
        '直接使用已有固定集合，一次提交全批。等待交给后台，不逐图查询或思考。疑点集中列在交付清单，同时导出待复核版本(include_pending_review=true)，明确未经人工确认；无需逐图询问。')
    wait_samples=[];started=time.perf_counter();run=None
    directive_phase='initial';phase_started=None;directive_trace=[]
    try:
        run=await runtime.submit(session['id'],goal,'replayGoal')
        deadline=started+args.timeout
        while time.perf_counter()<deadline:
            current=runtime.store.run(run['id'])
            model_count=len(runtime.store.rows('SELECT id FROM model_requests WHERE run_id=?',(run['id'],)))
            if current['status']=='waiting_job' and (not args.exercise_directives or directive_phase.endswith('idle') or directive_phase=='initial'):
                wait_samples.append({'elapsed_seconds':time.perf_counter()-started,'model_requests':model_count,'phase':directive_phase})
            if args.exercise_directives and replay.batches:
                batch=next(iter(replay.batches.values()))
                pending=runtime.store.rows("SELECT * FROM operations WHERE run_id=? AND tool='ocr_screen_batch_run'",(run['id'],))
                retained=bool(pending and pending[0]['state']=='pending')
                deliveries=[json.loads(item['result']) for item in runtime.store.rows(
                    "SELECT result FROM operations WHERE run_id=? AND tool='delivery_publish' AND result IS NOT NULL",(run['id'],))]
                partial=any(item.get('data',{}).get('verified') and item.get('data',{}).get('continuing_job_refs') for item in deliveries)
                if directive_phase=='pause_sent' and batch['paused'] and current['status']=='waiting_job':
                    directive_phase='pause_idle';phase_started=time.perf_counter()
                    directive_trace.append({'action':'pause_ack','elapsed_seconds':phase_started-started,'original_operation_pending':retained})
                if directive_phase=='export_sent' and partial and current['status']=='waiting_job':
                    directive_phase='export_idle';phase_started=time.perf_counter()
                    directive_trace.append({'action':'partial_delivery_ack','elapsed_seconds':phase_started-started,'original_operation_pending':retained,
                        'partial_verified':True})
                if directive_phase=='resume_sent' and not batch['paused']:
                    replay.hold=0;directive_phase='completion_idle';phase_started=time.perf_counter()
                    directive_trace.append({'action':'resume_ack_release_frozen_wait','elapsed_seconds':phase_started-started,
                        'original_operation_pending':retained})
                if current['status']=='waiting_job' and directive_phase in {'initial','pause_idle','export_idle'}:
                    phase_started=phase_started or time.perf_counter()
                    if time.perf_counter()-phase_started>=3:
                        instructions={
                            'initial':('pause','暂停正在处理的原批次，保留已完成文件和原任务，不要重新提交。'),
                            'pause_idle':('export','先导出原批次目前已有的文件，明确包含未经人工复核的版本；立即交付部分ZIP，剩余原任务继续保留暂停状态，不要重新提交。'),
                            'export_idle':('resume','继续处理刚才暂停的原批次，保持原批次身份；完成后再交付整批ZIP。')}
                        action,content=instructions[directive_phase]
                        returned=await runtime.submit(session['id'],content,'directive'+action.title())
                        if returned['id']!=run['id']:raise ValueError('Directive created another durable run')
                        directive_trace.append({'action':action+'_sent','elapsed_seconds':time.perf_counter()-started,'model_requests':model_count})
                        directive_phase=action+'_sent';phase_started=None
                save(root/'directive-trace.json',directive_trace)
            if current['status'] not in {'running','waiting_job'}:break
            await asyncio.sleep(.5)
        wall=time.perf_counter()-started
        final=runtime.store.run(run['id']);operations=runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY created',(run['id'],))
        events=runtime.store.events(session['id'],limit=10000)['items']
        usages=[item['payload'] for item in events if item['kind']=='usage']
        delivery_rows=[json.loads(item['result']) for item in operations if item['tool']=='delivery_publish' and item['result']]
        export_receipts=[]
        for operation in operations:
            if operation['tool']!='ocr_screen_batch_export' or not operation['result']:continue
            result=json.loads(operation['result'])
            if result['status']!='success':continue
            for reference in result.get('artifact_refs',[]):
                artifact=value.artifact_center.file('wreplay',reference['object_id'],'ocr-results.zip')
                with value.artifact_center.store.stage_download('wreplay',artifact.artifact_id,
                    expected_revision=artifact.revision,expected_hash=artifact.blob_hash) as source:
                    with zipfile.ZipFile(source) as archive:
                        manifest=json.loads(archive.read('manifest.json'))
                        files=[name for name in archive.namelist() if name.endswith('.xlsx')]
                export_receipts.append({'bundle_id':reference['object_id'],'zip_sha256':artifact.blob_hash,
                    'xlsx_files':len(files),'manifest_success_files':manifest['success_files'],
                    'manifest_items':len(manifest['items']),
                    'exported_needs_review':sum(item.get('status')=='needs_review' and bool(item.get('file_name')) for item in manifest['items']),
                    'human_review':'not_performed'})
        native_submits=[item for item in replay.calls if item['method']=='POST' and item['path']=='/api/projects/replayProject/screen-batches']
        model_count=len(runtime.store.rows('SELECT id FROM model_requests WHERE run_id=?',(run['id'],)))
        wait_stable=all(b['model_requests']==a['model_requests'] for a,b in zip(wait_samples,wait_samples[1:]) if a['phase']==b['phase'])
        summary={'format':'screen-agent-real-controller-replay-v1','controller':controller,
            'execution':'frozen-native-http-replay','gpu_calls':0,'production_tool_handlers_replaced':False,
            'scope_inputs':len(mapping),'independent_frozen_receipts':len({item['sample_id'] for item in mapping}),
            'duplicates_are_not_quality_samples':True,'policy':'hunyuan/no-upgrades/no-layout replay only',
            'status':final['status'],'run_error':final.get('error'),
            'platform_source_binding_sha256':hashlib.sha256(encoded(source_binding).encode()).hexdigest(),
            'wall_seconds_including_harness_wait_and_local_durable_storage':wall,
            'injected_native_wait_seconds':300 if args.exercise_directives else args.hold_seconds,
            'directive_wait_released_on_resume':any(item['action']=='resume_ack_release_frozen_wait' for item in directive_trace),
            'model_rounds':model_count,'tool_calls':len(operations),
            'native_batch_submissions':len(native_submits),'model_http_post_attempts':len(timings),
            'model_request_retries':len(timings)-len({item['request_id'] for item in timings}),
            'input_tokens':sum(item.get('input_tokens') or 0 for item in usages),
            'output_tokens':sum(item.get('output_tokens') or 0 for item in usages),
            'reasoning_tokens':None,'model_requests':timings,'wait_samples':wait_samples,
            'waiting_controller_count_stable':wait_stable,'waiting_samples_observed':len(wait_samples),
            'planned_user_directives':3 if args.exercise_directives else 0,
            'additional_user_directives':sum(item['action'].endswith('_sent') for item in directive_trace),'directive_trace':directive_trace,
            'directive_checks':{'same_run':True,'pause_observed':any(item['action']=='pause_ack' for item in directive_trace),
                'partial_delivery_observed':any(item['action']=='partial_delivery_ack' for item in directive_trace),
                'resume_observed':any(item['action']=='resume_ack_release_frozen_wait' for item in directive_trace),
                'original_operation_retained':all(item.get('original_operation_pending',True) for item in directive_trace)} if args.exercise_directives else None,
            'verified_delivery':bool(delivery_rows and delivery_rows[-1].get('data',{}).get('verified')),
            'delivery_completeness':delivery_rows[-1].get('data',{}).get('completeness') if delivery_rows else None,
            'export_receipts':export_receipts,
            'targets_apply_to_no_directive_baseline':not args.exercise_directives,
            'target_rounds_le_5':model_count<=5 if not args.exercise_directives else None,
            'target_tools_le_8':len(operations)<=8 if not args.exercise_directives else None,
            'tools':[{'tool':item['tool'],'status':json.loads(item['result'])['status'] if item['result'] else item['state']} for item in operations]}
        save(root/'agent-summary.json',summary)
        save(root/'native-request-log.json',replay.calls)
        save(root/'usage.json',usages)
        # A direct production bridge call over the same frozen outputs measures
        # local import/export work, excluding actual OCR and controller time.
        replay.hold=0
        if not args.skip_direct_baseline:
            direct_started=time.perf_counter()
            direct=value.screen_batches.submit('wreplay',ScreenSubmit(link_id=link.link_id,asset_set_id=fixed.asset_set_id,
                asset_set_revision=fixed.revision,request_id='directReplay',policy={'primary_engine':'hunyuan','max_upgrades':0,'fallback_engines':[],'layout_evidence':False}))
            bundle=value.screen_batches.export('wreplay',direct['batch_id'],ScreenExport(include_pending_review=True))
            save(root/'direct-summary.json',{'format':'screen-direct-frozen-replay-v1','scope_inputs':len(mapping),
                'wall_seconds':time.perf_counter()-direct_started,'counts':direct['counts'],'bundle_id':bundle['bundle_id'],
                'actual_ocr_seconds':None,'model_calls':0,'gpu_calls':0,'handoff_cache_shared_with_agent':True})
        print(encoded({key:summary[key] for key in ('status','model_rounds','tool_calls','waiting_controller_count_stable','verified_delivery','target_rounds_le_5','target_tools_le_8')}))
    finally:
        if run and runtime.store.run(run['id'])['status'] in {'running','waiting_job','waiting_user'}:
            runtime.cancel(run['id'])
        await runtime.close();runtime_module.request=original_request


async def check_contract(root,mapping,replay,value,fixed,link,runtime):
    """Actual dispatcher and lifecycle against frozen HTTP, no controller stub."""
    from packages.contracts.agent import AgentRef
    reference=AgentRef(kind='asset_set',workspace_id='wreplay',object_id=fixed.asset_set_id,
        revision=fixed.revision,sha256=fixed.manifest_hash)
    session=runtime.store.create_session('wreplay','Frozen HTTP contract smoke',
        [item.asset_id for item in fixed.items],'contractSession',[reference.model_dump()])
    run,_=runtime.store.start_run(session['id'],'Verify frozen production bridge','contractGoal',0)
    state=runtime.initial_state(run,'Verify frozen production bridge')
    context=runtime.context(state)
    calls=[]
    async def dispatch(identifier,tool,arguments,expected='success'):
        result=await runtime.registry.dispatch({'call_id':identifier,'tool':tool,'arguments':arguments},context)
        calls.append({'tool':tool,'call_id':identifier,'status':result['status']})
        save(root/'contract-calls.json',calls)
        save(root/'native-request-log.json',replay.calls)
        if result['status']!=expected:
            raise ValueError('Contract smoke failed at '+tool+': '+encoded(result.get('error')))
        return result
    replay.hold=0
    prepared=await dispatch('contractPrepare','ocr_prepare',{'link_id':link.link_id})
    if prepared['data']['requires_project_selection'] or prepared['data']['project_id']!='replayProject':
        raise ValueError('Production preparation did not verify the frozen project')
    arguments={'link_id':link.link_id,'asset_set_id':fixed.asset_set_id,'asset_set_revision':fixed.revision,
        'policy':{'primary_engine':'hunyuan','max_upgrades':0,'fallback_engines':[],'layout_evidence':False}}
    submitted=await dispatch('contractRun','ocr_screen_batch_run',arguments,'pending')
    repeated=await dispatch('contractRun','ocr_screen_batch_run',arguments,'pending')
    if submitted!=repeated:raise ValueError('Duplicate dispatcher identity changed the submission receipt')
    job=submitted['job_refs'][0];batch_id=job['object_id']
    status=await dispatch('contractStatus','ocr_screen_batch_status',{'batch_id':batch_id,'status':'needs_review','limit':10})
    if status['data']['counts']['total']!=len(mapping) or status['data']['counts']['pending']:
        raise ValueError('Frozen terminal coverage did not reach the actual batch reader')
    settlement=await runtime.settle(submitted,state)
    if not settlement or settlement['coverage']['needs_review']!=len(mapping):
        raise ValueError('Production lifecycle did not preserve unreviewed frozen coverage')
    operation=runtime.store.rows("SELECT id FROM operations WHERE run_id=? AND tool='ocr_screen_batch_run'",(run['id'],))[0]
    runtime.store.finish_operation(operation['id'],settlement)
    exported=await dispatch('contractExport','ocr_screen_batch_export',{'batch_id':batch_id,'include_pending_review':True})
    again=await dispatch('contractExportRetry','ocr_screen_batch_export',{'batch_id':batch_id,'include_pending_review':True})
    if again['artifact_refs']!=exported['artifact_refs']:
        raise ValueError('Semantic export retry published another bundle')
    delivered=await dispatch('contractDelivery','delivery_publish',{'summary':'冻结回执流程验证；文件未经人工确认',
        'artifact_refs':exported['artifact_refs']})
    if delivered['data']['verified'] is not True or delivered['data']['completeness']!='partial':
        raise ValueError('Production delivery incorrectly declared complete human verification')
    bundle=exported['artifact_refs'][0]['object_id']
    artifact=value.artifact_center.file('wreplay',bundle,'ocr-results.zip')
    with value.artifact_center.store.stage_download('wreplay',artifact.artifact_id,
            expected_revision=artifact.revision,expected_hash=artifact.blob_hash) as path:
        with zipfile.ZipFile(path) as archive:
            manifest=json.loads(archive.read('manifest.json'))
            files=[name for name in archive.namelist() if name.endswith('.xlsx')]
    submits=sum(item['method']=='POST' and item['path']=='/api/projects/replayProject/screen-batches' for item in replay.calls)
    if submits!=1 or len(files)!=len(mapping) or manifest['success_files']!=len(mapping):
        raise ValueError('Contract smoke submission/export mapping is incomplete')
    summary={'format':'screen-agent-production-contract-smoke-v1','scope_inputs':len(mapping),
        'external_calls':0,'gpu_calls':0,'production_tool_handlers_replaced':False,'native_batch_submissions':submits,
        'xlsx_files':len(files),'verified_delivery':True,'delivery_completeness':'partial',
        'human_review':'not_performed','duplicates_are_not_quality_samples':True,'tools':calls,'status':'passed'}
    save(root/'contract-summary.json',summary)
    save(root/'native-request-log.json',replay.calls)
    runtime.store.status(run['id'],run['generation'],'partial')
    print(encoded({key:summary[key] for key in ('status','scope_inputs','external_calls','gpu_calls','xlsx_files','native_batch_submissions','verified_delivery')}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--receipts',type=Path,required=True)
    parser.add_argument('--ocr-source',type=Path,required=True)
    parser.add_argument('--agent-layer',type=Path,default=Path('F:/OCR/output/platform-agent-dev-20261004/dependencies/agent'))
    parser.add_argument('--image-layer',type=Path,default=Path('F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr/runtimes/control/Lib/site-packages'))
    parser.add_argument('--controller-store',type=Path,default=Path('F:/OCR/output/ai-audit-product-dev-20261004/workspace/agent/operations.sqlite3'))
    parser.add_argument('--count',type=int,default=240)
    parser.add_argument('--hold-seconds',type=float,default=30)
    parser.add_argument('--timeout',type=float,default=600)
    parser.add_argument('--run-controller',action='store_true')
    parser.add_argument('--check-contract',action='store_true',help='Exercise production tools/lifecycle on frozen HTTP with zero controller calls')
    parser.add_argument('--exercise-directives',action='store_true',help='Actual natural-language pause, partial export, resume in the original running task')
    parser.add_argument('--skip-direct-baseline',action='store_true',help='Do not repeat the optional local direct-export baseline')
    args=parser.parse_args()
    if not 1<=args.count<=1000 or not 1<=args.hold_seconds<=300:raise ValueError('Replay scope or wait out of bounds')
    if args.check_contract and (args.run_controller or args.count>8):
        raise ValueError('Contract smoke uses at most 8 inputs and cannot run the controller')
    if args.exercise_directives and (not args.run_controller or args.count<2):
        raise ValueError('Directive replay requires the actual controller and at least two inputs')
    root=configure(args)
    if (root/'workspaces.sqlite3').exists():raise ValueError('Use a new isolated replay output directory')
    mapping=freeze(args,root);replay=ReplayService(root,mapping,300 if args.exercise_directives else args.hold_seconds,
        early_count=max(1,min(12,args.count//3)) if args.exercise_directives else 0)
    keeper=None
    try:
        value,fixed,link,keeper=services(root,mapping,replay)
        asyncio.run(evaluate(args,root,mapping,replay,value,fixed,link))
    finally:
        if keeper:keeper.close()
        replay.close()


if __name__=='__main__':main()
