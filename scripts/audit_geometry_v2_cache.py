"""Cache/storage profile on one largest frozen development table, without GPU."""
import json
from pathlib import Path
import sys
import time
import shutil
from unittest.mock import patch
from geometry_eval_common import ROOT,sha,write_json,mapping_code_lock
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.geometry import enqueue_geometry,complete_geometry,geometry_view
from ocr_workbench.table_matching import local_mapping
from report_geometry_v2 import percentiles


def main():
    out=ROOT/'build/table-matching-v2/cache-profile'
    if out.exists():raise ValueError('Use a fresh benchmark directory')
    manifest_path=ROOT/'build/table-matching-v2/dataset/inputs/development.json'
    manifest=json.loads(manifest_path.read_text('utf-8'))
    sample=max(manifest['samples'],key=lambda s:sum(len(t['cells']) for t in s['fixed_edit']['tables']))
    source=ROOT/'build/table-matching-v2/inference/development'/ 'geometry'/sample['id']/'geometry.json'
    prediction=json.loads(source.read_text('utf-8'))
    store=Store(out/'workspace');project=store.project('Geometry cache profile')
    temporary=out/'import-copy.jpg'
    shutil.copyfile(manifest_path.parent/sample['image'],temporary)
    image=add_image(store,project['id'],'frozen-table.jpg',temporary)
    task=store.enqueue(project['id'],[image['active_version']],['ppocr'])[0];store.claim()
    store.complete(task,{**sample['fixed_edit'],'engine':'ppocr','blocks':prediction['ocr_blocks']})
    result_id=store.one('tasks',task)['result_id'];prediction['image_version']=image['active_version']
    artifact=store.root/'frozen-geometry.json';write_json(artifact,prediction)
    enqueue_geometry(store,result_id,0);task=store.claim();mapping_time=[]
    def timed_mapping(*args,**kwargs):
        started=time.perf_counter();result=local_mapping(*args,**kwargs);mapping_time.append((time.perf_counter()-started)*1000);return result
    started=time.perf_counter()
    with patch('ocr_workbench.table_matching.local_mapping',side_effect=timed_mapping):
        assert complete_geometry(store,task,prediction,artifact)
    complete_ms=(time.perf_counter()-started)*1000
    cells=sample['fixed_edit']['tables'][0]['cells'];queries=[];enqueues=[]
    for index in range(100):
        cell=cells[index%len(cells)];target={'kind':'cell','table':0,'row':cell['row'],'column':cell['column']}
        started=time.perf_counter();view=geometry_view(store,result_id,target);queries.append((time.perf_counter()-started)*1000)
        assert view['evidence']
        started=time.perf_counter();cached=enqueue_geometry(store,result_id,0);enqueues.append((time.perf_counter()-started)*1000)
        assert cached['cached']
    receipt={'version':2,'scope':'One largest development table; actual SQLite/evidence and unchanged cached requests; no GPU, HTTP, browser or human timing',
        'sample_id':sample['id'],'cells':len(cells),'source_artifact_sha256':sha(source),'code':mapping_code_lock(),
        'mapping_ms':mapping_time[0],'complete_geometry_ms':complete_ms,
        'artifact_hash_and_commit_overhead_ms':max(0,complete_ms-mapping_time[0]),
        'focused_cache_read_ms':percentiles(queries),'unchanged_request_cache_ms':percentiles(enqueues)}
    write_json(ROOT/'audit/table-matching-v2/cache-profile.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k!='code'},indent=2))


if __name__=='__main__':main()
