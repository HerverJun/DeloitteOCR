"""Resume the original paused continuation with a distinct authorized action.

The previous recovery's successful resume identity cannot execute a second
resume after the runtime-copy safety pause. Look up that completed action,
inspect the current original batch and issue one new intentional resume. This
never submits/recreates a batch, edits tasks, resets costs or changes policy.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from common import frozen_json, read_json
import controlled_recovery as recovery
import controlled_recovery_run as runner


def resume(execution_path,output):
    recovery.require_environment()
    execution_path=recovery.plain_path(execution_path);execution=read_json(execution_path)
    root=recovery.plain_path(execution['output']);runtime=recovery.plain_path(execution['runtime_root'])
    output=recovery.plain_path(output)
    if output.exists() or not output.is_relative_to(root):raise ValueError('A new action evidence scope is required')
    if (execution['gpu_slot']!='h60-controlled-recovery-20261008-002'
            or execution['helper']!=recovery.bound(Path(runner.__file__))
            or read_json(root/'launch-preflight.json').get('success') is not True):
        raise ValueError('Distinct resume is restricted to the newly authorized execution')
    started=read_json(root/'started.json');argv=started['native_service_actual_argv']
    port=int(argv[argv.index('--port')+1]);token=(root/'native-token.txt').read_text('utf-8')
    output.mkdir(parents=True,exist_ok=False)
    service=SimpleNamespace(base_url=f'http://127.0.0.1:{port}',token=token,trace=output/'native-http-timings.jsonl')
    batch_id=execution['original_batch_id'];endpoint=f'/api/projects/{read_json(execution["prepared"]["path"])["native_project_id"]}/screen-batches/{batch_id}'
    before=runner.native_http(service,'GET',endpoint+'?limit=1')
    old=runner.native_http(service,'POST',endpoint+'/action-lookup',
        {'action':'resume','request_id':'controlledRecoveryResume20261008'})
    with closing(recovery.connect(runtime/recovery.NATIVE_DB,readonly=True)) as db:
        batch=dict(db.execute('SELECT * FROM screen_batches WHERE id=?',(batch_id,)).fetchone())
        primary=[dict(row) for row in db.execute('SELECT * FROM screen_tasks WHERE batch_id=? ORDER BY rowid',(batch_id,))]
        contexts=[dict(row) for row in db.execute('SELECT * FROM screen_contexts WHERE batch_id=? ORDER BY rowid',(batch_id,))]
    if (before['status']!='paused' or batch['deadline']!=execution['deadline'] or batch['paused_at'] is not None
            or before['policy']!=execution['policy'] or len(primary)!=36 or len(contexts)!=60
            or {row['task_id'] for row in primary}!=set(execution['original_primary_task_ids'])
            or old.get('batch_id')!=batch_id or batch['deadline']<=datetime.now(timezone.utc).timestamp()):
        raise ValueError('Original completed action/current paused batch scope needs independent verification')
    request_id='h60RecoveryResume20261008Execution002'
    review={'format':'h60-distinct-authorized-resume-v1','utc':datetime.now(timezone.utc).isoformat(),
        'execution':recovery.bound(execution_path),'runner':execution['helper'],
        'batch_id':batch_id,'current_status':before['status'],
        'previous_completed_action':{key:old.get(key) for key in ('batch_id','status','request_id','counts')},
        'new_action_request_id':request_id,'primary_count':len(primary),'context_count':len(contexts),
        'deadline':batch['deadline'],'paused_at':batch['paused_at'],'policy_sha256':recovery.fingerprint(execution['policy']),
        'reason':'Previous execution already completed the old resume action; copied-runtime safety pause is a new explicit continuation action, not an unknown-outcome retry.',
        'root_authorization':'Continue original batch in exclusive h60-controlled-recovery-20261008-002 slot; original task costs/deadline preserved.',
        'batch_resubmitted':False,'tasks_or_costs_edited':False,'running_runner_changed':False,
        'truth_read':False,'performance_baseline_qualified':False}
    frozen_json(output/'review-before-action.json',review)
    response=runner.native_http(service,'POST',endpoint+'/action',{'action':'resume','request_id':request_id})
    after=runner.native_http(service,'GET',endpoint+'?limit=1')
    with closing(recovery.connect(runtime/recovery.NATIVE_DB,readonly=True)) as db:
        after_batch=dict(db.execute('SELECT * FROM screen_batches WHERE id=?',(batch_id,)).fetchone())
    if after_batch['deadline']!=batch['deadline'] or after_batch['paused_at'] is not None:
        raise ValueError('Intentional continuation unexpectedly changed the original wall allowance')
    report={**review,'action_response':{key:response.get(key) for key in ('batch_id','status','counts')},
            'status_after':after['status'],'counts_after':after['counts'],'deadline_unchanged':True,
            'runner_unchanged':execution['helper']==recovery.bound(Path(runner.__file__))}
    frozen_json(output/'result.json',report)
    print(json.dumps({key:report[key] for key in ('batch_id','new_action_request_id','status_after',
                                               'counts_after','deadline_unchanged','runner_unchanged')}))
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execution',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();resume(args.execution,args.output)


if __name__=='__main__':main()
