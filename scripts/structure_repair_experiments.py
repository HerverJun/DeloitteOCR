"""Paired frozen-input ablations and offline upper bounds; no label-driven proposals."""
from collections import Counter
from copy import deepcopy
import argparse
import importlib.util
import time
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from structure_repair_run import ROOT, AUDIT, BUILD, HISTORY, read, save, sha, update


def minimal_failures():
    from ocr_workbench.structure_diagnostics import preserve_values
    old={'rows':1,'columns':2,'cells':[{'row':0,'column':0,'row_span':1,'column_span':2,'text':'A  001'}]}
    new={'rows':1,'columns':2,'cells':[{'row':0,'column':i,'row_span':1,'column_span':1,'text':v,
         'structure_source':{'token_ids':['t'+str(i)]}} for i,v in enumerate(['A','001'])]}
    tokens=[{'id':'t0','raw_text':'A'},{'id':'t1','raw_text':'001'}]
    out,conflicts,retained=preserve_values(old,new,old,tokens)
    value={'current':old,'candidate':new,'tokens':tokens,'output':out,'conflicts':conflicts,
        'retained':retained,'unsafe_accepted':not conflicts,'raw_literal_changed':old['cells'][0]['text']!=''.join(c['text'] for c in out['cells']),
        'problem':'Whitespace-insensitive match accepted split without adopted fragment boundaries; matching_text uses NFC, not NFKC'}
    save(AUDIT/'baseline-invariants-whitespace.json',value)
    print('unsafe_accepted:',not conflicts)


def ablate(name=''):
    from ocr_workbench.structure_diagnostics import local_variants
    from ocr_workbench.structure_repair import content_baseline,bind_adopted,make_patch
    rows=read(BUILD/'baseline/rows.json')
    if len(rows)!=120:raise ValueError('baseline unfinished')
    results=[];all_candidates=[]
    for method in ['source','geometry','adjacency']:
        records=[];start=time.perf_counter()
        for row in rows:
            for ti,current in enumerate(row['adopted']['tables']):
                b=content_baseline(current,result_id=row['result_id'],revision=row['revision'],
                    image_version=row['version_id'],image_sha256=row['image_sha256'])
                item={'sample':row['id'],'table':ti,'candidates':[],'method':method}
                for candidate in row['candidates']:
                    if ti not in candidate['adopted_table_indices']:continue
                    skeleton=candidate['table']['skeleton']
                    variants=[{'kind':'replace_table','table':skeleton}]
                    try:variants+=local_variants(current,skeleton)
                    except ValueError as error: item.setdefault('variant_errors',[]).append(str(error))
                    for variant in variants:
                        started=time.perf_counter()
                        proposed,conflicts,moves=bind_adopted(b,variant['table'],method=method)
                        record={'provider':candidate['provider'],'candidate_table':candidate['table']['id'],'kind':variant['kind'],
                            'binding_conflicts':conflicts,'moves':moves,'eligible':False}
                        if not conflicts:
                            try:
                                patch=make_patch(b,proposed,moves,local=variant['kind']!='replace_table')
                                record['patch']=patch
                                if candidate['table']['unassigned_token_ids'] or candidate['rejected_tokens'] or candidate['grid_error']:
                                    record['gate_error']='existing product source/grid gate'
                                else:record['eligible']=True
                            except ValueError as error:record['gate_error']=str(error)
                        record['milliseconds']=(time.perf_counter()-started)*1000
                        item['candidates'].append(record)
                records.append(item)
        save(BUILD/(method+'-ablation'+name+'.json'),records)
        options=[c for r in records for c in r['candidates']]
        summary={'method':method,'tables':len(records),'candidate_variants':len(options),
                 'bound_candidates':sum(not c['binding_conflicts'] for c in options),
                 'eligible_candidates':sum(c['eligible'] for c in options),
                 'eligible_tables':sum(any(c['eligible'] for c in r['candidates']) for r in records),
                 'primary_conflicts':dict(Counter(c['binding_conflicts'][0]['kind'] if c['binding_conflicts'] else c.get('gate_error','eligible') for c in options)),
                 'seconds':time.perf_counter()-start,'max_binding_ms':max((c['milliseconds'] for c in options),default=0),
                 'output_sha256':sha(BUILD/(method+'-ablation'+name+'.json'))}
        results.append(summary);print(summary,flush=True)
    save(AUDIT/('local-ablations'+name+'.json'),{'input_sha256':sha(BUILD/'baseline/rows.json'),'variants':results,
        'original':read(AUDIT/'baseline-funnel.json'),'same_adopted_baseline':True,'new_ocr':False,
        'continue_gate_met':False if not any(r['eligible_candidates'] for r in results) else None})


