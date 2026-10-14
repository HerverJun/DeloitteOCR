"""WTW polygon detection supplement, explicitly without logical/text GT claims."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from ocr_workbench.adapter import EngineAdapter
from PIL import Image
base=root/'build/document-workflow';source=base/'geometry-holdout/wtw';out=base/'wtw-evaluation';out.mkdir(exist_ok=True)
manifest=json.loads((source/'download-manifest.json').read_text('utf-8'))
lock={'manifest_sha256':hashlib.sha256((source/'download-manifest.json').read_bytes()).hexdigest(),
    'purpose':'additional Chinese and photographed-cell polygon detection; no original text, row/column/span annotations in this mirror',
    'photo_sample_numbers':[2,8,16,17,20],'chinese_sample_numbers':[n for n in range(1,21) if n not in (8,16,19)],
    'visual_labels':'manual inspection of original image contact sheet before inference; template duplicates possible, not part of 100 independent-document primary score',
    'gt_boxes_given_to_models':False,'model_lock_sha256':hashlib.sha256((root/'config/table-model-lock.json').read_bytes()).hexdigest()}
if (out/'run-lock.json').exists():assert json.loads((out/'run-lock.json').read_text())==lock
else:(out/'run-lock.json').write_text(json.dumps(lock,indent=2),'utf-8')
for engine in ('ppocr','geometry'):
    adapter=EngineAdapter(a.bundle,engine,out/'sessions');adapter.load()
    try:
        for n,s in enumerate(manifest['samples'],1):
            folder=out/engine/f'{n:02d}';folder.mkdir(parents=True,exist_ok=True)
            if (folder/'evaluation.json').exists():continue
            image=source/s['image'];assert hashlib.sha256(image.read_bytes()).hexdigest()==s['image_sha256']
            request=None
            if engine=='geometry':
                ocr_path=out/'ppocr'/f'{n:02d}'/'result.json'
                raw=json.loads(ocr_path.read_text('utf-8')) if ocr_path.exists() else {'blocks':[]}
                request={'regions':[],'ocr_source':'independent-ppocr','ocr_blocks':raw['blocks']}
            start=time.perf_counter()
            try:
                result=adapter.recognize(image.resolve(),folder.resolve(),request)
                record={'status':'success','seconds':time.perf_counter()-start,'inference_seconds':result.get('inference_seconds')}
            except Exception as error:record={'status':'failed','seconds':time.perf_counter()-start,'error':str(error)}
            (folder/'evaluation.json').write_text(json.dumps(record,indent=2),'utf-8')
            print(engine,n,record,flush=True)
    finally:adapter.unload()
