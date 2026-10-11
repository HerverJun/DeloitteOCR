"""Audit historical technical failures without reopening or reviving a run."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from common import digest,fpath,frozen_json,read_json
from controlled_recovery import bound,require_environment


def clone(folder,output):
    files={p.name:digest(p) for p in folder.glob('workbench.sqlite3*') if p.is_file()}
    output.mkdir()
    for name,sha in files.items():
        shutil.copyfile(folder/name,output/name)
        if digest(output/name)!=sha:raise ValueError('Ledger copy differs')
    return files


def records(folder):
    with closing(sqlite3.connect(folder/'workbench.sqlite3')) as db:
        db.row_factory=sqlite3.Row
        batches={row['id']:dict(row) for row in db.execute('SELECT * FROM screen_batches')}
        result=[]
        for item in db.execute('SELECT * FROM screen_items'):
            row=dict(item);policy=json.loads(batches[row['batch_id']]['policy'])
            tasks=[]
            for table in ('screen_tasks','screen_contexts'):
                for task in db.execute('SELECT t.*,s.spent_seconds FROM '+table+' s JOIN tasks t ON t.id=s.task_id '
                        'WHERE s.batch_id=? AND s.input_version_id=?',(row['batch_id'],row['input_version_id'])):
                    task=dict(task)
                    if task['status'] not in {'succeeded','failed','cancelled'} or not task['finished']:
                        raise ValueError('Audit requires terminal, finished tasks')
                    task['active_seconds']=max(0.,(datetime.fromisoformat(task['finished'])-
                        datetime.fromisoformat(task['started'])).total_seconds()) if task['started'] else 0.
                    task['ledger']=table;tasks.append(task)
            prep=json.loads(row['preparation']) if row['preparation'] else {}
            cost=sum(t['active_seconds']+t['spent_seconds'] for t in tasks)+prep.get('preparation_seconds',0)
            result.append({'source_hash':row['source_hash'],'input_version_id':row['input_version_id'],
                'status':row['status'],'per_source_budget_seconds':policy['budget_seconds'],
                'original_deadline':batches[row['batch_id']]['deadline'],
                'preparation_seconds':prep.get('preparation_seconds',0),
                'source_cost_seconds':cost,'remaining_at_terminal_seconds':max(0.,policy['budget_seconds']-cost),
                'tasks':tasks})
        return result


def main():
    require_environment()
    root=fpath('F:/OCR/output/platform-specialized-agent-20261007')
    output=fpath(sys.argv[1])
    if output.exists():raise ValueError('New immutable audit identity required')
    output.mkdir()
    g=root/'evaluation/m1-glm-validation-direct-independent-006-002'
    h=root/'evaluation/m1-h-math-validation-direct-independent-007-001'
    original={str(folder):clone(folder,output/name) for folder,name in ((g,'glm-copy'),(h,'hunyuan-copy'))}
    gm,hm=records(output/'glm-copy'),records(output/'hunyuan-copy')
    if len(gm)!=40 or len(hm)!=40 or {r['source_hash'] for r in gm}!={r['source_hash'] for r in hm}:
        raise ValueError('Full native source scope differs')
    by_sha={r['source_hash']:r for r in hm}
    failures=[]
    for row in gm:
        if row['status']!='failed':continue
        comparison=by_sha[row['source_hash']]
        h_tasks=[t for t in comparison['tasks'] if t['ledger']=='screen_tasks']
        h_seconds=sum(t['active_seconds']+t['spent_seconds'] for t in h_tasks)
        failures.append({'glm':row,'independent_h_tasks':h_tasks,'h_observed_ocr_seconds':h_seconds,
            'historical_reserve_at_least_one_second':row['remaining_at_terminal_seconds']>=1,
            'historical_remaining_exceeds_observed_h_ocr':row['remaining_at_terminal_seconds']>=h_seconds,
            'new_inference_permitted_by_audit':False})
    for folder,files in original.items():
        if {p.name:digest(p) for p in Path(folder).glob('workbench.sqlite3*') if p.is_file()}!=files:
            raise ValueError('Prior independent evidence changed')
    report={'format':'historical-precision-fallback-budget-audit-v1','scope_inputs':40,
        'source_byte_hashes':original,'bindings':[bound(g/'review.json'),bound(h/'review.json'),bound(__file__)],
        'technical_failures':failures,'failures_with_one_second_reserve':sum(r['historical_reserve_at_least_one_second'] for r in failures),
        'original_identities_costs_deadlines_unchanged':True,'native_databases_opened_only_from_new_copies':True,
        'original_runs_revived':False,'model_executed':False,'truth_read':False,'holdout_read':False,
        'new_precision_route_qualified':False,
        'conclusion':'A blanket three-image H fallback is not budget-feasible. One GLM failure already consumed nearly all of its 360-second original source budget; a future route must reserve fallback capacity before the first pass. Old failures remain terminal.'}
    frozen_json(output/'report.json',report)
    print({'technical_failures':len(failures),'with_one_second_reserve':report['failures_with_one_second_reserve'],
        'remaining_seconds':[round(r['glm']['remaining_at_terminal_seconds'],3) for r in failures]})


if __name__=='__main__':main()
