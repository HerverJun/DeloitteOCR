"""Frozen OCR/candidate inference. Replay correspondence separately, without GPU."""
import argparse
import json
from pathlib import Path
import sys
import time

from geometry_eval_common import ROOT, ensure_lock, file_lock, runtime_info, sha, verified_marker, verify_inputs, write_json, verify_test_seal
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.adapter import EngineAdapter
from ocr_workbench.coordinates import box_polygon


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--bundle',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--phase',choices=['all','ppocr','geometry'],default='all')
    p.add_argument('--mapper',choices=['none','legacy'],default='none')
    p.add_argument('--test-code-lock',type=Path)
    a=p.parse_args();manifest=verify_inputs(a.manifest)
    test_seal=verify_test_seal(manifest,a.test_code_lock)
    for engine in (['ppocr','geometry'] if a.phase=='all' else [a.phase]):
        code=['adapter.py','engine_host.py','worker.py','coordinates.py','windows_paths.py','atomic_files.py','processes.py','tables.py','normalize.py']
        if engine=='geometry':code+=['table_geometry_worker.py','geometry_contract.py','geometry_providers.py']
        code_paths=[ROOT/'src/ocr_workbench'/name for name in code if (ROOT/'src/ocr_workbench'/name).exists()]
        if a.mapper=='legacy':code_paths += [ROOT/'src/ocr_workbench/geometry.py']
        model_lock=a.bundle/'config'/('table-model-lock.json' if engine=='geometry' else 'model-lock.json')
        lock={'version':2,'phase':'candidates','engine':engine,'manifest_sha256':sha(a.manifest),
            'model_lock_sha256':sha(model_lock),'code':file_lock(code_paths),'runtime':runtime_info(),
            'engine_runtime':file_lock([a.bundle/'runtimes/ppocr/python.exe',a.bundle/'runtimes/ppocr/python312.dll']),
            'device':'gpu:0','mapper':a.mapper,'ground_truth_given_to_model':False,'test_selection_sha256':test_seal,
            'ocr_input':'frozen-independent-ppocr' if engine=='geometry' else 'image',
            'evaluator':file_lock([Path(__file__),ROOT/'scripts/geometry_eval_common.py'])}
        if engine=='geometry':
            lock['ocr_snapshot']=file_lock([a.output/'ppocr'/s['id']/'result.json' for s in manifest['samples'] if (a.output/'ppocr'/s['id']/'result.json').exists()])
            lock['ocr_run_lock_sha256']=sha(a.output/'ppocr/run-lock.json')
        folder=a.output/engine
        lock_hash=ensure_lock(folder/'run-lock.json',lock)
        artifact_name='geometry.json' if engine=='geometry' else 'result.json'
        pending=[s for s in manifest['samples'] if verified_marker(folder/s['id'],lock_hash,[artifact_name]) is None]
        if not pending:continue
        adapter=EngineAdapter(a.bundle,engine,a.output/'sessions',worker_source=ROOT/'src')
        started=time.perf_counter();adapter.load();cold=time.perf_counter()-started
        write_json(folder/'timing.json',{'cold_process_load_seconds':cold,'model_load_seconds':adapter.load_seconds})
        try:
            for n,sample in enumerate(pending):
                destination=folder/sample['id'];destination.mkdir(parents=True,exist_ok=True)
                started=time.perf_counter()
                try:
                    request=None
                    if engine=='geometry':
                        ocr_folder=a.output/'ppocr'/sample['id']
                        ocr_marker=verified_marker(ocr_folder,lock['ocr_run_lock_sha256'],['result.json'])
                        if not ocr_marker or ocr_marker['status']!='success':raise ValueError('Independent OCR failed; no reference coordinates substituted')
                        blocks=json.loads((ocr_folder/'result.json').read_text('utf-8'))['blocks']
                        request={'regions':[{'polygon':box_polygon([0,0,sample['width'],sample['height']]),'source':'input-table-crop'}],
                            'ocr_source':'independent-ppocr:'+sample['sha256'],'ocr_blocks':blocks,'image_version':sample['sha256'],
                            'allow_auxiliary_ocr':False}
                    prediction=adapter.recognize((a.manifest.parent/sample['image']).resolve(),destination.resolve(),request)
                    result={'status':'success','seconds':time.perf_counter()-started,'inference_seconds':prediction.get('inference_seconds'),
                            'artifact_sha256':{artifact_name:sha(destination/artifact_name)}}
                    if engine=='geometry' and a.mapper=='legacy':
                        from ocr_workbench.geometry import conservative_mapping
                        result['mappings']=conservative_mapping(sample['fixed_edit'],prediction,sample['width'],sample['height'])
                except Exception as error:
                    result={'status':'failed','seconds':time.perf_counter()-started,'error':str(error),'artifact_sha256':{}}
                result['run_lock_sha256']=lock_hash;write_json(destination/'evaluation.json',result)
                print(engine,n+1,len(pending),sample['id'],result['status'],round(result['seconds'],2),flush=True)
        finally:adapter.unload()


if __name__=='__main__':main()
