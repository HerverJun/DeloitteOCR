"""Independent scoring, including every target and failed/missing inference item."""
import argparse
from copy import deepcopy
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys
import numpy as np
from geometry_eval_common import ROOT,sha,write_json
from geometry_reference_identity import associate,IDENTITY_RULE
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.coordinates import bounds,box_polygon
from ocr_workbench.geometry_contract import intersection_area,polygon_iou,signed_area,checked_polygon,full_cell_evidence
from ocr_workbench.geometry import iou


def percentiles(values):
    return {k:float(np.percentile(values,p)) if values else None for k,p in [('median',50),('p90',90),('p95',95)]} | {'n':len(values)}


def rates(counts):
    n,a=counts.get('targets',0),counts.get('offered',0)
    return {'precise_coverage':counts.get('correct',0)/n if n else None,
        'wrong_cell_rate':counts.get('wrong',0)/a if a else None,
        'offered_quality':counts.get('correct',0)/a if a else None,
        'strict_output_quality':counts.get('correct',0)/(a+counts.get('extra_outputs',0)) if a+counts.get('extra_outputs',0) else None,
        'low_iou_rate':counts.get('low_iou',0)/a if a else None,'offered_rate':a/n if n else None}


def score(sample,prediction):
    targets=sample['targets'];mappings=prediction.get('mappings',[]);by_target=defaultdict(list)
    target_polys=[box_polygon(t['box']) for t in targets]
    if 'adopted_edit' in prediction:
        association=associate(targets,prediction['adopted_edit'])
        mappings=deepcopy(mappings)
        represented={(m['target'].get('table',0),m['target']['row'],m['target']['column']) for m in mappings if m['target']['kind']=='cell'}
        for ti,table in enumerate(prediction['adopted_edit'].get('tables',[])):
            for cell in table['cells']:
                original=(ti,cell['row'],cell['column'])
                if original not in association and original not in represented:
                    mappings.append({'target':{'kind':'cell','table':ti,'row':cell['row'],'column':cell['column']},
                        'level':'image','polygon':None,'reason':'reference_identity_unavailable'})
        for mapping in mappings:
            target=mapping['target']
            if target['kind']!='cell':continue
            original=(target.get('table',0),target['row'],target['column'])
            mapped=association.get(original)
            if mapped:
                target['table'],target['row'],target['column']=mapped
            else:
                target['table']=-1-original[0]
    for m in mappings:
        if m['target']['kind']=='cell':by_target[(m['target'].get('table',0),m['target']['row'],m['target']['column'])].append(m)
    rows=[];consumed=set()
    for index,target in enumerate(targets):
        key=(target.get('table',0),target['row'],target['column']);items=by_target.get(key,[]);consumed.add(key)
        m=items[0] if items else None;counts=Counter(targets=1,offered=0,correct=0,wrong=0,low_iou=0)
        counts['extra_outputs']=max(0,len(items)-1)
        detail={'target':{'table':key[0],'row':key[1],'column':key[2]},'tags':target.get('tags',[]),
                'primary_failure_stage':m.get('primary_failure_stage','legacy') if m else 'inference',
                'reason_codes':m.get('reason_codes',[m.get('reason')]) if m else ['inference_failed']}
        if not m:counts['no_output']+=1
        elif m.get('level')!='cell' or not m.get('polygon'):
            scope=m.get('range_semantics');counts['text_fallback' if scope=='text_extent' else 'region_fallback' if m.get('polygon') else 'image_fallback']+=1
        else:
            counts['offered']+=1
            members=m.get('cell_polygons') or [m['polygon']]
            try:
                if m.get('contract_version',1)>=2 and not full_cell_evidence(m):raise ValueError('Invalid full-cell capability')
                for member in members:checked_polygon(member,sample.get('width',float('inf')),sample.get('height',float('inf')))
            except (ValueError,TypeError,IndexError):
                counts['invalid_geometry']+=1;detail.update(correct=False,wrong_cell=False,scorer_outcome='invalid_geometry',counts=dict(counts))
                rows.append(detail);continue
            # Full-cell groups have disjoint members by the matcher contract;
            # reject overlap instead of adding areas twice.
            overlaps_members=any(intersection_area(a,b)>1e-6 for i,a in enumerate(members) for b in members[i+1:])
            area=sum(abs(signed_area(p)) for p in members)
            overlaps=[]
            for gt in target_polys:
                overlap=sum(intersection_area(p,gt) for p in members)
                overlaps.append(overlap/(area+abs(signed_area(gt))-overlap) if area+abs(signed_area(gt))-overlap>0 else 0)
            best=max(overlaps,default=0);mine=overlaps[index];wrong=best>mine+1e-9
            ties=sum(abs(v-best)<=1e-9 for v in overlaps)>1 and best>0
            correct=not wrong and not ties and not overlaps_members and mine>=.5
            counts['wrong']+=int(wrong);counts['correct']+=int(correct)
            counts['low_iou']+=int(not wrong and not ties and not overlaps_members and not correct)
            counts['identity_tie']+=int(ties);counts['invalid_geometry']+=int(overlaps_members);counts['no_intersection']+=int(best==0)
            legacy_overlaps=[iou(bounds(m['polygon']),t['box']) for t in targets]
            legacy_wrong=max(legacy_overlaps)>legacy_overlaps[index]+1e-9
            counts['legacy_wrong']+=int(legacy_wrong);counts['legacy_correct']+=int(not legacy_wrong and legacy_overlaps[index]>=.5)
            detail.update(iou=mine,best_iou=best,correct=correct,wrong_cell=wrong,identity_tie=ties,
                scorer_outcome='correct' if correct else 'wrong_target' if wrong else 'identity_ambiguous' if ties else 'boundary_not_qualified',
                legacy_bbox_iou=legacy_overlaps[index],legacy_wrong_cell=legacy_wrong)
            if not correct and not wrong and not ties:
                ratio=area/abs(signed_area(target_polys[index]))
                detail['boundary_error_type']='undersized' if ratio<.67 else 'oversized' if ratio>1.5 else 'offset_or_shape'
        detail['counts']=dict(counts);rows.append(detail)
    extra=sum(len(items) for key,items in by_target.items() if key not in consumed)
    return rows,extra


