"""Seal a stopped recovery failure and prepare its exact continuation, CPU only.

No original SQLite database is opened. This command preserves closed historical
segments and tasks created by the original batch coordinator; it never submits
a batch, creates an asset or invokes a service, controller or OCR model.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from common import fingerprint, frozen_json, read_json
import controlled_recovery as recovery


def seal(prepared_path, execution, output, processes, terminal):
    recovery.require_environment()
    prepared_path = recovery.plain_path(prepared_path)
    initial = read_json(prepared_path)
    source = recovery.plain_path(initial['runtime_root'])
    execution, output = recovery.plain_path(execution), recovery.plain_path(output)
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('A new nonoverlapping evidence directory is required')
    result = read_json(execution/'result.json')
    cleanup = read_json(execution/'native-own-cleanup.json')
    started = read_json(execution/'started.json')
    process_binding = recovery.bound(processes)
    snapshot = json.loads(Path(processes).read_text('utf-8-sig'))
    if (snapshot.get('matching_python_or_llama_processes') != []
            or snapshot.get('owned_pids') != [started['runner_pid'],cleanup['pid'],
                                            *[row['pid'] for row in cleanup['workers']]]):
        raise ValueError('Stopped owned process identity snapshot is incomplete')
    terminal_binding = recovery.bound(terminal)
    handle = read_json(terminal)
    if (handle.get('session_id') != 45604 or handle.get('exit_code') != 1
            or handle.get('observation_timeout') is not False or handle.get('active_restart') is not False):
        raise ValueError('The original failed actual continuation terminal proof is required')
    expected, directories = recovery.file_inventory(source), recovery.directory_inventory(source)
    output.mkdir(parents=True,exist_ok=False)
    recovery.copy_bytes(source,output/'failed-runtime-byte-copy',expected,directories)
    recovery.copy_bytes(output/'failed-runtime-byte-copy',output/'logical-readback-copy',expected,directories)
    logical = output/'logical-readback-copy'
    for name in expected:
        if Path(name).suffix in {'.sqlite3','.sqlite','.db'}:
            recovery.consolidate_database(logical/name,logical)
    state = recovery.native_state(logical/recovery.NATIVE_DB)
    for name, rows in state.items():
        sealed = execution/'native-tables'/(name+'.json')
        if sealed.is_file() and read_json(sealed)!=rows:
            raise ValueError('Failed runtime differs from terminal sealed native tables')
    tasks = {row['id']:row for row in state['tasks']}
    current = [row['task_id'] for row in state['screen_tasks']]
    closed = [row for row in state['tasks'] if row['id'] in set(current) and row['status']=='interrupted']
    for row in closed:
        first,last = (datetime.fromisoformat(row[key].replace('Z','+00:00')) for key in ('started','finished'))
        if first.tzinfo is None or last.tzinfo is None or last < first:
            raise ValueError('Closed actual execution segment is invalid')
    frozen_json(output/'original-files.json',expected)
    frozen_json(output/'original-directories.json',directories)
    frozen_json(output/'native-state.json',state)
    lineage = {'original_preparation':recovery.bound(prepared_path),
        'execution_result':recovery.bound(execution/'result.json'),
        'native_cleanup':recovery.bound(execution/'native-own-cleanup.json'),
        'original_directory_inventory':recovery.bound(output/'original-directories.json'),
        'terminal_proof':terminal_binding,
        'base_primary_task_ids':initial['preserved_primary_tasks'],
        'added_primary_task_ids':[key for key in current if key not in initial['preserved_primary_tasks']],
        'preserved_primary_count':len(current),
        'closed_execution_segments_sha256':fingerprint(closed),
        'closed_execution_segments':[
            {'task_id':row['id'],'started':row['started'],'finished':row['finished'],
             'actual_segment_seconds':(datetime.fromisoformat(row['finished'])-
                                     datetime.fromisoformat(row['started'])).total_seconds(),
             'carried_spent_seconds':next(item['spent_seconds'] for item in state['screen_tasks']
                                          if item['task_id']==row['id'])} for row in closed],
        'history_note':'Initial H tasks, coordinator-created continuation tasks and the closed actual segment are all preserved. Remaining inputs may only be dispatched by the existing original batch coordinator.',
        'new_batch_created':False,'new_assets_created':False,'costs_reset':False,'deadline_increased':False}
    restore = []
    original_blob = recovery.plain_path(initial['original_run'])/'blobs'
    if not (source/'blobs').exists() and original_blob.is_dir() and not any(original_blob.iterdir()):
        restore=[{'relative':'blobs','source':str(original_blob),'source_empty':True}]
    receipt = {'format':recovery.FOLLOWUP_FORMAT,'original_run':str(source),
        'all_original_run_files_unchanged':True,'sqlite_originals_opened':False,
        'model_invoked':False,'original_recovery_invoked':False,
        'original_inventory_before':recovery.bound(output/'original-files.json'),
        'failure':{'terminal_handle':{key:handle[key] for key in
                    ('session_id','exit_code','observation_timeout','active_restart')},
            'terminal_handle_source':handle['source'],
            'matching_owned_python_or_llama_processes':0,'process_snapshot':process_binding},
        'timeline':{'file':recovery.bound(execution/'stage-timestamps.jsonl')},
        'inputs':{'count':len(state['screen_items'])},
        'native':{'batch':state['screen_batches'][0],
            'context_success_count':sum(tasks[row['task_id']]['status']=='succeeded' for row in state['screen_contexts']),
            'context_tasks':state['screen_contexts'],'primary_tasks':state['screen_tasks']},
        'lineage':lineage,'restore_empty_directories':restore,
        'byte_copy':str(output/'failed-runtime-byte-copy'),
        'logical_readback_copy':str(logical),'service_or_model_started':False,'truth_read':False}
    recovery.stop_evidence(source, _write(output/'failure-receipt.json',receipt), expected)
    recovery.validate_native(state,receipt)
    if recovery.file_inventory(source)!=expected or recovery.directory_inventory(source)!=directories:
        raise ValueError('Failed original scene changed while it was sealed')
    return receipt


def _write(path,value):
    frozen_json(path,value)
    return path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared',required=True)
    p.add_argument('--execution',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--processes',required=True)
    p.add_argument('--terminal',required=True)
    args=p.parse_args()
    value=seal(args.prepared,args.execution,args.output,args.processes,args.terminal)
    print(json.dumps({'failed_scene_sealed':True,'primary_count':value['lineage']['preserved_primary_count'],
                      'service_or_model_started':False,'original_sqlite_opened':False}))


if __name__=='__main__':main()
