"""Pinned RapidTable CPU provider; legacy and v2 share exactly the same candidates."""
import argparse
import json
from pathlib import Path
import socket
import sys
import time
from geometry_eval_common import ROOT, ensure_lock, file_lock, runtime_info, sha, verified_marker, verify_inputs, write_json, verify_test_seal


def main():
    p=argparse.ArgumentParser();base=ROOT/'build/document-workflow'
    p.add_argument('--manifest',type=Path,default=base/'geometry-holdout/frozen/manifest.json')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--ocr',type=Path,required=True)
    p.add_argument('--dependencies',type=Path,default=base/'rapid-reference/dependencies')
    p.add_argument('--model',type=Path,default=base/'rapid-reference/slanet-plus.onnx')
    p.add_argument('--test-code-lock',type=Path)
    p.add_argument('--mapper',choices=['none','legacy','local-v2'],default='legacy');a=p.parse_args()
    sys.path[:0]=[str(a.dependencies),str(ROOT/'src')]
    import numpy as np
    from rapid_table import RapidTable,RapidTableInput,ModelType,EngineType
    from ocr_workbench.geometry_legacy_rapid import legacy_rapid_mapping
    from ocr_workbench.table_matching import local_mapping,default_policy
    def denied(*args,**kwargs):raise RuntimeError('Evaluation forbids network access')
    socket.socket.connect=denied;socket.socket.connect_ex=denied;socket.create_connection=denied
    if sha(a.model)!='d57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b':raise ValueError('Rapid model hash mismatch')
    manifest=verify_inputs(a.manifest)
    test_seal=verify_test_seal(manifest,a.test_code_lock)
    lock={'version':2,'provider':'rapidtable-3.0.2','manifest_sha256':sha(a.manifest),'model_sha256':sha(a.model),
        'provider_code':file_lock((a.dependencies/'rapid_table').rglob('*.py')),'device':'CPUExecutionProvider','threads':{'intra':4,'inter':1},
        'ocr_snapshot':file_lock([a.ocr/s['id']/'result.json' for s in manifest['samples'] if (a.ocr/s['id']/'result.json').exists()]),
        'mapper':a.mapper,'runtime':runtime_info(),'network':'denied','ground_truth_given_to_model':False,'test_selection_sha256':test_seal,
        'evaluator':file_lock([Path(__file__),ROOT/'scripts/geometry_eval_common.py']),
        'mapping_code':file_lock((ROOT/'src/ocr_workbench').rglob('*.py')) if a.mapper!='none' else {},
        'policy':default_policy() if a.mapper=='local-v2' else None}
    lock_hash=ensure_lock(a.output/'run-lock.json',lock)
    pending=[s for s in manifest['samples'] if verified_marker(a.output/s['id'],lock_hash,['prediction.json']) is None]
    if not pending:return
    started=time.perf_counter()
    engine=RapidTable(RapidTableInput(model_type=ModelType.SLANETPLUS,model_dir_or_path=a.model,engine_type=EngineType.ONNXRUNTIME,use_ocr=False,
        engine_cfg={'intra_op_num_threads':4,'inter_op_num_threads':1,'use_cuda':False}))
    write_json(a.output/'timing.json',{'cold_load_seconds':time.perf_counter()-started})
    for n,sample in enumerate(pending):
        folder=a.output/sample['id'];folder.mkdir(parents=True,exist_ok=True);started=time.perf_counter()
        try:
            blocks=json.loads((a.ocr/sample['id']/'result.json').read_text('utf-8'))['blocks']
            imgs=engine._load_imgs(str(a.manifest.parent/sample['image']))
            structures,batch_boxes=engine.table_structure(imgs);logic=engine.table_matcher.decode_logic_points(structures)
            external=[(np.asarray([b['polygon'] for b in blocks],dtype=np.float32),tuple(b['text'] for b in blocks),tuple(float(b.get('confidence') or 0) for b in blocks))]
            dt,rec=engine.get_ocr_results(imgs,0,1,external);html=engine.table_matcher(structures,batch_boxes,dt,rec)[0]
            inference=time.perf_counter()-started
            prediction={'html':html,'cell_bboxes':batch_boxes[0].tolist(),'logic_points':logic[0].tolist(),
                'box_format':'quad8','span_format':'inclusive-r0-r1-c0-c1','origin':'structure_prediction',
                'model_sha256':lock['model_sha256'],'image_version':sample['sha256'],'ocr_blocks':blocks,
                'ocr_source':'independent-ppocr:'+sample['sha256'],'inference_seconds':inference}
            write_json(folder/'prediction.json',prediction)
            result={'status':'success','seconds':time.perf_counter()-started,'inference_seconds':inference,'artifact_sha256':{'prediction.json':sha(folder/'prediction.json')}}
            if a.mapper!='none':result['mappings']=(legacy_rapid_mapping if a.mapper=='legacy' else local_mapping)(sample['fixed_edit'],prediction,sample['width'],sample['height'])
        except Exception as error:
            result={'status':'failed','seconds':time.perf_counter()-started,'error':str(error),'artifact_sha256':{}}
        result['run_lock_sha256']=lock_hash;write_json(folder/'evaluation.json',result)
        print(n+1,len(pending),sample['id'],result['status'],round(result['seconds'],3),flush=True)


if __name__=='__main__':main()
