"""Offline RapidTable comparator: the same images, fixed text and PP-OCR blocks.

SLANet+ boxes remain labelled structure predictions, never detector evidence.
Mapping uses the production topology/text safeguards, with no GT coordinates.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import socket
import sys
import time

root = Path(__file__).resolve().parents[1]
base = root/'build/document-workflow'
dependencies = base/'rapid-reference/dependencies'
sys.path[:0] = [str(dependencies), str(root/'src')]
import numpy as np
from rapid_table import RapidTable, RapidTableInput, ModelType, EngineType
from ocr_workbench.geometry import topology
from ocr_workbench.coordinates import box_polygon, validate_polygon
from ocr_workbench.tables import parse_tables

def denied(*args, **kwargs):
    raise RuntimeError('Evaluation forbids network access')
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied

model = base/'rapid-reference/slanet-plus.onnx'
assert hashlib.sha256(model.read_bytes()).hexdigest() == 'd57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b'
manifest_path = base/'geometry-holdout/frozen/manifest.json'
manifest = json.loads(manifest_path.read_text('utf-8'))
out = base/'geometry-evaluation/rapidtable'
out.mkdir(exist_ok=True)
started = time.perf_counter()
engine = RapidTable(RapidTableInput(model_type=ModelType.SLANETPLUS, model_dir_or_path=model,
    engine_type=EngineType.ONNXRUNTIME, use_ocr=False,
    engine_cfg={'intra_op_num_threads':4,'inter_op_num_threads':1,'use_cuda':False}))
cold = time.perf_counter()-started
(out/'run-lock.json').write_text(json.dumps({'model_sha256':hashlib.sha256(model.read_bytes()).hexdigest(),
    'main_py_sha256':hashlib.sha256((dependencies/'rapid_table/main.py').read_bytes()).hexdigest(),
    'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'providers':engine.table_structure.session.session.get_providers() if hasattr(engine.table_structure,'session') else ['CPUExecutionProvider'],
    'cold_load_seconds':cold,'own_ocr':False,'network':'denied','gt_boxes_given_to_model':False},indent=2),'utf-8')
for n,sample in enumerate(manifest['samples']):
    folder = out/sample['id'];folder.mkdir(exist_ok=True)
    marker = folder/'evaluation.json'
    if marker.exists(): continue
    started = time.perf_counter()
    try:
        image = manifest_path.parent/sample['image']
        assert hashlib.sha256(image.read_bytes()).hexdigest() == sample['sha256']
        blocks = json.loads((base/'geometry-evaluation/ppocr'/sample['id']/'result.json').read_text('utf-8'))['blocks']
        imgs = engine._load_imgs(str(image))
        structures, batch_boxes = engine.table_structure(imgs)
        logic = engine.table_matcher.decode_logic_points(structures)
        external = [(np.asarray([b['polygon'] for b in blocks],dtype=np.float32),
                     tuple(b['text'] for b in blocks), tuple(float(b.get('confidence') or 0) for b in blocks))]
        dt, rec = engine.get_ocr_results(imgs,0,1,external)
        html = engine.table_matcher(structures,batch_boxes,dt,rec)[0]
        inference = time.perf_counter()-started
        boxes = batch_boxes[0].tolist()
        tables = parse_tables(html)
        expected = sample['table'];pred = tables[0] if len(tables)==1 else None
        norm = lambda value: ''.join(value.split())
        counts = Counter(norm(c['text']) for c in expected['cells'] if norm(c['text']))
        shared = sum((counts & Counter(norm(c['text']) for c in pred['cells'] if norm(c['text']))).values())/max(1,sum(counts.values())) if pred else 0
        chosen = pred is not None and topology(expected)==topology(pred) and shared>=.6 and len(boxes)==len(pred['cells'])
        mappings=[]
        for k,cell in enumerate(expected['cells']):
            item={'target':{'kind':'cell','table':0,'row':cell['row'],'column':cell['column']},
                  'level':'region','polygon':box_polygon([0,0,sample['width'],sample['height']]),
                  'reason':'structure_or_table_identity_ambiguous'}
            if chosen:
                value=norm(cell['text']); agreed=norm(pred['cells'][k]['text'])==value
                row_anchor=any(c['row']==cell['row'] and c['column']!=cell['column'] and norm(c['text']) and counts[norm(c['text'])]==1 and norm(c['text'])==norm(pred['cells'][j]['text']) for j,c in enumerate(expected['cells']))
                col_anchor=any(c['column']==cell['column'] and c['row']!=cell['row'] and norm(c['text']) and counts[norm(c['text'])]==1 and norm(c['text'])==norm(pred['cells'][j]['text']) for j,c in enumerate(expected['cells']))
                b=boxes[k];poly=box_polygon(b) if len(b)==4 else [b[j:j+2] for j in range(0,len(b),2)]
                try:poly=validate_polygon(poly,sample['width'],sample['height'])
                except ValueError:poly=None
                if poly and agreed and (bool(value) and counts[value]==1 or row_anchor and col_anchor):
                    item.update(level='cell',polygon=poly,reason='unique_structure_and_text',geometry_origin='structure_prediction')
                else:item['reason']='text_or_geometry_ambiguous'
            mappings.append(item)
        result={'status':'success','seconds':time.perf_counter()-started,'inference_seconds':inference,'mappings':mappings}
        (folder/'prediction.json').write_text(json.dumps({'html':html,'cell_bboxes':boxes,'logic_points':logic[0].tolist(),'origin':'structure_prediction'},ensure_ascii=False,indent=2),'utf-8')
    except Exception as error:
        import traceback
        result={'status':'failed','seconds':time.perf_counter()-started,'error':str(error),'traceback':traceback.format_exc()}
    marker.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(n+1,sample['id'],result['status'],round(result['seconds'],3),result.get('error',''),flush=True)