def aggregate(rows,*,bootstrap=True):
    counts=Counter();documents=defaultdict(Counter)
    for row in rows:
        counts.update(row['counts']);documents[row['group_id']].update(row['counts'])
    result={'counts':dict(counts),**rates(counts),'tables':len({r['sample_id'] for r in rows}),'documents':len(documents),
        'primary_failure_stages':dict(Counter(r['primary_failure_stage'] for r in rows)),
        'reason_codes_nonadditive':dict(Counter(code for r in rows for code in r['reason_codes'])),
        'boundary_iou':percentiles([r['iou'] for r in rows if 'iou' in r])}
    result['boundary_error_types']=dict(Counter(r['boundary_error_type'] for r in rows if 'boundary_error_type' in r))
    tables=defaultdict(Counter)
    for row in rows:tables[row['sample_id']].update(row['counts'])
    result['mean_table_precise_coverage']=statistics.mean(c['correct']/c['targets'] for c in tables.values()) if tables else None
    if bootstrap and documents:
        keys=['targets','offered','correct','wrong'];data=np.asarray([[c[k] for k in keys] for c in documents.values()],dtype=float)
        rng=np.random.default_rng(20260913);samples=data[rng.integers(0,len(data),size=(1000,len(data)))].sum(axis=1)
        intervals={}
        for name,num,den in [('precise_coverage',2,0),('wrong_cell_rate',3,1),('offered_quality',2,1)]:
            valid=samples[:,den]>0;values=samples[valid,num]/samples[valid,den]
            intervals[name]=[float(v) for v in np.percentile(values,[2.5,97.5])] if len(values) else None
        result['document_group_bootstrap_95pct']=intervals
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--annotations',type=Path,required=True);p.add_argument('--replay',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise SystemExit('Reports are immutable; choose a new output path')
    annotations=json.loads(a.annotations.read_text('utf-8'));lock=json.loads((a.replay/'run-lock.json').read_text('utf-8'))
    lock_hash=sha(a.replay/'run-lock.json');incomplete=[]
    input_path=a.annotations.parent.parent/'inputs'/(annotations['split']+'.json')
    if not input_path.exists() or lock['manifest_sha256']!=sha(input_path):incomplete.append('input_manifest_mismatch')
    split_path=a.annotations.parent.parent/'split-lock.json'
    if split_path.exists():
        split=json.loads(split_path.read_text('utf-8'));declared={k.replace('\\','/'):v for k,v in split['files'].items()}
        if declared.get(str(a.annotations.relative_to(split_path.parent)).replace('\\','/'))!=sha(a.annotations):incomplete.append('annotation_hash_mismatch')
    else:incomplete.append('missing_split_lock')
    outputs={};all_rows={};timings={};extras={}
    for method in lock['matrix']:
        rows=[];elapsed=[];elapsed_small=[];elapsed_large=[];extra=0
        for sample in annotations['samples']:
            folder=a.replay/method/sample['id'];file=folder/'mapping.json';marker=folder/'evaluation.json'
            try:
                receipt=json.loads(marker.read_text('utf-8'))
                if receipt['run_lock_sha256']!=lock_hash or receipt['artifact_sha256']['mapping.json']!=sha(file):raise ValueError('mapping_hash_mismatch')
                prediction=json.loads(file.read_text('utf-8'))
                if prediction.get('incomplete_input'):incomplete.append(method+':'+sample['id']+':incomplete_frozen_input')
            except (OSError,ValueError,KeyError) as error:
                incomplete.append(method+':'+sample['id']+':'+str(error));prediction={'status':'failed','mappings':[]}
            scored,additional=score(sample,prediction);extra+=additional
            for row in scored:row.update(sample_id=sample['id'],group_id=sample.get('group_id',sample['original_document_id']),cohort=sample['cohort'],table_tags=sample.get('tags',[]),input_kind=sample.get('input_kind','unknown'))
            rows+=scored
            if prediction.get('mapping_seconds') is not None:
                ms=prediction['mapping_seconds']*1000;elapsed.append(ms)
                (elapsed_small if len(sample['targets'])<=250 else elapsed_large).append(ms)
        groups={'all':aggregate(rows)}
        groups['all']['counts']['extra_outputs']=groups['all']['counts'].get('extra_outputs',0)+extra
        groups['all'].update(rates(groups['all']['counts']))
        for source in sorted({r['cohort'] for r in rows}):groups['source:'+source]=aggregate([r for r in rows if r['cohort']==source])
        for kind in sorted({r['input_kind'] for r in rows}):groups['input:'+kind]=aggregate([r for r in rows if r['input_kind']==kind])
        for tag in ('merged','repeated','empty'):
            groups['target:'+tag]=aggregate([r for r in rows if tag in r['tags']])
            groups['target:not-'+tag]=aggregate([r for r in rows if tag not in r['tags']])
            groups['tables-with:'+tag]=aggregate([r for r in rows if tag in r['table_tags']])
        groups['all']['mapping_ms']=percentiles(elapsed)
        groups['all']['mapping_ms_at_most_250_cells']=percentiles(elapsed_small)
        groups['all']['mapping_ms_above_250_cells']=percentiles(elapsed_large)
        groups['all']['mapping_timing_scope']='artifact read, provider adaptation and correspondence; CPU only; model inference excluded'
        rate=groups['all'];groups['all']['thresholds_passed']=bool(rate['precise_coverage'] is not None and rate['precise_coverage']>=.9 and rate['wrong_cell_rate'] is not None and rate['wrong_cell_rate']<=.01 and rate['offered_quality'] is not None and rate['offered_quality']>=.95)
        outputs[method]=groups;all_rows[method]=rows
    deltas={}
    for method,rows in all_rows.items():
        baseline='B1' if method.startswith('N1') else 'B2' if method.startswith('N2') else None
        if baseline:
            counts=Counter()
            for old,new in zip(all_rows[baseline],rows):
                counts['new_correct']+=int(bool(new.get('correct')) and not old.get('correct'))
                counts['lost_correct']+=int(bool(old.get('correct')) and not new.get('correct'))
                counts['new_wrong']+=int(bool(new.get('wrong_cell')) and not old.get('wrong_cell'))
                counts['new_low_iou']+=max(0,new['counts'].get('low_iou',0)-old['counts'].get('low_iou',0))
            deltas[method]={'baseline':baseline,**counts}
    report={'version':2,'complete':not incomplete,'incomplete_reasons':incomplete,'split':annotations.get('split'),
        'annotations_sha256':sha(a.annotations),'replay_lock_sha256':lock_hash,'track':lock['track'],
        'real_result_identity_rule':IDENTITY_RULE if lock['track']=='real-result' else None,'groups':outputs,'deltas':deltas,
        'definitions':{'precise_coverage':'C/N: correct identity, nonambiguous full polygon IoU >= .5',
            'wrong_cell_rate':'W/A: another target has strictly higher polygon IoU',
            'offered_quality':'C/A','strict_output_quality':'C/(A+extra/duplicate outputs)',
            'legacy_fields':'Historical bounding-rectangle IoU and strict higher-other-target rule retained independently',
            'failure_denominator':'Every annotation target remains in N, including missing markers and inference failures',
            'confidence_interval':'1000 bootstrap resamples of original document/template groups, fixed seed'},
        'unmeasured_groups':['native PDF real-result track','physical scan real-result track','FinTabNet canonical refresh','ruled/borderless','page-level multi-table cell matching','Chinese/photo logical'],
        'production_acceptance':False,'production_reason':'Control-track crop results alone cannot establish production applicability; real-result and critical cohorts required',
        'rows':all_rows}
    write_json(a.output,report)
    print(json.dumps({'complete':report['complete'],'results':{k:v['all'] for k,v in outputs.items()},'deltas':deltas},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
