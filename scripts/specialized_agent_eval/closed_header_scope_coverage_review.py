"""Offline independent coverage and released-field review, after rule freeze/replay.

This process alone opens development reference truth. It never feeds reference
values to the rule, changes candidate data, or changes the scorer normalization.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from closed_header_review_scope_probe import safe,sha,bound,read,save,fp

def norm(value):return ''.join(unicodedata.normalize('NFC',str(value)).split())

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('replay','truth','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args();replay=safe(args.replay);out=safe(args.output);truth_path=safe(args.truth)
    if out.exists():raise ValueError('New immutable independent coverage output required')
    assert truth_path.parent.name=='development','No validation/holdout oracle in development review'
    result=read(replay/'result.json');decisions=read(replay/'decisions.json');observations=read(replay/'observations.json')
    original=read(result['original_observations']['path']);truth=read(truth_path)
    freeze=read(truth_path.parent/'freeze.json')
    assert sha(truth_path)==freeze['truth_sha256'] and observations['inputs_sha256']==freeze['inputs_sha256']
    assert bound(replay/'observations.json')==result['derived_observations'] and bound(replay/'decisions.json')==result['decisions']
    rows={r['id']:r for r in observations['records']};before={r['id']:r for r in original['records']};wanted={r['id']:r for r in truth['samples']}
    ds={r['id']:r for r in decisions['decisions']}
    assert len(rows)==len(observations['records'])==len(wanted)==len(truth['samples'])==len(ds)==len(before) and rows.keys()==wanted.keys()==ds.keys()==before.keys()
    assert all(r['candidate']==before[sid]['candidate'] for sid,r in rows.items())
    for sid,r in rows.items():
        assert r['image_sha256']==wanted[sid]['image_sha256']
        if not ds[sid]['eligible']:assert r==before[sid]
    # Match whole tables only in this offline scorer process. Candidate selection
    # and the frozen role policy have already completed without oracle access.
    from quality import match_tables,score_files
    counts=Counter();errors=[];freed=[];signed=[]
    for sid,w in wanted.items():
        r=rows[sid];candidate=r['candidate'];actual=candidate.get('edited',{}).get('tables',candidate['original']['tables'])
        pairs,_=match_tables(w['tables'],actual);regions=r['review_regions']
        full=r.get('review_image') is True or any(i.get('scope')=='image' for i in regions)
        for ti,t,ai,a in pairs:
            got={(c['row'],c['column']):c for c in a['cells']}
            for c in t['cells']:
                cell=got.get((c['row'],c['column']));visible=c.get('visibility','visible')=='visible'
                critical='critical' in c.get('tags',[]) or bool(re.search(r'\d',str(c['text'])))
                covered=full or any(i.get('scope')=='table' and i.get('table_index')==ai or i.get('scope')=='cell' and i.get('table_index')==ai and i.get('row')==c['row'] and i.get('column')==c['column'] and i.get('reliable') is True and i.get('evidence_ref') for i in regions)
                exact=cell is not None and norm(cell['text'])==norm(c['text']) and (cell.get('row_span',1),cell.get('column_span',1))==(c.get('row_span',1),c.get('column_span',1))
                counts['reference_cells']+=1;counts['reviewed_reference_cells']+=bool(covered)
                counts['visible_reference_cells']+=visible;counts['visible_critical_cells']+=visible and critical
                if visible and not exact:
                    e={'id':sid,'table':ti,'row':c['row'],'column':c['column'],'expected':c['text'],'actual':cell['text'] if cell else None,'critical':critical,'reviewed':bool(covered)}
                    errors.append(e);counts['visible_error_cells']+=1;counts['visible_error_cells_reviewed']+=bool(covered)
                    counts['visible_critical_error_cells']+=critical;counts['visible_critical_error_cells_reviewed']+=critical and bool(covered)
                    if '−' in str(c['text']) or '-' in str(cell['text'] if cell else ''):signed.append(e)
                if not covered:
                    assert ds[sid]['eligible']
                    field=next(f for f in ds[sid]['freed_fields'] if (f['table'],f['row'],f['column'])==(ai,c['row'],c['column']))
                    assert field['role']=='frozen_business_field_header' and cell['text']==field['literal']
                    freed.append({'id':sid,'table':ti,'row':c['row'],'column':c['column'],'text':cell['text'],'reference':c['text'],'exact':exact,'critical':critical,'role_basis':field['role_basis']})
        counts['images']+=1
    report=score_files(truth_path,replay/'observations.json');baseline=score_files(truth_path,result['original_observations']['path'])
    totals=report['summary']['totals']
    for key in ('visible_reference_cells','visible_critical_cells','visible_error_cells','visible_error_cells_reviewed','visible_critical_error_cells','visible_critical_error_cells_reviewed'):
        assert counts[key]==totals[key],key
    assert counts['reference_cells']==totals['all_reference_cells'] and counts['reviewed_reference_cells']==totals['review_reference_cells']
    error_content=lambda v:{r['id']:[{k:x for k,x in e.items() if k!='reviewed'} for e in r['errors']] for r in v['records']}
    assert error_content(report)==error_content(baseline),'Policy changed content errors'
    uncovered=[e for e in errors if not e['reviewed']]
    released_errors=[f for f in freed if not f['exact'] or f['critical']]
    decision={'format':'closed-business-header-independent-coverage-v1','utc':datetime.now(timezone.utc).isoformat(),
        'scope':'Development-informed CPU feasibility; no adoption/final qualification',
        'bindings':[bound(__file__),bound(replay/'result.json'),bound(replay/'decisions.json'),bound(replay/'observations.json'),bound(truth_path),bound(truth_path.parent/'freeze.json'),bound(Path(__file__).parent/'quality.py')],
        'counts':dict(counts),'coverage_ignores_automatic_delivery':True,'released_fields':freed,'signed_errors':signed,
        'uncovered_visible_errors':uncovered,'released_inexact_or_critical_fields':released_errors,
        'all_original_errors_and_candidates_unchanged':True,'development_truth_only_in_this_scoring_process':True,
        'status':'passed_development_feasibility' if not uncovered and not released_errors else 'rejected_development_feasibility',
        'review_cells_reduced':baseline['summary']['totals']['review_reference_cells']-totals['review_reference_cells'],
        'net_value_or_human_time_qualified':False,'validation40_unknown':True,'previous_v1_adopted':False,
        'adoption_qualification':False,'final_delivery_qualification':False,'baseline_summary':baseline['summary'],'after_summary':report['summary']}
    out.mkdir(parents=True);save(out/'review.json',decision);save(out/'quality.json',report)
    print({k:decision[k] for k in ('status','counts','review_cells_reduced','validation40_unknown')})

if __name__=='__main__':main()
