"""Historical output identity and real-artifact local perturbation audit."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
from geometry_eval_common import ROOT,sha,write_json,mapping_code_lock
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.geometry_legacy_rapid import legacy_rapid_mapping
from ocr_workbench.table_matching import local_mapping
from report_geometry_v2 import score


def key(mapping):
    target=mapping['target'];return target.get('table',0),target['row'],target['column']


def main():
    output=ROOT/'audit/table-matching-v2/real-artifact-regressions.json'
    if output.exists():raise ValueError('Audit is immutable')
    historical=ROOT/'build/document-workflow/geometry-holdout/frozen/manifest.json'
    old=json.loads(historical.read_text('utf-8'));legacy=[]
    for sample in old['samples']:
        folder=ROOT/'build/document-workflow/geometry-evaluation/rapidtable'/sample['id']
        saved=json.loads((folder/'evaluation.json').read_text('utf-8'))
        if saved['status']!='success':legacy.append({'id':sample['id'],'status':'inference_failed'});continue
        prediction=json.loads((folder/'prediction.json').read_text('utf-8'))
        replay=legacy_rapid_mapping(sample['fixed_edit'],prediction,sample['width'],sample['height'])
        legacy.append({'id':sample['id'],'equal':replay==saved['mappings'],'cells':len(replay),
            'prediction_sha256':sha(folder/'prediction.json'),'historical_result_sha256':sha(folder/'evaluation.json')})
    root=ROOT/'build/table-matching-v2'
    inputs=json.loads((root/'dataset/inputs/development.json').read_text('utf-8'))['samples']
    labels={s['id']:s for s in json.loads((root/'dataset/annotations/development.annotations.json').read_text('utf-8'))['samples']}
    # First twelve nonmerged development tables, selected without model scores.
    chosen=[s for s in inputs if all(c['row_span']==c['column_span']==1 for t in s['fixed_edit']['tables'] for c in t['cells'])][:12]
    rows=[]
    for sample in chosen:
        for provider in ('geometry','rapidtable'):
            artifact=root/'inference/development'/provider/sample['id']/('geometry.json' if provider=='geometry' else 'prediction.json')
            prediction=json.loads(artifact.read_text('utf-8'))
            base=local_mapping(sample['fixed_edit'],prediction,sample['width'],sample['height'],image_version=sample['sha256'])
            baseline,_=score(labels[sample['id']],{'mappings':base})
            correct={(r['target']['table'],r['target']['row'],r['target']['column']) for r in baseline if r.get('correct')}
            original={key(m):m for m in base if m['target']['kind']=='cell'}
            for operation in ('extra_header','missing_middle_row'):
                edit=deepcopy(sample['fixed_edit']);table=edit['tables'][0];removed=table['rows']//2
                if operation=='extra_header':
                    for cell in table['cells']:cell['row']+=1
                    table['cells']=[{'row':0,'column':c,'row_span':1,'column_span':1,'text':f'NEW HEADER {c}'} for c in range(table['columns'])]+table['cells'];table['rows']+=1
                else:
                    table['cells']=[c for c in table['cells'] if c['row']!=removed]
                    for cell in table['cells']:
                        if cell['row']>removed:cell['row']-=1
                    table['rows']-=1
                modified=local_mapping(edit,prediction,sample['width'],sample['height'],image_version=sample['sha256'])
                after={key(m):m for m in modified if m['target']['kind']=='cell'}
                eligible=[k for k in correct if operation=='extra_header' or k[1]!=removed]
                retained=changed=0
                for k in eligible:
                    row=k[1]+1 if operation=='extra_header' else k[1]-(k[1]>removed)
                    m=after.get((k[0],row,k[2]),{})
                    retained+=m.get('level')=='cell' and m.get('polygon')==original[k]['polygon']
                    changed+=m.get('level')=='cell' and m.get('polygon')!=original[k]['polygon']
                rows.append({'sample_id':sample['id'],'provider':provider,'operation':operation,'correct_unaffected_before':len(eligible),
                    'same_correct_polygon_after':retained,'changed_offered_polygon':changed,'artifact_sha256':sha(artifact)})
    totals={}
    for provider in ('geometry','rapidtable'):
        for operation in ('extra_header','missing_middle_row'):
            c=Counter()
            for row in rows:
                if row['provider']==provider and row['operation']==operation:c.update({k:row[k] for k in ('correct_unaffected_before','same_correct_polygon_after','changed_offered_polygon')})
            totals[provider+':'+operation]={**c,'retention':c['same_correct_polygon_after']/c['correct_unaffected_before'] if c['correct_unaffected_before'] else None}
    write_json(output,{'version':2,'code':mapping_code_lock(),'historical_manifest_sha256':sha(historical),
        'legacy_rapid_all_identical':all(r.get('equal',True) for r in legacy),'legacy_rapid':legacy,
        'locality_scope':'First twelve nonmerged development tables; correct baseline identities independently scored against development labels. Synthetic adopted-structure perturbations over frozen real candidates, not an independent accuracy test.',
        'locality_tables':len(chosen),'locality':totals,'rows':rows})
    print(json.dumps({'legacy_rapid_all_identical':all(r.get('equal',True) for r in legacy),'locality':totals},indent=2))


if __name__=='__main__':main()
