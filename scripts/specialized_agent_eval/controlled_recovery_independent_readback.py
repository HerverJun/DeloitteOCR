"""Independently read a prepared H60 continuation using new SQLite byte copies."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import shutil

from common import fingerprint, frozen_json, read_json
import controlled_recovery as recovery


def readback(prepared_path,binding_path,cpu_path,output):
    recovery.require_environment()
    prepared_path=recovery.plain_path(prepared_path);prepared=read_json(prepared_path)
    output=recovery.plain_path(output);runtime=recovery.plain_path(prepared['runtime_root'])
    if output.exists() or output.is_relative_to(runtime) or runtime.is_relative_to(output):
        raise ValueError('Independent readback requires a new nonoverlapping F scope')
    verified=recovery.validate_launch(prepared_path,binding_path)
    original=recovery.plain_path(prepared['original_run'])
    original_files=read_json(prepared['original_inventory']['path'])
    original_dirs=read_json(prepared['original_directories']['path'])
    runtime_files=read_json(prepared['runtime_inventory']['path'])
    runtime_dirs=read_json(prepared['runtime_directories']['path'])
    if (recovery.file_inventory(original)!=original_files or recovery.directory_inventory(original)!=original_dirs
            or recovery.file_inventory(prepared['original_byte_copy'])!=original_files
            or recovery.directory_inventory(prepared['original_byte_copy'])!=original_dirs):
        raise ValueError('Preserved failed continuation or its byte evidence changed')
    cpu=read_json(cpu_path)
    if cpu.get('success') is not True or cpu.get('actual_recovery_proven') is not False:
        raise ValueError('Actual services CPU initialization qualification is required')
    output.mkdir(parents=True,exist_ok=False);copies=output/'sqlite-byte-copies';copies.mkdir()
    for name,entry in runtime_files.items():
        if Path(name).suffix in {'.sqlite3','.sqlite','.db'} or name.endswith(('-wal','-shm')):
            destination=recovery.plain_path(copies/name);destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(runtime/name,destination)
            if recovery.file_sha(destination)!=entry['sha256']:raise ValueError('Independent SQLite byte copy changed')
    for name in runtime_files:
        if Path(name).suffix in {'.sqlite3','.sqlite','.db'}:
            recovery.consolidate_database(copies/name,copies)
    before=read_json(Path(prepared['recovery_root'])/'evidence/native-state-before.json')
    after=recovery.native_state(copies/recovery.NATIVE_DB)
    expected_after=read_json(Path(prepared['recovery_root'])/'evidence/native-state-after.json')
    if after!=expected_after:raise ValueError('Prepared native state differs from its recorded readback')
    if len(after['screen_contexts'])!=60 or len(after['screen_tasks'])!=36:
        raise ValueError('Independent H60 identity counts differ')
    closed_before=[row for row in before['tasks'] if row['status']=='interrupted']
    closed_after=[row for row in after['tasks'] if row['status']=='interrupted']
    if closed_before!=closed_after or len(closed_after)!=1:raise ValueError('Closed actual execution segment changed')
    budget_keys=('task_id','attempt','spent_seconds')
    if ([{key:row[key] for key in budget_keys} for row in before['screen_tasks']]
            != [{key:row[key] for key in budget_keys} for row in after['screen_tasks']]):
        raise ValueError('Historical task cost or attempt was reset')
    checks=recovery.pixel_geometry_checks(after,runtime,original_files,original)
    with closing(recovery.connect(copies/'assets.sqlite3',readonly=True)) as db:
        assets=[dict(row) for row in db.execute('SELECT * FROM registered_asset ORDER BY asset_id')]
        versions=[dict(row) for row in db.execute('SELECT * FROM asset_set_versions ORDER BY asset_set_id,revision')]
    with closing(recovery.connect(copies/'agent/operations.sqlite3',readonly=True)) as db:
        agent_runs=[dict(row) for row in db.execute('SELECT id,status FROM runs')]
        operations=[dict(row) for row in db.execute('SELECT id,tool,state FROM operations')]
    if (len(assets)!=60 or len(agent_runs)!=1 or agent_runs[0]['status']!='needs_verification'
            or not any(row['tool']=='ocr_screen_batch_run' and row['state']=='pending' for row in operations)):
        raise ValueError('Original assets or durable Agent pending operation were replaced')
    directories=(runtime/'blobs').is_dir() and not any((runtime/'blobs').iterdir())
    intact=(recovery.file_inventory(runtime)==runtime_files and recovery.directory_inventory(runtime)==runtime_dirs
            and recovery.file_inventory(original)==original_files and recovery.directory_inventory(original)==original_dirs)
    if not directories or not intact:raise ValueError('Independent readback changed the original or runtime scene')
    scan=recovery.scan_runtime_paths(copies,original)
    report={'format':'controlled-recovery-followup-independent-readback-v1','success':True,
            'utc':datetime.now(timezone.utc).isoformat(),'prepared':recovery.bound(prepared_path),
            'source_binding':recovery.bound(binding_path),'restore_cpu':recovery.bound(cpu_path),
            'native_batch_id':prepared['batch_id'],'input_count':len(after['screen_items']),
            'contexts_reused':60,'existing_primary_task_ids_preserved':36,'tasks_created':0,'assets_created':0,
            'asset_count':len(assets),'fixed_set_version_count':len(versions),'assets_sha256':fingerprint(assets),
            'paused_task_count':sum(row['status']=='paused' for row in after['tasks']),
            'closed_task_segments':prepared['lineage']['closed_execution_segments'],
            'closed_segments_costs_attempts_unchanged':True,
            'deadline':prepared['deadline'],'deadline_increased':False,
            'original_agent_run':agent_runs[0],'original_pending_operations':operations,
            'geometry_pixel_checks':len(checks),'geometry_pixels_unchanged':True,
            'empty_original_blob_directory_restored':directories,'path_scan':scan,
            'failed_runtime_and_evidence_unchanged':True,'prepared_runtime_unchanged':True,
            'complete_frozen_source_verified':verified['launch_source_and_scope_verified'],
            'original_or_prepared_sqlite_opened':False,'only_independent_sqlite_byte_copies_opened':True,
            'service_or_model_started':False,'agent_started_or_reconciled':False,
            'actual_recovery_proven':False,'performance_baseline_qualified':False,'truth_read':False,
            'helper':recovery.bound(Path(__file__))}
    frozen_json(output/'result.json',report)
    print(json.dumps({key:report[key] for key in ('success','contexts_reused','existing_primary_task_ids_preserved',
                    'asset_count','geometry_pixel_checks','prepared_runtime_unchanged','service_or_model_started')}))
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('prepared','source-binding','restore-cpu','output'):p.add_argument('--'+key,required=True)
    args=p.parse_args();readback(args.prepared,args.source_binding,args.restore_cpu,args.output)


if __name__=='__main__':main()
