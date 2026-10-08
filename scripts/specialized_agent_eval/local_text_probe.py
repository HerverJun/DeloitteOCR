"""Explicit GLM local-text development experiment, isolated from native selection."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import importlib
import os
from pathlib import Path
import shutil
import sys
import time
from common import digest,fingerprint,fpath,frozen_json,read_json
from local_cell_probe import check_bound_file,validate_prepared

FORMAT='development-explicit-local-text-probe-v1'
VARIANT_FILES={'src/ocr_workbench/adapter.py','src/ocr_workbench/engine_host.py',
    'src/ocr_workbench/worker.py','src/ocr_workbench/glm.py'}
NEW_MODULE='src/ocr_workbench/recognition_modes.py'


@contextmanager
def variant_source(source):
    source=fpath(source)
    previous={name:value for name,value in sys.modules.items() if name=='ocr_workbench' or name.startswith('ocr_workbench.')}
    for name in previous:
        sys.modules.pop(name)
    sys.path.insert(0,str(source/'src'))
    try:
        yield
    finally:
        sys.path.pop(0)
        for name in list(sys.modules):
            if name=='ocr_workbench' or name.startswith('ocr_workbench.'):
                sys.modules.pop(name)
        sys.modules.update(previous)


def prepare(base_path,variant,source_output,output):
    base_path,variant,source_output,output=map(fpath,(base_path,variant,source_output,output))
    if output.exists() or source_output.exists():
        raise ValueError('Local text source/run exists; use a new immutable experiment')
    base=read_json(base_path)
    validate_prepared(base)
    expected=base['engine_source_files']
    files={relative:digest(variant/relative) for relative in [*expected,NEW_MODULE]}
    if {key for key,sha in files.items() if key in expected and sha!=expected[key]}!=VARIANT_FILES:
        raise ValueError('Local text variant changes unexpected original source/config files')
    actual={path.relative_to(variant).as_posix() for folder in ('src','config') for path in (variant/folder).rglob('*') if path.is_file()}
    if actual!=set(expected)|{NEW_MODULE}:
        raise ValueError('Local text variant has missing/extra executable source/config files')
    source_output.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(variant,source_output)
    frozen_files={relative:digest(source_output/relative) for relative in files}
    if frozen_files!=files:
        raise ValueError('Frozen local text source differs from CPU-tested variant')
    output.mkdir(parents=True,exist_ok=False)
    manifest={'format':FORMAT,'recognition_mode':'local_text','stage':'prepared',
        'base_prepared_path':str(base_path),'base_prepared_sha256':digest(base_path),
        'task_count':base['task_count'],'tasks':base['tasks'],
        'engine_source':str(source_output),'engine_source_files':frozen_files,'bundle':base['bundle'],
        'bundle_engines_sha256':base['bundle_engines_sha256'],'changed_original_files':sorted(VARIANT_FILES),
        'new_module':NEW_MODULE,'source_variant':str(variant),'source_inventory_sha256':fingerprint(frozen_files),
        'truth_read':False,'validation_or_holdout_read':False,'development_only':True,
        'candidate_selection_modified':False,'native_review_or_delivery_modified':False,
        'local_reread_capability_enabled':False,'default_strategy_changed':False,
        'scope':'Same complete 81-cell source-bound development crop denominator as prior page-mode probe; explicit official text request bypasses layout. No native edits or delivery qualification.'}
    frozen_json(output/'prepared.json',manifest)
    return manifest


def validate_manifest(manifest):
    if (manifest.get('format')!=FORMAT or manifest.get('recognition_mode')!='local_text'
            or manifest.get('truth_read') is not False or manifest.get('development_only') is not True
            or manifest.get('validation_or_holdout_read') is not False
            or manifest.get('candidate_selection_modified') is not False
            or manifest.get('native_review_or_delivery_modified') is not False
            or manifest.get('local_reread_capability_enabled') is not False
            or manifest.get('default_strategy_changed') is not False):
        raise ValueError('Direct text experiment changed source, mode or qualification')
    base_path=check_bound_file(manifest['base_prepared_path'],manifest['base_prepared_sha256'])
    base=read_json(base_path)
    validate_prepared(base)
    if manifest['tasks']!=base['tasks'] or manifest['task_count']!=base['task_count']:
        raise ValueError('Direct text experiment changes full prepared crop denominator')
    if fingerprint(manifest['engine_source_files'])!=manifest['source_inventory_sha256']:
        raise ValueError('Direct text source inventory changed')
    for relative,sha in manifest['engine_source_files'].items():
        check_bound_file(fpath(manifest['engine_source'])/relative,sha)
    check_bound_file(fpath(manifest['bundle'])/'config/engines.json',manifest['bundle_engines_sha256'])


def run(prepared_path,output):
    prepared_path,output=fpath(prepared_path),fpath(output)
    manifest=read_json(prepared_path)
    validate_manifest(manifest)
    if output.exists():
        raise ValueError('Recognition output exists; never restart prior experiment implicitly')
    for name in ('TEMP','TMP','LOCALAPPDATA','APPDATA','HF_HOME','TORCH_HOME','XDG_CACHE_HOME'):
        if not os.environ.get(name):
            raise ValueError('Missing F-drive child-process cache/temp environment')
        fpath(os.environ[name])
    output.mkdir(parents=True,exist_ok=False)
    frozen_json(output/'started.json',{'format':FORMAT,'pid':os.getpid(),'start_unix':time.time(),
        'recognition_mode':'local_text','prepared_sha256':digest(prepared_path),'task_count':manifest['task_count'],
        'truth_read':False,'gpu_serial_slot_required':True})
    records,error,cleanup_error=[],None,None
    start=time.perf_counter()
    adapter=None
    with variant_source(manifest['engine_source']):
        try:
            adapter_type=importlib.import_module('ocr_workbench.adapter').EngineAdapter
            adapter=adapter_type(manifest['bundle'],'glm',output/'sessions',worker_source=fpath(manifest['engine_source'])/'src')
            load_start=time.perf_counter()
            adapter.load()
            frozen_json(output/'ready.json',{'load_wall_seconds':time.perf_counter()-load_start,
                'sdk_model_load_seconds':adapter.load_seconds,'worker_pid':adapter.process.pid,'session_id':adapter.ipc.name})
            for task in manifest['tasks']:
                check_bound_file(task['transform']['crop_path'],task['transform']['crop_sha256'])
                row={'task_id':task['id'],'recognition_mode':'local_text','crop_sha256':task['transform']['crop_sha256'],
                    'source_sha256':task['source_sha256'],'source_bbox':task['transform']['source_bbox'],
                    'crop_to_source_mapping':{'source_to_crop_affine':task['transform']['source_to_crop_affine'],
                        'full_source_cell_geometry':task['geometry']},
                    'automatic_apply':False,'human_review':'not_performed'}
                tick=time.perf_counter()
                raw_output=output/'raw'/task['id']
                try:
                    result=adapter.recognize_local_text(task['transform']['crop_path'],raw_output,allow_empty=True)
                    if (result.get('recognition_mode')!='local_text' or result.get('tables')!=[]
                            or result.get('raw',{}).get('recognition_mode')!='local_text'
                            or result['raw'].get('formatter_applied') is not False
                            or result['raw'].get('layout_applied') is not False
                            or result['image']['sha256']!=task['transform']['crop_sha256']):
                        raise ValueError('Worker output is not explicit source-bound local text')
                    row.update(status='success',result=result,result_sha256=fingerprint(result))
                except Exception as failure:
                    row.update(status='failed',error_type=type(failure).__name__,error=str(failure))
                    if (raw_output/'error.json').exists():
                        row['worker_error_path']=str(raw_output/'error.json')
                        row['worker_error_sha256']=digest(raw_output/'error.json')
                row['recognition_wall_seconds']=time.perf_counter()-tick
                frozen_json(output/'receipts'/f'{task["id"]}.json',row)
                records.append(row)
                print(f'{len(records)}/{manifest["task_count"]} {task["id"]} {row["status"]}',flush=True)
                if row['status']=='failed' and (adapter.process is None or adapter.process.poll() is not None):
                    raise RuntimeError('Direct text worker exited; remaining source-bound cells were not run')
            validate_manifest(manifest)
        except BaseException as failure:
            error={'type':type(failure).__name__,'message':str(failure)}
        finally:
            if adapter is not None:
                try:
                    adapter.unload()
                except BaseException as failure:
                    cleanup_error={'type':type(failure).__name__,'message':str(failure)}
    report={'format':FORMAT,'stage':'model_terminal','recognition_mode':'local_text',
        'prepared_sha256':digest(prepared_path),'task_count':manifest['task_count'],'attempted_tasks':len(records),
        'records':records,'wall_seconds':time.perf_counter()-start,'error':error,'cleanup_error':cleanup_error,
        'worker_alive_after_unload':bool(adapter and adapter.process and adapter.process.poll() is None),
        'real_engine_adapter':True,'engine':'glm','truth_read':False,'validation_or_holdout_read':False,
        'candidate_selection_modified':False,'native_review_or_delivery_modified':False,
        'local_reread_capability_enabled':False,'default_strategy_changed':False,'human_review':'not_performed'}
    frozen_json(output/'summary.json',report)
    if error or cleanup_error or report['worker_alive_after_unload']:
        raise RuntimeError(str(error or cleanup_error or 'Worker cleanup did not finish'))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    pre=sub.add_parser('prepare')
    pre.add_argument('--base-prepared',required=True)
    pre.add_argument('--variant',required=True)
    pre.add_argument('--frozen-source',required=True)
    pre.add_argument('--output',required=True)
    execute=sub.add_parser('run')
    execute.add_argument('--prepared',required=True)
    execute.add_argument('--output',required=True)
    args=parser.parse_args()
    result=prepare(args.base_prepared,args.variant,args.frozen_source,args.output) if args.command=='prepare' else run(args.prepared,args.output)
    print({'stage':result['stage'],'task_count':result['task_count']},flush=True)


if __name__=='__main__':
    main()
