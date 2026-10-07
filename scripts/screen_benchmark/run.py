"""Resumable, real EngineAdapter benchmark. No labels or ideal ROIs reach inference."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

def isolate_runtime(output):
    runtime_root=fpath(output)/'runtime';runtime_root.mkdir(parents=True,exist_ok=True)
    variables={'TEMP':'temp','TMP':'temp','APPDATA':'appdata','LOCALAPPDATA':'localappdata','PIP_CACHE_DIR':'cache/pip','HF_HOME':'cache/huggingface','HF_HUB_CACHE':'cache/huggingface/hub','HUGGINGFACE_HUB_CACHE':'cache/huggingface/hub','TRANSFORMERS_CACHE':'cache/huggingface/transformers','TORCH_HOME':'cache/torch','TORCH_EXTENSIONS_DIR':'cache/torch-extensions','PADDLE_PDX_CACHE_HOME':'cache/paddlex','PADDLE_HOME':'cache/paddle','CUDA_CACHE_PATH':'cache/cuda','NUMBA_CACHE_DIR':'cache/numba','TRITON_CACHE_DIR':'cache/triton','XDG_CACHE_HOME':'cache','MPLCONFIGDIR':'cache/matplotlib'}
    for key,relative in variables.items():target=runtime_root/relative;target.mkdir(parents=True,exist_ok=True);os.environ[key]=str(target)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def fpath(path):
    p=Path(path).resolve()
    if p.drive.upper()!='F:':raise ValueError(f'F-only project path: {p}')
    return p

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n','utf-8');temporary.replace(path)

def source_hash(root):
    rows=[]
    for path in sorted(Path(root).rglob('*.py')):rows.append([path.relative_to(root).as_posix(),digest(path)])
    return hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()

def gpu_sample():
    try:
        p=subprocess.run(['nvidia-smi','--query-gpu=name,memory.used,memory.total,utilization.gpu','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=8,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return {'time':time.time(),'observed':p.stdout.strip(),'exit_code':p.returncode}
    except Exception as e:return {'time':time.time(),'unavailable':str(e)}

def first_pass_images(metadata, selector=None):
    if selector is not None:return selector(metadata)
    images=metadata['prepared_images'];crops=[p for p in images if p['kind']=='table_crop']
    if len(crops)>1:return crops
    selected=[p for p in images if p['id']==metadata['primary_id']]
    if len(selected)!=1:raise ValueError('Preprocessing has no unique primary image')
    return selected

def aggregate_results(results, prepared):
    if len(results)==1:return results[0]
    tables=[];blocks=[]
    for index,result in enumerate(results):
        for table in result.get('tables',[]):tables.append({**table,'prepared_image_id':prepared[index]['id'],'region_id':prepared[index].get('region_id')})
        blocks.extend(result.get('blocks',[]))
    return {**results[0],'text':'\n\n'.join(r.get('text','') for r in results),'tables':tables,'blocks':blocks,
            'raw':{'kind':'disjoint_detected_regions','regions':[{'prepared':p,'result':r} for p,r in zip(prepared,results)]},
            'warnings':[warning for r in results for warning in r.get('warnings',[])],
            'elapsed_seconds':sum(r.get('elapsed_seconds',0) for r in results)}

def validate_context_binding(context_binding,binding,source_policy='exact'):
    if source_policy not in {'exact','recorded'}:raise ValueError('Unsupported context source policy')
    fields=('inputs_sha256','engine_config_sha256') if source_policy=='recorded' else ('inputs_sha256','worker_source_sha256','engine_config_sha256')
    for field in fields:
        if context_binding.get(field)!=binding[field]:raise ValueError(f'Context {field} differs')
    if source_policy=='recorded':
        if not context_binding.get('worker_source_sha256'):raise ValueError('Context source identity missing')
        if binding.get('context_worker_source_sha256') is not None and binding['context_worker_source_sha256']!=context_binding['worker_source_sha256']:
            raise ValueError('Recorded context worker source differs')
    if context_binding.get('stage')!='screen-context' or context_binding.get('geometry_provider')!='context' or context_binding.get('labels_or_ideal_regions_provided') is not False:
        raise ValueError('Only verified inference context permitted')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--worker-source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--engine',choices=['hunyuan','paddlevl','glm','ppocr'],required=True)
    p.add_argument('--limit',type=int);p.add_argument('--offset',type=int,default=0);p.add_argument('--retry-failed',action='store_true')
    p.add_argument('--preprocess-module',help='Optional module name or .py path; prepare_screen_image(image, output, evidence=None, perspective=True); receives no truth')
    p.add_argument('--evidence-run',type=Path,help='Verified real context.py output on exactly these inputs and worker source')
    p.add_argument('--context-source-policy',choices=['exact','recorded'],default='exact',help='recorded explicitly reuses old real context across a new CPU/worker source; keeps both source identities, requires identical inputs/config')
    a=p.parse_args();a.output=fpath(a.output);a.bundle=fpath(a.bundle);a.worker_source=fpath(a.worker_source);a.inputs=fpath(a.inputs)
    if a.offset<0 or a.limit is not None and a.limit<1:raise ValueError('Offset must be nonnegative and limit positive')
    if a.evidence_run and not a.preprocess_module:raise ValueError('Context evidence requires screen preprocessing')
    a.output.mkdir(parents=True,exist_ok=True)
    # Explicitly redirect all supported process and child cache/temp locations.
    isolate_runtime(a.output)
    sys.path.insert(0,str(a.worker_source))
    from ocr_workbench.adapter import EngineAdapter
    inputs=json.loads(a.inputs.read_text('utf-8'))
    if inputs.get('truth_available_to_inference') is not False:raise ValueError('Only isolated model-input manifest permitted')
    samples=inputs['samples'][a.offset:a.offset+a.limit if a.limit else None]
    if len({s['id'] for s in samples})!=len(samples):raise ValueError('Duplicate sample IDs')
    for s in samples:
        fpath(s['image'])
        if digest(s['image'])!=s['image_sha256']:raise ValueError('Input hash mismatch')
        if Path(s['id']).name!=s['id'] or s['id'] in ('','.','..'):raise ValueError('Unsafe sample ID')
    preprocess=None;preprocess_file=None;assess=None;map_result=None;normalize=None;verify_context=None;context_binding=None;selector=None
    if a.preprocess_module:
        if a.preprocess_module.endswith('.py'):
            preprocess_file=fpath(a.preprocess_module);spec=importlib.util.spec_from_file_location('reviewed_screen_preprocess',preprocess_file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        else:module=importlib.import_module(a.preprocess_module);preprocess_file=Path(module.__file__)
        preprocess=module.prepare_screen_image
        selector=getattr(module,'select_screen_first_pass',None)
        assess=importlib.import_module('ocr_workbench.screen_quality').assess_screen_result
        map_result=importlib.import_module('ocr_workbench.screen_quality').map_screen_result_to_source
    binding={'inputs_sha256':digest(a.inputs),'worker_source_sha256':source_hash(a.worker_source),'engine':a.engine,
             'engine_config_sha256':digest(a.bundle/'config/engines.json'),'bundle':str(a.bundle),
             'offset':a.offset,'limit':a.limit,'preprocess_module_sha256':digest(preprocess_file) if preprocess_file else None,
             'first_pass':True,'labels_or_ideal_regions_provided':False}
    if a.evidence_run:
        a.evidence_run=fpath(a.evidence_run)
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from context import verify_context_record
        verify_context=verify_context_record
        normalize=importlib.import_module('ocr_workbench.screen_evidence').normalize_screen_context if a.context_source_policy=='exact' else None
        context_path=a.evidence_run/'binding.json';context_binding=json.loads(context_path.read_text('utf-8'))
        validate_context_binding(context_binding,binding,source_policy=a.context_source_policy)
        binding.update(context_binding_sha256=digest(context_path),context_run=str(a.evidence_run))
        if a.context_source_policy=='recorded':
            binding.update(context_source_policy='recorded',context_worker_source_sha256=context_binding['worker_source_sha256'],
                           context_evidence_policy='stored normalized evidence hash verified; new CPU preparation reconciles it independently',
                           runner_sha256=digest(__file__))
    previous=a.output/'binding.json'
    if previous.exists() and json.loads(previous.read_text('utf-8'))!=binding:raise ValueError('Cannot mix immutable benchmark/model/source identities; select a new output directory')
    if not previous.exists():write(previous,binding)
    stop=threading.Event();gpu=[]
    def monitor():
        while not stop.is_set():gpu.append(gpu_sample());stop.wait(2)
    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    adapter=None;started=time.perf_counter();loads=[];finished=0;resumed=0
    try:
        for sample in samples:
            folder=a.output/sample['id'];record_path=folder/'record.json'
            old=json.loads(record_path.read_text('utf-8')) if record_path.exists() else None
            if old and old.get('image_sha256')!=sample['image_sha256']:raise ValueError('Resume input identity differs')
            if old and (old.get('status')=='success' or old.get('status')=='failed' and not a.retry_failed):
                if old.get('status')=='success' and (not (folder/'result.json').exists() or digest(folder/'result.json')!=old['result_sha256']):raise ValueError('Saved success result changed')
                if old.get('status')=='success' and verify_context:
                    context_record,_,_=verify_context(a.evidence_run/sample['id'],sample,normalize=normalize)
                    if old.get('context_evidence_sha256')!=context_record['evidence_sha256'] or old.get('context_sha256')!=context_record['context_sha256']:raise ValueError('Consumed context changed')
                finished+=1;resumed+=1;continue
            attempt_number=1+len(list(folder.glob('attempt-*'))) if folder.exists() else 1
            attempt=folder/f'attempt-{attempt_number:04d}';attempt.mkdir(parents=True)
            record={'id':sample['id'],'image_sha256':sample['image_sha256'],'engine':a.engine,'status':'running','attempt':attempt_number,'output':str(attempt)}
            write(record_path,record);begin=time.perf_counter();prepared=Path(sample['image']);quality=None;preprocessing_seconds=0
            prepared_items=[{'id':'original','path':str(prepared),'kind':'original'}];prepared_info=None;evidence=None
            try:
                if verify_context:
                    context_record,context,evidence=verify_context(a.evidence_run/sample['id'],sample,normalize=normalize)
                    record.update(context_sha256=context_record['context_sha256'],context_evidence_sha256=context_record['evidence_sha256'],context_wall_seconds=context_record['wall_seconds'],context_inference_seconds=context_record.get('inference_seconds'),context_model_load_seconds=context_record.get('model_load_seconds'))
                    write(attempt/'consumed-context.json',{'binding':context_binding,'record':context_record,'evidence':evidence})
                if preprocess:
                    tick=time.perf_counter();prepared_info=preprocess(prepared,attempt/'preprocess',evidence=evidence,perspective=True);prepared_items=first_pass_images(prepared_info,selector);quality=prepared_info.get('quality');preprocessing_seconds=time.perf_counter()-tick
                    write(attempt/'preprocess.json',prepared_info)
                if adapter is None:
                    adapter=EngineAdapter(a.bundle,a.engine,a.output/'sessions',worker_source=a.worker_source);tick=time.perf_counter();adapter.load();loads.append({'session_id':adapter.ipc.name,'wall_seconds':time.perf_counter()-tick,'reported_load_seconds':adapter.load_seconds})
                results=[];tick=time.perf_counter()
                for index,item in enumerate(prepared_items):
                    prepared=fpath(item['path']);destination=attempt/f'region-{index:02d}';destination.mkdir()
                    raw_result=adapter.recognize(prepared,destination,allow_empty=True)
                    results.append(map_result(raw_result,item) if map_result else raw_result)
                inference_seconds=time.perf_counter()-tick;result=aggregate_results(results,prepared_items)
                if assess:
                    quality=assess(result,image_info=prepared_info,text_evidence={'blocks':evidence['text'],'provenance':evidence['provenance']} if evidence else None)
                    write(attempt/'quality.json',quality)
                    result['quality']=quality
                record.update(status='success',engine_session=result.get('engine_session'),inference_seconds=inference_seconds,model_load_seconds=result.get('load_seconds'),prepared_images=[{**item,'sha256':digest(item['path'])} for item in prepared_items],quality=quality)
                # Preserve engine-produced raw output. Score its returned representation unchanged.
                write(folder/'result.json',result);record['result_sha256']=digest(folder/'result.json')
            except Exception as e:
                record.update(status='failed',error_type=type(e).__name__,error=str(e),quality=quality)
                if adapter is not None:adapter.unload();adapter=None
            finally:
                record.update(wall_seconds=time.perf_counter()-begin,preprocessing_seconds=preprocessing_seconds)
                write(attempt/'record.json',record);write(record_path,record);finished+=1
                write(a.output/'progress.json',{'binding':binding,'completed':finished,'total':len(samples),'last':record,'wall_seconds':time.perf_counter()-started})
                print(json.dumps({'completed':finished,'total':len(samples),'id':sample['id'],'status':record['status'],'seconds':round(record['wall_seconds'],3),'error':record.get('error')},ensure_ascii=False),flush=True)
    finally:
        if adapter is not None:adapter.unload()
        stop.set();thread.join(timeout=12);write(a.output/'gpu-samples.json',gpu)
        records=[json.loads((a.output/s['id']/'record.json').read_text('utf-8')) for s in samples if (a.output/s['id']/'record.json').exists()]
        write(a.output/'run-summary.json',{'binding':binding,'attempt_wall_seconds':time.perf_counter()-started,'completed':sum(r['status'] in {'success','failed'} for r in records),'total':len(samples),'success':sum(r['status']=='success' for r in records),'failed':sum(r['status']=='failed' for r in records),'loads':loads,'gpu_peak_note':'device-wide nvidia-smi samples include other applications; no per-process peak inferred','resumed_records':resumed,'cache_resume_separate':bool(resumed)})

if __name__=='__main__':main()
