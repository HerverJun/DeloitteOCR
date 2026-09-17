"""Replay a frozen candidate matrix. Inference inputs only; no scoring labels."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from geometry_eval_common import ROOT, ensure_lock, file_lock, sha, verify_inputs, verified_marker, write_json,mapping_code_lock
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.geometry import conservative_mapping
from ocr_workbench.geometry_legacy_rapid import legacy_rapid_mapping
from ocr_workbench.table_matching import local_mapping,default_policy


def candidate_plan(policy,ablations):
    result={'B0':('geometry','region',None),'B1':('geometry','legacy',None),'B2':('rapidtable','legacy',None),
            'N1':('geometry','local-v2',policy),'N2':('rapidtable','local-v2',policy)}
    if ablations:
        for provider,prefix in [('geometry','N1'),('rapidtable','N2')]:
            for suffix,change in [('token-only',{'local_correspondence':False,'merged_cells':False,'repeated_empty_cells':False}),
                                  ('local-only',{'merged_cells':False,'repeated_empty_cells':False}),
                                  ('no-merged',{'merged_cells':False}),('no-repeat-empty',{'repeated_empty_cells':False})]:
                variant={**deepcopy(policy),**change};variant['policy_version']+='-'+suffix
                result[prefix+'-'+suffix]=(provider,'local-v2',variant)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--candidates',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--policy',type=Path);p.add_argument('--ablations',action='store_true')
    p.add_argument('--test-code-lock',type=Path);p.add_argument('--adopted-results',type=Path);a=p.parse_args()
    manifest=verify_inputs(a.manifest);policy=json.loads(a.policy.read_text('utf-8')) if a.policy else default_policy()
    code=mapping_code_lock()
    if manifest.get('split')=='test':
        if not a.test_code_lock:raise SystemExit('Seal the code, policy and adoption rule BEFORE test inference/replay')
        sealed=json.loads(a.test_code_lock.read_text('utf-8'))
        if sealed['code']!=code or sealed['policy']!=policy:raise SystemExit('Test code/policy differs from sealed selection')
    files=[]
    if a.adopted_results:
        files += [a.adopted_results/s['id']/f for s in manifest['samples'] for f in ('result.json','evaluation.json') if (a.adopted_results/s['id']/f).exists()]
        files.append(a.adopted_results/'run-lock.json')
    for provider,name in [('geometry','geometry.json'),('rapidtable','prediction.json'),('ppocr','result.json')]:
        files += [a.candidates/provider/s['id']/f for s in manifest['samples'] for f in (name,'evaluation.json') if (a.candidates/provider/s['id']/f).exists()]
        if (a.candidates/provider/'run-lock.json').exists():files.append(a.candidates/provider/'run-lock.json')
    lock={'version':2,'manifest_sha256':sha(a.manifest),'code':code,'policy':policy,'candidate_artifacts':file_lock(files),
        'matrix':candidate_plan(policy,a.ablations),'track':'real-result' if a.adopted_results else manifest.get('track','historical-control'),'annotations_read':False}
    lock_hash=ensure_lock(a.output/'run-lock.json',lock)
    matrix=candidate_plan(policy,a.ablations)
    for name,(provider,method,strategy) in matrix.items():
        for sample in manifest['samples']:
            folder=a.output/name/sample['id']
            if verified_marker(folder,lock_hash,['mapping.json']) is not None:continue
            artifact_name='geometry.json' if provider=='geometry' else 'prediction.json'
            artifact=a.candidates/provider/sample['id']/artifact_name
            marker_path=artifact.with_name('evaluation.json')
            started=time.perf_counter()
            adopted_edit=None
            try:
                if a.adopted_results:
                    adopted_marker=verified_marker(a.adopted_results/sample['id'],sha(a.adopted_results/'run-lock.json'),['result.json'])
                    if not adopted_marker or adopted_marker['status']!='success':raise RuntimeError('Frozen adopted structure inference failed')
                    adopted=json.loads((a.adopted_results/sample['id']/'result.json').read_text('utf-8'))
                    adopted_edit={'text':adopted['text'],'tables':adopted['tables']}
                edit=adopted_edit if adopted_edit is not None else sample['fixed_edit']
                if method=='region':
                    from ocr_workbench.coordinates import box_polygon
                    mappings=[{'target':{'kind':'cell','table':ti,'row':c['row'],'column':c['column']},
                        'polygon':box_polygon([0,0,sample['width'],sample['height']]),'level':'region','reason':'region_baseline'}
                        for ti,t in enumerate(edit['tables']) for c in t['cells']]
                    status='success';inference=0
                else:
                    marker=json.loads(marker_path.read_text('utf-8'))
                    if marker['status']!='success':raise RuntimeError(marker.get('error','provider_inference_failed'))
                    provider_lock=a.candidates/provider/'run-lock.json'
                    if marker.get('run_lock_sha256'):
                        verified_marker(artifact.parent,sha(provider_lock),[artifact_name])
                    prediction=json.loads(artifact.read_text('utf-8'))
                    if 'ocr_blocks' not in prediction:
                        prediction['ocr_blocks']=json.loads((a.candidates/'ppocr'/sample['id']/'result.json').read_text('utf-8'))['blocks']
                    inference=prediction.get('inference_seconds',marker.get('inference_seconds'))
                    if method=='legacy':mappings=(conservative_mapping if provider=='geometry' else legacy_rapid_mapping)(edit,prediction,sample['width'],sample['height'])
                    else:mappings=local_mapping(edit,prediction,sample['width'],sample['height'],policy=strategy,
                        result_id=sample['id'],image_version=sample['sha256'])
                    status='success'
                mapping={'status':status,'mappings':mappings,'mapping_seconds':time.perf_counter()-started,'inference_seconds':inference}
            except Exception as error:
                mapping={'status':'failed','mappings':[],'mapping_seconds':time.perf_counter()-started,'error':str(error),
                         'incomplete_input':isinstance(error,(FileNotFoundError,KeyError)) or 'hash' in str(error).lower()}
            if a.adopted_results:
                mapping['adopted_edit']=adopted_edit or {'text':'','tables':[]}
            write_json(folder/'mapping.json',mapping)
            write_json(folder/'evaluation.json',{'status':mapping['status'],'run_lock_sha256':lock_hash,'artifact_sha256':{'mapping.json':sha(folder/'mapping.json')}})
        print(name,len(manifest['samples']),'replayed',flush=True)


if __name__=='__main__':main()
