"""Hash-verified historical replay and ablations; never claims fresh inference."""
from collections import Counter,defaultdict
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys
import time

from complex_table_run import ROOT,BUILD,AUDIT,save,sha,update
sys.path.insert(0,str(ROOT/'src'))
from compare_tableformer_geometry import checked_artifact,bind_prediction
from ocr_workbench.table_matching import local_mapping,policy_for_algorithm,matching_code_fingerprint
from report_geometry_v2 import score,rates,percentiles


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',default='historical-replay-01')
    parser.add_argument('--local-only',action='store_true',help='Compare final local-v3 and local-v4 only after broad ablations')
    args=parser.parse_args()
    history=ROOT/'build/tableformer-next-20260915'
    manifest_path=history/'dataset/inputs/test.json'
    labels_path=history/'dataset/sealed/test.annotations.json'
    manifest=json.loads(manifest_path.read_text('utf-8'))
    annotation=json.loads(labels_path.read_text('utf-8'))
    labels={s['id']:s for s in annotation['samples']}
    v2=policy_for_algorithm('local-v2');v3=policy_for_algorithm('local-v3')
    optimized=policy_for_algorithm('local-v4')
    matrix={'Paddle-local-v2':('geometry',v2),'TableFormer-local-v2':('tableformer',v2),
        'TableFormer-local-v3':('tableformer',v3),'TableFormer-no-merged':('tableformer',{**v3,'merged_cells':False}),
        'TableFormer-no-repeated-empty':('tableformer',{**v3,'repeated_empty_cells':False,'neighbor_anchors':False}),
        'TableFormer-no-neighbor':('tableformer',{**v3,'neighbor_anchors':False}),
        'TableFormer-spatial':('tableformer',optimized)}
    if args.local_only:matrix={k:v for k,v in matrix.items() if k in ('TableFormer-local-v3','TableFormer-spatial')}
    if not args.run.replace('-','').isalnum():raise ValueError('Invalid run name')
    output=BUILD/'runs'/args.run;output.mkdir(parents=True,exist_ok=True)
    lock={'code':matching_code_fingerprint(),'manifest_sha256':sha(manifest_path),'labels_sha256':sha(labels_path),
        'matrix':matrix,'scope':'historical exposed crop regression; fixed old OCR and candidate replay, NOT new inference or fresh acceptance',
        'samples':[s['id'] for s in manifest['samples']],'adoption_rule':'successful fixed PaddleOCR-VL result only, no per-sample engine choice'}
    lock_path=output/'run-lock.json'
    if lock_path.exists() and json.loads(lock_path.read_text('utf-8'))!=json.loads(json.dumps(lock)):
        raise ValueError('Code or inputs changed; use a new experiment directory')
    save(lock_path,lock)
    aggregates=defaultdict(Counter);failures=defaultdict(Counter);timings=defaultdict(list);by_sample=defaultdict(dict)
    for n,s in enumerate(manifest['samples']):
        try:
            ocr,_,_=checked_artifact(history/'inference/test/ppocr',s,'result.json')
            adopted,_,_=checked_artifact(history/'adopted/test',s,'result.json')
            actual={'text':adopted['text'],'tables':adopted['tables']}
            source_error=None
        except (ValueError,OSError,KeyError) as error:source_error=str(error)
        for track in ('actual_adoption','reference_structure_control'):
            for method,(provider,policy) in matrix.items():
                key=track+'/'+method
                path=output/track/method/(s['id']+'.json')
                if path.exists():result=json.loads(path.read_text('utf-8'))
                else:
                    start=time.perf_counter()
                    try:
                        if source_error:raise ValueError(source_error)
                        name='geometry.json' if provider=='geometry' else 'prediction.json'
                        pred,_,digest=checked_artifact(history/'inference/test'/provider,s,name)
                        pred=bind_prediction(pred,ocr['blocks'],s,provider)
                        edit=actual if track=='actual_adoption' else s['fixed_edit']
                        start=time.perf_counter()
                        mappings=local_mapping(edit,pred,s['width'],s['height'],policy=policy,result_id=s['id'],
                            image_version=s['sha256'],ocr_blocks=ocr['blocks'])
                        result={'status':'success','mappings':mappings,'elapsed_ms':(time.perf_counter()-start)*1000,
                            'candidate_sha256':digest}
                        if track=='actual_adoption':result['adopted_edit']=edit
                    except (ValueError,OSError,KeyError) as e:result={'status':'failed','error':str(e),'mappings':[],'elapsed_ms':(time.perf_counter()-start)*1000}
                    rows,extras=score(labels[s['id']],result)
                    result['scored_rows']=rows;result['extra_outputs']=extras
                    save(path,result)
                counts=Counter()
                for row in result['scored_rows']:
                    counts.update(row['counts'])
                    if not row['counts'].get('correct'):failures[key][row['primary_failure_stage']]+=1
                counts['extra_outputs']+=result['extra_outputs']
                aggregates[key].update(counts);timings[key].append(result['elapsed_ms'])
                by_sample[key][s['id']]={'correct':counts['correct'],'wrong':counts['wrong'],'targets':counts['targets'],
                    'document':s['original_document_id'],'template_group':s['group_id'],
                    'correct_targets':[r['target'] for r in result['scored_rows'] if r['counts'].get('correct')]}
        if n%5==0:print(n+1,len(manifest['samples']),s['id'],flush=True)
    report={'scope':lock['scope'],'quality':'not_measured','new_inference':False,'methods':{k:{'counts':dict(c),'rates':rates(c),
        'elapsed_ms':percentiles(timings[k]),'primary_failures':dict(failures[k])} for k,c in aggregates.items()},'per_sample':dict(by_sample)}
    report_folder=AUDIT/('baseline' if args.run=='historical-replay-01' else args.run)
    save(report_folder/'report.json',report);save(report_folder/'run-lock.json',lock)
    differences={}
    for track in ('actual_adoption','reference_structure_control'):
        baseline=by_sample[track+'/TableFormer-local-v3']
        for method in matrix:
            changes=Counter()
            for ident,current in by_sample[track+'/'+method].items():
                a={json.dumps(t,sort_keys=True) for t in baseline[ident]['correct_targets']}
                b={json.dumps(t,sort_keys=True) for t in current['correct_targets']}
                changes.update(new_correct=len(b-a),lost_correct=len(a-b),wrong_delta=current['wrong']-baseline[ident]['wrong'])
            differences[track+'/'+method]=dict(changes)
    save(report_folder/'ablations.json',{'comparisons_to_v3':differences,'same_inputs':True,'fresh_quality_acceptance':False})
    save(AUDIT/'diagnosis.json',{'primary_counts':dict(failures),'ranking':['candidate structure and grid errors','token ownership','geometry matching and budgets'],
        'native_pdf_observation':'filled background rectangles can introduce false row/column boundaries; independently observed in hk-audit-85-ch1-p24'})
    save(AUDIT/'hypotheses.json',{'bounded_variants':['spatial index with unchanged overlap validator','fully validated anchors survive incomplete later rounds',
        'exclude non-stroked shading rectangles from optional native vector candidate'],'do_not_change_default_until_qualified':True})
    update('A07','done_with_gaps',[f'audit/{AUDIT.name}/baseline/{n}.json' for n in ('report','run-lock')],engineering='passed',quality='not_measured',next_step='Use historical failure analysis for bounded local experiments; no fresh quality claim.')
    update('A08','done',[f'audit/{AUDIT.name}/{n}.json' for n in ('diagnosis','hypotheses')],engineering='passed',next_step='Complete local engineering and native candidate experiment.')


if __name__=='__main__':main()
