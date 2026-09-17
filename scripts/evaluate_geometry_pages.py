"""Full-page table detection supplement; inference never reads table labels."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
from geometry_eval_common import ROOT,ensure_lock,file_lock,sha,verify_inputs,verified_marker,write_json,verify_test_seal
sys.path.insert(0,str(ROOT/'src'))


def infer(a):
    manifest=verify_inputs(a.manifest)
    test_seal=verify_test_seal(manifest,a.test_code_lock)
    model_dir=a.bundle/'models/PP-DocLayoutV3'
    lock={'version':2,'manifest_sha256':sha(a.manifest),'model':'PP-DocLayoutV3',
        'model_files':file_lock(p for p in model_dir.iterdir() if p.is_file()),
        'code':file_lock([Path(__file__),ROOT/'scripts/geometry_eval_common.py']),
        'device':'gpu:0','threshold':.5,'gt_regions_given_to_model':False,'test_selection_sha256':test_seal}
    digest=ensure_lock(a.output/'run-lock.json',lock)
    pending=[s for s in manifest['samples'] if verified_marker(a.output/s['id'],digest,['prediction.json']) is None]
    if not pending:return
    from ocr_workbench.offline import configure,install_guard
    configure(a.output);install_guard(a.output/'network-blocked.log')
    import msvcrt
    gpu_path=Path(os.environ['LOCALAPPDATA'])/'OfflineOCR/gpu.lock'
    with gpu_path.open('a+b') as gpu:
        gpu.seek(0);msvcrt.locking(gpu.fileno(),msvcrt.LK_NBLCK,1)
        try:
            from paddlex import create_model
            from ocr_workbench.worker import json_safe
            started=time.perf_counter()
            model=create_model('PP-DocLayoutV3',model_dir=str(model_dir),device='gpu:0')
            write_json(a.output/'timing.json',{'model_load_seconds':time.perf_counter()-started})
            for index,sample in enumerate(pending):
                folder=a.output/sample['id'];folder.mkdir(parents=True,exist_ok=True);started=time.perf_counter()
                try:
                    result=next(iter(model(str((a.manifest.parent/sample['image']).resolve()),threshold=.5)))
                    boxes=json.loads(json.dumps(result['boxes'],default=json_safe))
                    prediction={'status':'success','boxes':boxes,'seconds':time.perf_counter()-started}
                except Exception as error:
                    prediction={'status':'failed','boxes':[],'seconds':time.perf_counter()-started,'error':str(error)}
                write_json(folder/'prediction.json',prediction)
                write_json(folder/'evaluation.json',{'status':prediction['status'],'run_lock_sha256':digest,
                    'artifact_sha256':{'prediction.json':sha(folder/'prediction.json')}})
                print(manifest['split'],index+1,len(pending),prediction['status'],flush=True)
        finally:
            gpu.seek(0);msvcrt.locking(gpu.fileno(),msvcrt.LK_UNLCK,1)


def report(a):
    from diagnose_geometry_legacy import matching_upper_bound
    from report_geometry_v2 import percentiles
    from ocr_workbench.geometry import iou
    if a.output.exists():raise ValueError('Report exists; choose a new output path')
    labels=json.loads(a.annotations.read_text('utf-8'))
    split_path=a.annotations.parent.parent/'split-lock.json'
    declared={k.replace('\\','/'):v for k,v in json.loads(split_path.read_text('utf-8'))['files'].items()}
    complete=declared.get(str(a.annotations.relative_to(split_path.parent)).replace('\\','/'))==sha(a.annotations)
    lock_path=a.predictions/'run-lock.json';digest=sha(lock_path)
    manifest=a.annotations.parent.parent/'inputs'/(labels['split']+'.json')
    if json.loads(lock_path.read_text('utf-8'))['manifest_sha256']!=sha(manifest):complete=False
    rows=[]
    for sample in labels['samples']:
        try:
            marker=verified_marker(a.predictions/sample['id'],digest,['prediction.json'])
            if marker is None:raise ValueError('Missing marker')
            prediction=json.loads((a.predictions/sample['id']/'prediction.json').read_text('utf-8'))
        except (OSError,ValueError,KeyError):
            complete=False;prediction={'status':'failed','boxes':[]}
        boxes=[b['coordinate'] for b in prediction['boxes'] if b['label']=='table']
        edges=[[i for i,box in enumerate(boxes) if iou(gt,box)>=.5] for gt in sample['table_boxes']]
        matched=matching_upper_bound(edges)
        rows.append({'sample_id':sample['id'],'group_id':sample['group_id'],'stratum':sample['stratum'],
            'targets':len(edges),'offered':len(boxes),'matched':matched,'false_positive':len(boxes)-matched,
            'missed':len(edges)-matched,'failed':int(prediction['status']!='success'),'seconds':prediction.get('seconds')})
    groups={}
    for name in ['all','no_table','single_table','multiple_tables']:
        subset=[r for r in rows if name=='all' or r['stratum']==name];counts=Counter()
        for row in subset:counts.update({k:row[k] for k in ('targets','offered','matched','false_positive','missed','failed')})
        groups[name]={'pages':len(subset),'documents':len({r['group_id'] for r in subset}),'counts':dict(counts),
            'precision':counts['matched']/counts['offered'] if counts['offered'] else None,
            'recall':counts['matched']/counts['targets'] if counts['targets'] else None,
            'pages_with_false_positive':sum(r['false_positive']>0 for r in subset),
            'inference_ms':percentiles([r['seconds']*1000 for r in subset if r['seconds'] is not None])}
    write_json(a.output,{'version':2,'complete':complete,'split':labels['split'],'track':'page-detection',
        'annotations_sha256':sha(a.annotations),'prediction_lock_sha256':digest,'groups':groups,'rows':rows,
        'definition':'One-to-one table bbox IoU >= .5; every page retained, including failed and no-table pages',
        'scope':'Full-page detector only; no logical cell or end-to-end matching claim'})
    print(json.dumps({'complete':complete,'groups':groups},indent=2))


def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='phase',required=True)
    q=sub.add_parser('infer');q.add_argument('--manifest',type=Path,required=True);q.add_argument('--bundle',type=Path,required=True)
    q.add_argument('--output',type=Path,required=True);q.add_argument('--test-code-lock',type=Path)
    q=sub.add_parser('report');q.add_argument('--annotations',type=Path,required=True)
    q.add_argument('--predictions',type=Path,required=True);q.add_argument('--output',type=Path,required=True)
    a=p.parse_args();(infer if a.phase=='infer' else report)(a)


if __name__=='__main__':main()
