"""Freeze actual PaddleOCR-VL adopted text/structure, without reference labels."""
import argparse
import json
from pathlib import Path
import sys
import time
from geometry_eval_common import ROOT,ensure_lock,file_lock,sha,verify_inputs,verified_marker,write_json,verify_test_seal
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.adapter import EngineAdapter


def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--test-code-lock',type=Path);a=p.parse_args()
    manifest=verify_inputs(a.manifest)
    test_seal=verify_test_seal(manifest,a.test_code_lock)
    if manifest.get('split')=='test' and not a.test_code_lock:raise ValueError('Test adoption rule must be sealed first')
    rule={'engine':'paddlevl','selection':'only successful PaddleOCR-VL text/tables, no per-sample engine selection','revision':0}
    if a.test_code_lock and json.loads(a.test_code_lock.read_text('utf-8'))['adoption_rule']!=rule:raise ValueError('Adoption rule changed')
    lock={'version':2,'manifest_sha256':sha(a.manifest),'adoption_rule':rule,'model_lock_sha256':sha(a.bundle/'config/model-lock.json'),
        'code':file_lock((ROOT/'src/ocr_workbench').rglob('*.py')),'evaluator_sha256':sha(__file__),'device':'gpu:0','gt_boxes_given_to_model':False,
        'test_selection_sha256':test_seal}
    lock_hash=ensure_lock(a.output/'run-lock.json',lock)
    pending=[s for s in manifest['samples'] if verified_marker(a.output/s['id'],lock_hash,['result.json']) is None]
    if not pending:return
    adapter=EngineAdapter(a.bundle,'paddlevl',a.output/'sessions',worker_source=ROOT/'src')
    started=time.perf_counter();adapter.load();write_json(a.output/'timing.json',{'cold_process_load_seconds':time.perf_counter()-started})
    try:
        for n,sample in enumerate(pending):
            folder=a.output/sample['id'];folder.mkdir(parents=True,exist_ok=True);started=time.perf_counter()
            try:
                result=adapter.recognize((a.manifest.parent/sample['image']).resolve(),folder.resolve())
                value={'status':'success','seconds':time.perf_counter()-started,'artifact_sha256':{'result.json':sha(folder/'result.json')}}
            except Exception as error:value={'status':'failed','seconds':time.perf_counter()-started,'error':str(error),'artifact_sha256':{}}
            value['run_lock_sha256']=lock_hash;write_json(folder/'evaluation.json',value)
            print(n+1,len(pending),sample['id'],value['status'],round(value['seconds'],2),flush=True)
    finally:adapter.unload()


if __name__=='__main__':main()
