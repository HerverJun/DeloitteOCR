"""Cold-restore the complete actual precision batch using current frozen readers.

Original DB/WAL/SHM bytes are only copied and hashed. Current schema migration,
path relocation, product backup/restore and HTTP reads run on owned F: copies.
This supplies engineering recovery evidence, not a new model/Agent trial.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import require_environment
from cold_backup_restore_native import (checkpoint_owned, database_state,
    freeze_consumers, inventory, read_native, require)


def execution_ledger(root):
    with closing(sqlite3.connect(root/'workbench.sqlite3')) as db:
        db.row_factory=sqlite3.Row
        queries={
            'batches':'SELECT id,project_id,request_id,payload_hash,policy,status,parent_batch_id,deadline FROM screen_batches ORDER BY id',
            'items':'SELECT batch_id,input_version_id,image_id,position,source_hash,status,stage,pass_index,attempt,result_id,result_revision,review_state,error,started FROM screen_items ORDER BY batch_id,position',
            'tasks':'SELECT id,project_id,version_id,engine,kind,status,started,finished,error FROM tasks ORDER BY id',
            'model_costs':'SELECT batch_id,input_version_id,task_id,pass_index,attempt,spent_seconds FROM screen_tasks ORDER BY task_id',
            'context_costs':'SELECT batch_id,input_version_id,task_id,spent_seconds FROM screen_contexts ORDER BY task_id',
        }
        return {name:[dict(row) for row in db.execute(query)] for name,query in queries.items()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-run','output','platform-source','native-source','bundle',
                 'pdf-layer','native-python','agent-layer'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    require_environment()
    output,source_run=fpath(args.output),fpath(args.source_run)
    require(not output.exists(),'Use a new cold-restore evidence identity')
    result=read_json(source_run/'result.json')
    require(result['status']=='terminal' and result['counts']['pending']==0,
            'Cold source must be the complete stopped actual batch')
    args.expected_inputs=result['scope_inputs']
    args.batch_id=result['batch_id']
    # read_native denies both original roots; this run has no S07 component.
    args.s07_run=str(source_run)
    args.additional_forbid_data_roots=[]
    output.mkdir(parents=True)
    original=source_run/'native/ocr'
    before=inventory(original)
    frozen_json(output/'original-native-bytes-before.json',before)
    consumers=freeze_consumers(args,output)
    platform=fpath(args.platform_source)
    sys.path[:0]=[str(platform),str(platform/'src')]
    from scripts.backup_workspace import create,verify,restore,_relocate_screen_paths
    from scripts.product_compatibility import check,DATA_COMPATIBILITY
    sources,restored=output/'sources',output/'restored'
    copied=sources/'native/ocr'
    copied.parent.mkdir(parents=True)
    shutil.copytree(original,copied)
    require(inventory(copied)==before and inventory(original)==before,'Exact original cold copy changed')
    # WAL recovery is confined to this fresh copy.
    checkpoint_owned(copied)
    ledger=execution_ledger(copied)
    frozen_json(output/'original-execution-ledger.json',ledger)
    files={name:{'sha256':value,'sqlite':name.endswith('.sqlite3')}
           for name,value in inventory(copied).items() if not name.endswith(('-wal','-shm'))}
    relocation=_relocate_screen_paths(copied,copied,{
        'kind':'ocr','source_data_dir':str(original),'files':files})
    source_read=read_native(args,copied,output,'source')
    require(execution_ledger(copied)==ledger,'Current migration/read changed identities, budgets, costs or failures')
    checkpoint_owned(sources)
    source_state=database_state(sources)
    cold_files=inventory(sources)
    frozen_json(output/'source-logical-state.json',source_state)
    backup=output/'backup'
    manifest=create(copied,backup,kind='ocr')
    require(verify(backup)==manifest,'Product backup verification changed manifest')
    receipt=restore(backup,restored/'native/ocr')
    require(inventory(sources)==cold_files,'Product backup/restore changed stopped source files')
    isolated=output/'isolated-cold-sources'
    require(sources.resolve().is_relative_to(output) and isolated.resolve().is_relative_to(output)
            and not isolated.exists(),'Cold isolation paths escaped the owned F: output')
    sources.rename(isolated)
    require(not sources.exists() and inventory(isolated)==cold_files,'Owned source isolation changed bytes')
    restored_before=database_state(restored)
    require(restored_before==source_state,'Product restore changed logical business records')
    check(restored,DATA_COMPATIBILITY)
    restored_read=read_native(args,restored/'native/ocr',output,'restored')
    for field in ('version_vector','details','policy','source_and_prepared_files',
                  'existing_export_files','local_candidate_reads'):
        require(source_read[field]==restored_read[field],'Restored actual HTTP field differs: '+field)
    require(execution_ledger(restored/'native/ocr')==ledger,'Restored reader changed the original execution ledger')
    require(database_state(restored)==restored_before,'Restored read-only consumer changed business rows')
    require(inventory(original)==before and inventory(isolated)==cold_files,'A protected original/cold source changed')
    frozen_json(output/'restored-logical-state.json',restored_before)
    frozen_json(output/'report.json',{'format':'precision-route-actual-cold-restore-v1','status':'passed',
        'source_run':str(source_run),'source_result_sha256':digest(source_run/'result.json'),
        'scope_inputs':args.expected_inputs,'batch_id':args.batch_id,
        'current_consumer_source_inventory_sha256':consumers['inventory_sha256'],
        'current_reader_schema':source_state['native/ocr/workbench.sqlite3']['user_version'],
        'original_native_bytes_unchanged':True,
        'original_execution_ledger_preserved':True,'owned_former_data_root_absent':True,
        'source_and_restored_http_identity_and_files_equal':True,
        'initial_owned_path_relocation':relocation,'product_restore_receipt':receipt,
        'backup_manifest_sha256':digest(backup/'manifest.json'),
        'actual_model_attempts':len(ledger['model_costs']),
        'actual_failed_model_attempts':sum(row['status']=='failed' and row['kind']!='screen_context' for row in ledger['tasks']),
        'source_pixel_read_events':source_read['actual_read_audit']['diagnostics_source_pixel_read_events'],
        'restored_pixel_read_events':restored_read['actual_read_audit']['diagnostics_source_pixel_read_events'],
        'current_queues_started':False,'new_model_calls':0,'new_agent_calls':0,
        'formal_current':'v0.9.3','final_model_or_agent_qualification':False,
        'review_relief_qualified':False,'driver_sha256':digest(__file__)})
    print(json.dumps({'status':'passed','scope_inputs':args.expected_inputs,'new_model_calls':0,'new_agent_calls':0}))


if __name__=='__main__':main()
