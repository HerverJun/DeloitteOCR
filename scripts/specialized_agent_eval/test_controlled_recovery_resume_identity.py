"""Reproduce completed-action reuse using actual frozen native SQLite code.

Only a sealed DB byte copy is opened. The queue is an explicit CPU fake and
there is no service, worker, image inference, controller or truth access.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sys
import threading

from common import fingerprint, frozen_json, read_json
import controlled_recovery as recovery


class CpuQueue:
    def __init__(self):self.wake=threading.Event();self.calls=[]
    def action(self,project,action,task_ids):self.calls.append({'project_id':project,'action':action,'task_ids':task_ids})


def check(sealed_database,freeze_path,output):
    recovery.require_environment()
    sealed_database=recovery.plain_path(sealed_database);output=recovery.plain_path(output)
    if output.exists():raise ValueError('A new CPU reproduction output is required')
    frozen=recovery.frozen_sources(freeze_path);ocr=Path(frozen['sources']['ocr']['path'])
    sys.path[:0]=[str(ocr/'src'),'F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/build/public-quality-20260916/tool/site-packages']
    from ocr_workbench.store import Store
    from ocr_workbench.screen_batches import ScreenBatches
    output.mkdir(parents=True,exist_ok=False);native=output/'native';native.mkdir()
    destination=native/'workbench.sqlite3';source_binding=recovery.bound(sealed_database)
    shutil.copy2(sealed_database,destination);recovery.consolidate_database(destination,native)
    store=Store(native);queue=CpuQueue();manager=ScreenBatches(store,queue,output,hold_wal=False)
    try:
        batch=store.rows('SELECT * FROM screen_batches')[0]
        with store.transaction() as db:
            db.execute("UPDATE screen_batches SET status='paused' WHERE id=?",(batch['id'],))
            db.execute("UPDATE screen_items SET status='paused' WHERE batch_id=?",(batch['id'],))
            db.execute("UPDATE tasks SET status='paused' WHERE status='queued'")
        before=store.rows('SELECT * FROM screen_tasks ORDER BY rowid')
        old_body={'action':'resume','request_id':'controlledRecoveryResume20261008'}
        original_actions=store.rows('SELECT * FROM screen_actions ORDER BY rowid')
        old=manager.action(batch['project_id'],batch['id'],old_body)
        old_status=store.rows('SELECT status FROM screen_batches')[0]['status']
        old_calls=len(queue.calls)
        fresh_body={'action':'resume','request_id':'h60RecoveryResume20261008Execution002'}
        fresh=manager.action(batch['project_id'],batch['id'],fresh_body)
        fresh_status=store.rows('SELECT status FROM screen_batches')[0]['status']
        fresh_calls=len(queue.calls)
        repeat=manager.action(batch['project_id'],batch['id'],fresh_body)
        repeated_calls=len(queue.calls)
        final_batch=store.rows('SELECT * FROM screen_batches')[0]
        after=store.rows('SELECT * FROM screen_tasks ORDER BY rowid')
        checks={'completed_old_identity_leaves_new_copy_paused':old_status=='paused' and old_calls==0,
                'distinct_intent_resumes_same_original_batch':fresh_status=='running' and fresh_calls==1,
                'same_execution_retry_is_idempotent':fresh==repeat and fresh_calls==repeated_calls,
                'same_task_ids_costs_attempts_metadata':before==after,
                'original_deadline_and_paused_at_unchanged':batch['deadline']==final_batch['deadline'] and final_batch['paused_at'] is None,
                'no_new_batch_or_tasks':len(store.rows('SELECT * FROM screen_batches'))==1 and len(after)==36,
                'original_action_receipt_unchanged':all(row in store.rows('SELECT * FROM screen_actions ORDER BY rowid') for row in original_actions)}
        result={'format':'h60-native-resume-identity-cpu-reproduction-v1','checks':checks,'success':all(checks.values()),
                'source_database':source_binding,'source_database_unchanged':source_binding==recovery.bound(sealed_database),
                'frozen_source':frozen['freeze'],'old_request_id':old_body['request_id'],'new_request_id':fresh_body['request_id'],
                'old_receipt_status':old.get('status'),'actual_state_after_old_action':old_status,
                'actual_state_after_distinct_action':fresh_status,'task_count':len(after),
                'tasks_before_sha256':fingerprint(before),'tasks_after_sha256':fingerprint(after),
                'fake_queue_calls':queue.calls,'service_or_model_started':False,'agent_or_controller_called':False,
                'actual_recovery_proven':False,'truth_read':False,'helper':recovery.bound(Path(__file__))}
        frozen_json(output/'result.json',result)
        print(json.dumps({'success':result['success'],'checks':checks,'service_or_model_started':False}))
        if not result['success']:raise SystemExit(1)
        return result
    finally:manager.stop()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('sealed-database','source-freeze','output'):p.add_argument('--'+key,required=True)
    args=p.parse_args();check(args.sealed_database,args.source_freeze,args.output)


if __name__=='__main__':main()
