"""Paired all-target and triggered-target audit with document-cluster intervals.

Input events must include every frozen target, including missing outputs. This
tool consumes correctness judgments; it does not manufacture reference labels.
"""
import argparse
from collections import Counter,defaultdict
import json
import random
from pathlib import Path


def summarize(events, expected_ids, *, seed=20260919):
    ids=[r['target_id'] for r in events]
    if len(ids)!=len(set(ids)) or set(ids)!=set(expected_ids):raise ValueError('Missing, extra or duplicate evaluation targets')
    for r in events:
        if any(type(r[k]) is not bool for k in ('before_correct','after_correct','triggered','abstained')):
            raise ValueError('Correctness, trigger and abstention must be explicit booleans')
        if not r.get('group_id'):raise ValueError('Document/template grouping is required')
    output={}
    for scope,rows in [('all',events),('triggered',[r for r in events if r['triggered']])]:
        counts=Counter(targets=len(rows),corrected=0,harmed=0,still_wrong=0,unchanged_correct=0,abstained=0)
        groups=defaultdict(list)
        for r in rows:
            gain=int(r['after_correct'])-int(r['before_correct']);groups[r['group_id']].append(gain)
            counts['corrected']+=gain==1;counts['harmed']+=gain==-1
            counts['still_wrong']+=not r['before_correct'] and not r['after_correct']
            counts['unchanged_correct']+=r['before_correct'] and r['after_correct']
            counts['abstained']+=r['abstained']
        rng=random.Random(seed);keys=sorted(groups);draws=[]
        for _ in range(2000):
            sample=[v for key in rng.choices(keys,k=len(keys)) for v in groups[key]] if keys else []
            if sample:draws.append(sum(sample)/len(sample))
        draws.sort()
        output[scope]={'counts':dict(counts),'net':counts['corrected']-counts['harmed'],
            'document_template_groups':len(keys),'net_rate_95_interval':[draws[49],draws[1949]] if draws else None}
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--events',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    data=json.loads(args.events.read_text('utf-8'))
    report={'scope':data['scope'],'real_service_measured':data.get('real_service_measured',False),
        'metrics':summarize(data['events'],data['expected_target_ids'])}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists():raise ValueError('Output exists; choose a new receipt')
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
