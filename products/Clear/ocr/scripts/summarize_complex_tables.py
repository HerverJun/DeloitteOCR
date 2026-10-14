"""Report measured local outcomes and scope boundaries; never a fresh-test claim."""
from collections import Counter,defaultdict
import json
import random
from pathlib import Path
import sys

from complex_table_run import ROOT,BUILD,AUDIT,save,sha
from measure_structure_net_benefit import summarize


def main():
    reports=BUILD/'runs/local-selection-03'
    annotations=json.loads((ROOT/'build/tableformer-next-20260915/dataset/sealed/test.annotations.json').read_text('utf-8'))
    groups={};comparisons={}
    random_check=set(random.Random(20260919).sample(sorted(s['id'] for s in annotations['samples']),12))
    save(AUDIT/'api/random-check-policy.json',{'seed':20260919,'historical_regression_only':True,'sample_ids':sorted(random_check),
        'rule':'12 document IDs sampled independently of errors; real API acceptance requires new frozen groups'})
    for track in ('actual_adoption','reference_structure_control'):
        events=[]
        for method in ('TableFormer-local-v3','TableFormer-spatial'):
            group_counts=defaultdict(Counter);tables=Counter();strict_tables=0
            for s in annotations['samples']:
                result=json.loads((reports/track/method/(s['id']+'.json')).read_text('utf-8'))
                by_target={tuple(r['target'].values()):r for r in result['scored_rows']}
                complete=True
                for i,row in enumerate(result['scored_rows']):
                    tags=['all']+row['tags']
                    for tag in tags:group_counts[tag].update(row['counts'])
                    complete &= bool(row['counts'].get('correct'))
                strict_tables+=complete and result['extra_outputs']==0
                group_counts['all']['extra_outputs']+=result['extra_outputs']
                for tag in {tag for r in result['scored_rows'] for tag in r['tags']}|{'all'}:tables[tag]+=1
            groups[track+'/'+method]={'groups':{k:{'counts':dict(v),'tables':tables[k],
                'coverage':v['correct']/v['targets'] if v['targets'] else None,
                'wrong_rate':v['wrong']/v['offered'] if v['offered'] else None,
                'strict_quality':v['correct']/(v['offered']+v['extra_outputs']) if v['offered']+v['extra_outputs'] else None} for k,v in group_counts.items()},
                'fully_localized_tables':strict_tables,'strict_structure_usable_rate':'not_measured; geometry coverage alone is insufficient'}
        for s in annotations['samples']:
            a=json.loads((reports/track/'TableFormer-local-v3'/(s['id']+'.json')).read_text('utf-8'))
            b=json.loads((reports/track/'TableFormer-spatial'/(s['id']+'.json')).read_text('utf-8'))
            for i,(before,after) in enumerate(zip(a['scored_rows'],b['scored_rows'])):
                if before['target']!=after['target']:raise ValueError('Unpaired target order')
                events.append({'target_id':s['id']+':'+str(i),'group_id':s['group_id'],
                    'before_correct':bool(before['counts'].get('correct')),'after_correct':bool(after['counts'].get('correct')),
                    'triggered':not bool(before['counts'].get('offered')) or s['id'] in random_check,
                    'abstained':not bool(after['counts'].get('offered'))})
        comparisons[track]=summarize(events,[e['target_id'] for e in events])
        save(BUILD/'runs/local-selection-03'/f'{track}-paired-events.json',{'scope':'historical local strategy comparison, not API',
            'events':events,'expected_target_ids':[e['target_id'] for e in events]})
    save(AUDIT/'local/grouped-results.json',{'groups':groups,'comparisons':comparisons,'fresh_acceptance':False})
    protocol=json.loads((AUDIT/'evaluation-protocol.json').read_text('utf-8'))
    selected=json.loads((ROOT/'config/geometry-matching-policy-v4.json').read_text('utf-8'))
    save(AUDIT/'ablations/selection.json',{'selected':selected,'default':'local-v2','default_changed':False,
        'evidence':'local-selection-03/report.json','production_quality':'not_met','limits':'new-data qualification and large-table timing fail formal gates'})
    # Engineering fixture: 4 new correct slots after selecting the fixed 4x2
    # candidate. This was exercised by the browser against a drawn source image.
    fixture=[{'target_id':str(i),'group_id':'drawn-4x2-table','before_correct':i<4,'after_correct':True,
        'triggered':True,'abstained':False} for i in range(8)]
    data={'scope':'synthetic loopback UI fixture, not real service effectiveness','real_service_measured':False,
        'events':fixture,'expected_target_ids':[str(i) for i in range(8)]}
    save(BUILD/'runs/api-fixture-events.json',data)
    save(AUDIT/'api/measurement-receipt.json',{'scope':data['scope'],'metrics':summarize(fixture,data['expected_target_ids']),
        'real_api_net_benefit':'not_measured','human_time_saved':'not_measured','script_operations':['confirm send','submit','inspect recommendation','accept','undo'],
        'real_service_cost':'not_measured','input_complete':True})
    print(json.dumps(comparisons,indent=2))


if __name__=='__main__':main()
