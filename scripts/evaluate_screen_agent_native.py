"""Prepare or execute the production Agent over a real native OCR queue.

Default preparation is CPU-only and never creates an inference service or
controller request. --run starts create_app, TaskQueue and EngineAdapter using
the explicit frozen source and read-only shared bundle. Ground truth and frozen
recognition receipts are never loaded by this harness.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace
from urllib.parse import urlsplit

sys.dont_write_bytecode = True


def f_path(value):
    path=Path(value).absolute()
    if path.drive.upper()!='F:' or path.resolve().drive.upper()!='F:':
        raise ValueError('Native evaluation paths must resolve to local F:')
    return path


def sha(path):
    with f_path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def save(path,value):
    f_path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def configure(args):
    root=f_path(args.output);root.mkdir(parents=True,exist_ok=True)
    temporary=root/'temp';temporary.mkdir(exist_ok=True)
    for name in ('TEMP','TMP','TMPDIR','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME','MPLCONFIGDIR'):
        os.environ[name]=str(temporary)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    os.environ['WORKBENCH_AGENT_LAYER']=str(f_path(args.agent_layer))
    sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'src')]
    from workbench_platform.supervisor.processes import storage_environment
    os.environ.update(storage_environment(root))
    return root


def inventory(source):
    source=f_path(source)
    if not (source/'src/ocr_workbench/service.py').is_file() or not (source/'config/screen-policy.json').is_file():
        raise ValueError('Frozen native source must include src and config/screen-policy.json')
    files={}
    for name in ('src','config'):
        for path in sorted((source/name).rglob('*')):
            if path.is_symlink() or path.is_junction():raise ValueError('Frozen source must contain actual local files')
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                files[path.relative_to(source).as_posix()]=sha(path)
    return files


def prepare(args,root):
    inputs=f_path(args.inputs);source=f_path(args.native_source);bundle=f_path(args.bundle)
    originals=json.loads(inputs.read_text(encoding='utf-8'))
    samples=originals['samples']
    if not 1<=len(samples)<=1000:raise ValueError('Expected 1–1000 independently frozen inputs')
    folder=root/'inputs';folder.mkdir(exist_ok=True)
    mapping=[]
    for index,sample in enumerate(samples):
        original=f_path(sample['image'])
        if sha(original)!=sample['image_sha256']:raise ValueError('A frozen source image digest changed')
        target=folder/f'{index+1:04d}-{sample["id"]}{original.suffix.lower()}'
        shutil.copyfile(original,target)
        if sha(target)!=sample['image_sha256']:raise ValueError('Input copy digest does not match original')
        mapping.append({'position':index,'sample_id':sample['id'],'source_image_sha256':sample['image_sha256'],
            'normalized_input':str(target),'normalized_png_sha256':sample['image_sha256']})
    files=inventory(source)
    engines=bundle/'config/engines.json'
    if not engines.is_file() or not (bundle/'runtimes/control/python.exe').is_file():
        raise ValueError('Read-only native bundle is incomplete')
    policy=json.loads((source/'config/screen-policy.json').read_text(encoding='utf-8'))
    if policy.get('visual_review'):raise ValueError('Batch visual review is not enabled in the native production contract')
    context=None
    if args.context_manifest:
        path=f_path(args.context_manifest)
        context={'path':str(path),'sha256':sha(path),'use':'comparison reference only; not injected into queue results'}
    binding={'format':'screen-agent-actual-native-prepared-v1','inputs':str(inputs),'inputs_sha256':sha(inputs),
        'input_count':len(mapping),'native_source':str(source),'native_source_files':files,
        'native_source_inventory_sha256':hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'bundle':str(bundle),'bundle_engines_sha256':sha(engines),'screen_policy':policy,
        'screen_policy_sha256':sha(source/'config/screen-policy.json'),'context_reference':context,
        'truth_read':False,'frozen_results_loaded':False,'external_calls':0,'gpu_calls':0,'prepared_only':not args.run}
    save(root/'prepared.json',binding);save(root/'input-map.json',{'items':mapping})
    return mapping,binding


def native_http(service,method,path,body=None):
    parsed=urlsplit(service.base_url);connection=http.client.HTTPConnection('127.0.0.1',parsed.port,timeout=30)
    payload=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
    try:
        connection.request(method,path,body=payload,headers={'Authorization':'Bearer '+service.token,
            'Content-Type':'application/json','Accept':'application/json'})
        response=connection.getresponse();content=response.read(16*1024*1024+1)
        if response.status!=200 or len(content)>16*1024*1024:
            raise ValueError('Actual native HTTP did not provide a verified JSON reply')
        return json.loads(content)
    finally:connection.close()


def server_mode(args):
    root=configure(args);source=f_path(args.native_source);bundle=f_path(args.bundle)
    sys.path[:0]=[str(source/'src'),str(f_path(args.pdf_layer).parent)]
    from ocr_workbench.service import create_app
    import uvicorn
    token=(root/'native-token.txt').read_text(encoding='utf-8')
    # No fake queue/adapter factory, no selection or quality overrides.
    app=create_app(bundle,root/'native/ocr',token,start_queue=True,agent_enabled=False)
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


async def execute(args,root,mapping,binding):
    from scripts.evaluate_screen_agent_replay import copy_controller,services
    from packages.contracts.agent import AgentRef
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.jobs.screen_batches import ScreenExport
    import workbench_platform.agent.runtime as runtime_module
    if inventory(args.native_source)!=binding['native_source_files']:
        raise ValueError('Source changed after preparation')
    receipts=root/'native-receipts';receipts.mkdir()
    token=secrets.token_urlsafe(36);(root/'native-token.txt').write_text(token,encoding='utf-8')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    command=[str(f_path(args.bundle)/'runtimes/control/python.exe'),'-I','-B','-X','utf8',str(Path(__file__).resolve()),
        '--serve-native','--output',str(root),'--inputs',str(f_path(args.inputs)),
        '--native-source',str(f_path(args.native_source)),'--bundle',str(f_path(args.bundle)),
        '--pdf-layer',str(f_path(args.pdf_layer)),'--port',str(port)]
    service=SimpleNamespace(base_url=f'http://127.0.0.1:{port}',token=token,asset_map={})
    keeper=None;runtime=None;run=None;timings=[];native_process=None
    original_request=runtime_module.request
    async def measured(config,key,payload,request_id,**kwargs):
        start=time.perf_counter();first=None;preview=kwargs.get('on_preview')
        async def frame(value):
            nonlocal first
            if first is None:first=time.perf_counter()-start
            if preview:await preview(value)
        kwargs['on_preview']=frame
        try:return await original_request(config,key,payload,request_id,**kwargs)
        finally:timings.append({'request_id':request_id,'wall_seconds':time.perf_counter()-start,
            'first_visible_preview_seconds':first,'thinking_seconds':None})
    with (root/'native-service.log').open('wb') as log:
        try:
            native_process=subprocess.Popen(command,cwd=root,env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline=time.perf_counter()+90
            while time.perf_counter()<deadline:
                if native_process.poll() is not None:raise ValueError('Actual native service exited; inspect isolated log')
                try:
                    health=await asyncio.to_thread(native_http,service,'GET','/api/health')
                    if health.get('status')=='ready' and health.get('queue',{}).get('healthy'):break
                except (OSError,ValueError):pass
                await asyncio.sleep(.5)
            else:raise ValueError('Actual native queue did not become healthy')
            project=await asyncio.to_thread(native_http,service,'POST','/api/projects',
                {'name':'Actual native OCR Agent benchmark','request_id':'nativeProject20261007'})
            service.project_id=project['id']
            value,fixed,link,keeper=services(root,mapping,service)
            runtime=AgentRuntime(root/'agent',value);value.agent=runtime
            controller=copy_controller(runtime,args.controller_store)
            runtime_module.request=measured;await runtime.start()
            reference=AgentRef(kind='asset_set',workspace_id='wreplay',object_id=fixed.asset_set_id,
                revision=fixed.revision,sha256=fixed.manifest_hash)
            session=runtime.store.create_session('wreplay','Actual native screen batch',
                [item.asset_id for item in fixed.items],'nativeSession',[reference.model_dump()])
            goal=('将固定范围内的'+str(len(mapping))+'张图片整批转Excel，每图一个xlsx，同图多表分工作表，最后给ZIP。'
                '这是实际屏拍模型评估；使用服务当前默认自动批次策略，一次提交全批，后台等待，不逐图思考和查询。'
                '疑点集中列清单；明确包含未经人工确认的待复核版本导出(include_pending_review=true)，不要自动签人工确认，不使用外部视觉复核。')
            start=time.perf_counter();run=await runtime.submit(session['id'],goal,'nativeGoal')
            wait_samples=[];deadline=start+args.timeout
            while time.perf_counter()<deadline:
                current=runtime.store.run(run['id'])
                if current['status']=='waiting_job':
                    wait_samples.append({'elapsed_seconds':time.perf_counter()-start,
                        'model_requests':len(runtime.store.rows('SELECT id FROM model_requests WHERE run_id=?',(run['id'],)))})
                if current['status'] not in {'running','waiting_job'}:break
                await asyncio.sleep(1)
            final=runtime.store.run(run['id'])
            operations=runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY created',(run['id'],))
            batch_rows=value.screen_batches._connect()
            try:batch_ids=[row[0] for row in batch_rows.execute('SELECT batch_id FROM screen_batch_refs WHERE batch_id IS NOT NULL')]
            finally:batch_rows.close()
            native_batches=[]
            for batch_id in batch_ids:
                summary=await asyncio.to_thread(value.screen_batches.status,'wreplay',batch_id,limit=1)
                items=[]
                for offset in range(0,summary['counts']['total'],30):
                    page=await asyncio.to_thread(value.screen_batches.status,'wreplay',batch_id,offset=offset,limit=30)
                    for item in page['items']:
                        detail=await asyncio.to_thread(value.screen_batches.detail,'wreplay',batch_id,item['input_version_id'])
                        sample=next(entry for entry in mapping if entry.get('asset_id')==item['asset_id'])
                        detail.update(sample_id=sample['sample_id'],source_image_sha256=sample['source_image_sha256'])
                        save(receipts/(sample['sample_id']+'.json'),detail);items.append(item)
                export_receipt=None
                if summary['counts']['completed']+summary['counts']['needs_review']:
                    bundle=await asyncio.to_thread(value.screen_batches.export,'wreplay',batch_id,ScreenExport(include_pending_review=True))
                    artifact=value.artifact_center.file('wreplay',bundle['bundle_id'],'ocr-results.zip')
                    with value.artifact_center.store.stage_download('wreplay',artifact.artifact_id,
                        expected_revision=artifact.revision,expected_hash=artifact.blob_hash) as source:
                        shutil.copyfile(source,root/(batch_id+'.zip'))
                        with zipfile.ZipFile(source) as archive:
                            manifest=json.loads(archive.read('manifest.json'))
                            files=[name for name in archive.namelist() if name.endswith('.xlsx')]
                    export_receipt={'bundle_id':bundle['bundle_id'],'zip_sha256':artifact.blob_hash,
                        'xlsx_files':len(files),'manifest_success_files':manifest['success_files'],
                        'manifest_items':len(manifest['items']),
                        'exported_needs_review':sum(item.get('status')=='needs_review' and bool(item.get('file_name')) for item in manifest['items'])}
                actual_policy=summary.get('policy',{})
                policy_fields={'strategy_version','primary_engine','group_size','max_upgrades','layout_evidence',
                    'budget_seconds','batch_budget_seconds','mode','perspective','allow_text_fixes','visual_review'}
                policy_comparison={key:{'expected':expected,'actual':actual_policy.get(key),
                    'matches':actual_policy.get(key)==expected} for key,expected in binding['screen_policy'].items() if key in policy_fields}
                if 'fallback_engines' in binding['screen_policy']:
                    expected=[engine for engine in binding['screen_policy']['fallback_engines'] if engine!=actual_policy.get('primary_engine')]
                    policy_comparison['fallback_engines']={'expected':expected,'actual':actual_policy.get('fallback_engines'),
                        'matches':actual_policy.get('fallback_engines')==expected}
                native_batches.append({'batch_id':batch_id,'counts':summary['counts'],'status':summary['status'],'items':items,
                    'policy':actual_policy,'resolved_policy_sha256':summary.get('resolved_policy_sha256'),
                    'frozen_policy_comparison':policy_comparison,
                    'frozen_policy_settings_match':all(field['matches'] for field in policy_comparison.values()),
                    'export_receipt':export_receipt,'human_review':'not_performed'})
            events=runtime.store.events(session['id'],limit=10000)['items']
            usage=[item['payload'] for item in events if item['kind']=='usage']
            delivered=[json.loads(item['result']) for item in operations if item['tool']=='delivery_publish' and item['result']]
            summary={'format':'screen-agent-actual-native-result-v1','status':final['status'],
                'run_error':final.get('error'),'inputs_sha256':binding['inputs_sha256'],'scope_inputs':len(mapping),
                'native_source_inventory_sha256':binding['native_source_inventory_sha256'],
                'screen_policy_sha256':binding['screen_policy_sha256'],'controller':controller,
                'truth_read':False,'frozen_results_loaded':False,'real_queue_and_engine_adapter':True,
                'shared_context_injected':False,'context_reference':binding['context_reference'],
                'wall_seconds':time.perf_counter()-start,'model_rounds':len(runtime.store.rows('SELECT id FROM model_requests WHERE run_id=?',(run['id'],))),
                'tool_calls':len(operations),'input_tokens':sum(item.get('input_tokens') or 0 for item in usage),
                'output_tokens':sum(item.get('output_tokens') or 0 for item in usage),'model_requests':timings,
                'thinking_seconds':None,'thinking_measurement':'供应商未提供独立思考计时',
                'waiting_controller_count_stable':all(a['model_requests']==b['model_requests'] for a,b in zip(wait_samples,wait_samples[1:])),
                'wait_samples':wait_samples,'native_batches':native_batches,
                'verified_delivery':bool(delivered and delivered[-1].get('data',{}).get('verified')),
                'delivery_completeness':delivered[-1].get('data',{}).get('completeness') if delivered else None}
            save(root/'agent-native-summary.json',summary);save(root/'usage.json',usage)
            print(json.dumps({key:summary[key] for key in ('status','scope_inputs','model_rounds','tool_calls','verified_delivery')},ensure_ascii=False))
        finally:
            if runtime:
                if run and runtime.store.run(run['id'])['status'] in {'running','waiting_job','waiting_user'}:
                    runtime.cancel(run['id'])
                await runtime.close()
            runtime_module.request=original_request
            if keeper:keeper.close()
            if native_process and native_process.poll() is None:
                # Terminate only our isolated service; its native queues stop
                # resident EngineAdapter children through their process jobs.
                native_process.terminate()
                try:native_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    native_process.kill();native_process.wait(timeout=15)
    # After the service stops, preserve authoritative task/result/provenance
    # tables beside existing native files for separate offline scoring.
    database=root/'native/ocr/workbench.sqlite3'
    if database.is_file():
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ('tasks','results','versions','screen_batches','screen_items','screen_tasks','screen_contexts','screen_candidates'):
                if name in names:save(root/'native-receipts'/(name+'.json'),[dict(row) for row in db.execute('SELECT * FROM '+name)])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('output','inputs','native-source','bundle'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--agent-layer',type=Path,default=Path('F:/OCR/output/platform-agent-dev-20261004/dependencies/agent'))
    parser.add_argument('--pdf-layer',type=Path,default=Path('F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/build/public-quality-20260916/tool/site-packages/pdfplumber'))
    parser.add_argument('--controller-store',type=Path,default=Path('F:/OCR/output/ai-audit-product-dev-20261004/workspace/agent/operations.sqlite3'))
    parser.add_argument('--context-manifest',type=Path)
    parser.add_argument('--timeout',type=float,default=14400)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--serve-native',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--port',type=int,default=0,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.serve_native:
        if not 1<=args.port<=65535:raise ValueError('Invalid isolated native port')
        server_mode(args);return
    root=configure(args)
    if (root/'prepared.json').exists() or (root/'workspaces.sqlite3').exists():
        raise ValueError('Use a new isolated output; existing evidence is never overwritten')
    mapping,binding=prepare(args,root)
    if args.run:asyncio.run(execute(args,root,mapping,binding))
    else:print(json.dumps({'status':'prepared','scope_inputs':len(mapping),'external_calls':0,'gpu_calls':0,'truth_read':False}))


if __name__=='__main__':main()
