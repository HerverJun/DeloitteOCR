"""Frozen reference text, independent real PP-OCR blocks, serial GPU geometry."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from ocr_workbench.adapter import EngineAdapter
from ocr_workbench.geometry import conservative_mapping
from ocr_workbench.coordinates import box_polygon

p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
manifest=json.loads(a.manifest.read_text('utf-8'));a.output.mkdir(parents=True,exist_ok=True)
sha=hashlib.sha256(a.manifest.read_bytes()).hexdigest()
lock=a.output/'run-lock.json'
if lock.exists() and json.loads(lock.read_text('utf-8'))['manifest_sha256']!=sha:raise SystemExit('Manifest changed after evaluation started')
lock.write_text(json.dumps({'manifest_sha256':sha,'model_lock_sha256':hashlib.sha256((root/'config/table-model-lock.json').read_bytes()).hexdigest(),
    'geometry_code_sha256':hashlib.sha256((root/'src/ocr_workbench/geometry.py').read_bytes()).hexdigest(),'ground_truth_boxes_given_to_models':False},indent=2),'utf-8')
for engine in ('ppocr','geometry'):
    adapter=EngineAdapter(a.bundle,engine,a.output/'sessions')
    started=time.perf_counter();adapter.load();cold=time.perf_counter()-started
    (a.output/f'{engine}-timing.json').write_text(json.dumps({'cold_process_load_seconds':cold,'model_load_seconds':adapter.load_seconds},indent=2),'utf-8')
    try:
        for n,sample in enumerate(manifest['samples']):
            folder=a.output/engine/sample['id'];folder.mkdir(parents=True,exist_ok=True)
            marker=folder/'evaluation.json'
            if marker.exists():continue
            image=a.manifest.parent/sample['image']
            if hashlib.sha256(image.read_bytes()).hexdigest()!=sample['sha256']:raise ValueError('Frozen image hash mismatch')
            started=time.perf_counter()
            try:
                request=None
                if engine=='geometry':
                    ocr_path=a.output/'ppocr'/sample['id']/'result.json'
                    if not ocr_path.exists():raise ValueError('Independent OCR failed; no reference coordinates substituted')
                    raw=json.loads(ocr_path.read_text('utf-8'))
                    request={'regions':[{'polygon':box_polygon([0,0,sample['width'],sample['height']]),'source':'input-table-crop'}],
                        'ocr_source':'independent-ppocr','ocr_blocks':raw['blocks']}
                prediction=adapter.recognize(image.resolve(),folder.resolve(),request)
                result={'status':'success','seconds':time.perf_counter()-started}
                if engine=='geometry':
                    result['mappings']=conservative_mapping(sample['fixed_edit'],prediction,sample['width'],sample['height'])
                    result['inference_seconds']=prediction['inference_seconds']
            except Exception as error:
                result={'status':'failed','seconds':time.perf_counter()-started,'error':str(error)}
            marker.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
            print(engine,n+1,sample['id'],result['status'],round(result['seconds'],2),flush=True)
    finally:adapter.unload()
