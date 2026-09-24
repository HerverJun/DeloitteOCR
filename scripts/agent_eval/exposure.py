"""Record held-out item exposure before human/agent inspection or debugging."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True,help='Frozen dataset root')
    parser.add_argument('--task',action='append',required=True,help='Held-out task ID; repeat to record multiple items')
    parser.add_argument('--reason',required=True,help='Why the task will be exposed')
    parser.add_argument('--actor',required=True,help='Inspector identity/role')
    args=parser.parse_args()
    split=json.loads((args.root/'evaluation-split.json').read_text('utf-8'))
    allowed={t['id'] for t in split['tasks'] if t['split']=='holdout'}
    if not set(args.task)<=allowed: raise ValueError('Only held-out IDs in this batch can be exposed')
    path=args.root/'exposure.json';record=json.loads(path.read_text('utf-8'))
    record['events'].append({'kind':'content_read_or_debug','tasks':sorted(set(args.task)),'reason':args.reason,'actor':args.actor,'timestamp':datetime.now(timezone.utc).isoformat()})
    record['invalidated_holdout_ids']=sorted(set(record['invalidated_holdout_ids'])|set(args.task))
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n','utf-8');temp.replace(path)
    print('Exposure recorded; these items cannot count toward the sealed gate. Generate independent replacements.')


if __name__=='__main__':main()