def diagnostic():
    from score_luna_structure_sim import score,aggregate
    from ocr_workbench.structure_diagnostics import local_variants
    from measure_structure_net_benefit import summarize
    rows=read(BUILD/'baseline/rows.json')
    labels={r['id']:r for r in read(HISTORY/'dataset/sealed/test.annotations.json')['samples']}
    records=[];scores=[];events=[];groups=Counter()
    for row in rows:
        reference=labels[row['id']];before=score(reference,row['adopted']);scores.append(before)
        choices=[]
        for candidate in row['candidates']:
            for ti in candidate['adopted_table_indices']:
                variants=[{'kind':'whole','table':candidate['table']['skeleton']}]
                try:variants+=local_variants(row['adopted']['tables'][ti],candidate['table']['skeleton'])
                except ValueError:pass
                for variant in variants:
                    edit=deepcopy(row['adopted']);edit['tables'][ti]=variant['table']
                    got=score(reference,edit)
                    choices.append({'provider':candidate['provider'],'table':ti,'kind':variant['kind'],
                        'correct':got['correct'],'structure_correct':got['structure_correct'],'extra_outputs':got['extra_outputs'],
                        'full_table_exact':got['full_table_exact'],'full_table_structure':got['full_table_structure']})
        records.append({'id':row['id'],'group':row['group'],'before':{k:v for k,v in before.items() if k!='rows'},
            'choices':choices,'best_structure_delta':max([before['structure_correct']]+[c['structure_correct'] for c in choices])-before['structure_correct'],
            'best_joint_delta':max([before['correct']]+[c['correct'] for c in choices])-before['correct']})
        for i,c in enumerate(before['rows']):
            events.append({'target_id':row['id']+':'+str(i),'group_id':row['group'],'before_correct':c['correct'],
                'after_correct':c['correct'],'triggered':False,'abstained':False,'tags':c['tags']})
            groups.update(c['tags'])
    save(BUILD/'oracle-diagnostic-cases.json',records)
    save(AUDIT/'repairability.json',{'scope':'offline strict-slot upper bound only; no identity-aware quality claim',
        'already_joint_exact':sum(r['before']['full_table_exact'] for r in records),
        'already_structure_exact':sum(r['before']['full_table_structure'] for r in records),
        'inputs_with_any_structure_gain':sum(r['best_structure_delta']>0 for r in records),
        'inputs_with_any_joint_gain':sum(r['best_joint_delta']>0 for r in records),
        'no_identified_candidate':sum(not r['choices'] for r in records),
        'labels_used_in_production':False,'label_sha256':sha(HISTORY/'dataset/sealed/test.annotations.json'),
        'baseline':aggregate(scores),'target_tags':dict(groups),
        'identity_metric':'not_measured: adopted cells lack independently linked target identity; strict slots reported separately'})
    save(AUDIT/'grouped-net-benefit.json',{'scope':'historical strict-slot product no-op, not model quality or confirmation',
        'all':summarize(events,[e['target_id'] for e in events]),
        'groups':{tag:summarize([e for e in events if tag in e['tags']],[e['target_id'] for e in events if tag in e['tags']]) for tag in ['merged','empty','repeated']},
        'cost':'not_measured','human_time':'not_measured'})
    save(BUILD/'paired-events.json',events)
    print('diagnostic records:',len(records),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['failures','ablate','diagnostic']);p.add_argument('--name',default='');a=p.parse_args()
    if a.phase=='ablate':ablate(a.name)
    else:{'failures':minimal_failures,'diagnostic':diagnostic}[a.phase]()
