"""F-drive CPU migration/refusal tests; no service, controller or model is run."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import struct
import unittest
import zlib

from common import fpath, frozen_json, read_json
import controlled_recovery as recovery

OUTPUT = None


def png(width=3,height=2):
    def chunk(kind,payload):
        return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff'*width*3)*height))+chunk(b'IEND',b'')


def fixture(root):
    original = root/'original'
    original.mkdir()
    (original/'native/ocr/images').mkdir(parents=True)
    (original/'native/ocr/prepared').mkdir()
    (original/'inputs').mkdir()
    (original/'blobs').mkdir()
    (original/'empty/nested').mkdir(parents=True)
    for folder in ('native/ocr/images','native/ocr/prepared','inputs'):
        for label in ('a','b'):
            (original/folder/(label+'.png')).write_bytes(png())
    stopped = datetime.now(timezone.utc)-timedelta(seconds=20)
    started = stopped-timedelta(seconds=5.5)
    native = original/recovery.NATIVE_DB
    db = sqlite3.connect(native)
    db.executescript('''
        CREATE TABLE screen_batches(id TEXT,project_id TEXT,request_id TEXT,payload_hash TEXT,policy TEXT,status TEXT,deadline REAL,paused_at REAL);
        CREATE TABLE screen_items(batch_id TEXT,input_version_id TEXT,image_id TEXT,position INT,source_hash TEXT,status TEXT,stage TEXT,attempt INT,pass_index INT,preparation TEXT,evidence TEXT,result_id TEXT);
        CREATE TABLE tasks(id TEXT,engine TEXT,kind TEXT,status TEXT,phase TEXT,started TEXT,finished TEXT);
        CREATE TABLE screen_contexts(task_id TEXT,batch_id TEXT,input_version_id TEXT,raw TEXT,artifact TEXT,spent_seconds REAL);
        CREATE TABLE screen_tasks(task_id TEXT,batch_id TEXT,input_version_id TEXT,prepared_id TEXT,prepared_path TEXT,metadata TEXT,attempt INT,spent_seconds REAL);
        CREATE TABLE ocr_submissions(project_id TEXT,request_id TEXT,input_hash TEXT,task_ids TEXT);
        CREATE TABLE versions(id TEXT,image_id TEXT,path TEXT,sha256 TEXT,width INT,height INT);
        CREATE TABLE images(id TEXT,active_version TEXT,original_path TEXT);
        CREATE TABLE results(id TEXT);
        CREATE TABLE selections(image_id TEXT,result_id TEXT);
        CREATE TABLE screen_final_tasks(task_id TEXT);
        CREATE TABLE reviews(image_id TEXT);
    ''')
    policy = {'budget_seconds':360,'batch_budget_seconds':36000,'strategy_version':'fixture-unchanged-v1'}
    db.execute('INSERT INTO screen_batches VALUES(?,?,?,?,?,?,?,?)',('batch1','project1','original-request1','original-payload-hash',json.dumps(policy),'running',datetime.now(timezone.utc).timestamp()+10000,None))
    for index,label in enumerate(('a','b')):
        version, image = 'version-'+label,'image-'+label
        pixel = recovery.file_sha(original/'native/ocr/images'/(label+'.png'))
        prepared_path = original/'native/ocr/prepared'/(label+'.png')
        geometry = {'width':3,'height':2,'source_to_prepared':[[1,0,0],[0,1,0],[0,0,1]],
            'prepared_to_source':[[1,0,0],[0,1,0],[0,0,1]],'source_polygon':[[0,0],[3,0],[3,2],[0,2]]}
        prepared = {'id':'prepared-'+label,'path':str(prepared_path),**geometry}
        metadata = {**prepared,'prepared_pixel_sha256':pixel,'input_version_id':version,'batch_id':'batch1',
            'budget_reservation_seconds':350,'timeout_seconds':350,'request_timeout_seconds':180,
            'model_identity':'original-model-identity','cache_key':'original-cache-'+label,'attempt':0}
        preparation = {'input_path':str(original/'native/ocr/images'/(label+'.png')),
                       'input_sha256':pixel,'prepared_images':[prepared],'preparation_seconds':1}
        db.execute('INSERT INTO screen_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('batch1',version,image,index,pixel,'running','first_pass',0,0,json.dumps(preparation),json.dumps({'layout':[],'text':[]} ),None))
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('context-'+label,'geometry','screen_context','succeeded','done',started.isoformat(),stopped.isoformat()))
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',('primary-'+label,'hunyuan','ocr','running' if index==0 else 'queued','loading',started.isoformat() if index==0 else None,None))
        db.execute('INSERT INTO screen_contexts VALUES(?,?,?,?,?,?)',('context-'+label,'batch1',version,json.dumps({'regions':[],'coordinate_space':'source_pixels'}),None,2))
        db.execute('INSERT INTO screen_tasks VALUES(?,?,?,?,?,?,?,?)',('primary-'+label,'batch1',version,'prepared-'+label,'prepared/'+label+'.png',json.dumps(metadata),0,7.25))
        db.execute('INSERT INTO ocr_submissions VALUES(?,?,?,?)',('project1','native-original-'+label,'identity-hash-'+label,json.dumps(['primary-'+label])))
        db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',(version,image,'images/'+label+'.png',pixel,3,2))
        db.execute('INSERT INTO images VALUES(?,?,?)',(image,version,'images/'+label+'.png'))
    db.commit()
    db.close()
    (original/'agent').mkdir()
    db = sqlite3.connect(original/'agent/operations.sqlite3')
    db.executescript('CREATE TABLE runs(id TEXT,status TEXT); CREATE TABLE operations(id TEXT,state TEXT,tool TEXT); CREATE TABLE registered_inputs(id TEXT,body TEXT);')
    db.execute('INSERT INTO runs VALUES(?,?)',('original-agent-run','needs_verification'))
    db.execute('INSERT INTO operations VALUES(?,?,?)',('original-operation','pending','ocr_screen_batch_run'))
    db.execute('INSERT INTO registered_inputs VALUES(?,?)',('asset1',json.dumps({'file':str(original/'inputs/a.png')})))
    db.commit()
    db.close()
    frozen_json(original/'input-map.json',{'items':[{'position':index,'sample_id':label,'normalized_input':str(original/'inputs'/(label+'.png'))} for index,label in enumerate(('a','b'))]})
    frozen_json(original/'stage-timestamps.json',{'events':[{'stage':'cleanup','event':'finished','utc_timestamp':stopped.isoformat()}]})
    (original/'native-service.log').write_bytes(b'')
    (root/'console.log').write_text('original failure\n','utf-8')
    return original, stopped


def stop_receipt(root,original):
    evidence = root/'failure'
    evidence.mkdir(exist_ok=True)
    state = recovery.native_state(original/recovery.NATIVE_DB)
    inventory = recovery.file_inventory(original)
    frozen_json(evidence/'original-files.json',inventory)
    frozen_json(evidence/'processes.json',{'matching_python_or_llama_processes':[]})
    receipt = {'format':'h-development-failure-readback-v1','original_run':str(original),
        'all_original_run_files_unchanged':True,'sqlite_originals_opened':False,
        'model_invoked':False,'original_recovery_invoked':False,
        'original_inventory_before':recovery.bound(evidence/'original-files.json'),
        'failure':{'terminal_handle':{'session_id':1234,'exit_code':1,'observation_timeout':False,'active_restart':False},
            'terminal_handle_source':'CPU fixture only; not an actual model execution',
            'matching_owned_python_or_llama_processes':0,'process_snapshot':recovery.bound(evidence/'processes.json')},
        'timeline':{'file':recovery.bound(original/'stage-timestamps.json')},
        'inputs':{'count':2},'native':{'batch':state['screen_batches'][0],'context_success_count':2,
            'context_tasks':state['screen_contexts'],'primary_tasks':state['screen_tasks']}}
    frozen_json(evidence/'failure-receipt.json',receipt)
    return evidence/'failure-receipt.json'


def freeze(root):
    output = root/'source-snapshot'
    output.mkdir()
    sources = {}
    for name,names in recovery.REQUIRED_SOURCE_FILES.items():
        directory = output/name
        directory.mkdir()
        for relative in names:
            path = directory/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('# CPU fixture only; never executable qualification\n','utf-8')
        sources[name] = {'current_working_contents_frozen':True,
            'files':{key:value['sha256'] for key,value in recovery.file_inventory(directory).items()}}
    frozen_json(output/'freeze.json',{'sources':sources,'fixture_only':True})
    return output/'freeze.json'


class ControlledRecovery(unittest.TestCase):
    def setUp(self):
        self.root = OUTPUT/self._testMethodName
        self.root.mkdir(parents=True,exist_ok=False)
        self.original,self.stopped = fixture(self.root)
        self.receipt = stop_receipt(self.root,self.original)
        self.target = self.root/'recovery'

    def test_preserves_identity_cost_geometry_and_original_bytes(self):
        before = recovery.file_inventory(self.original)
        original_state = recovery.native_state(self.original/recovery.NATIVE_DB)
        value = recovery.prepare(self.original,self.target,self.receipt)
        after = recovery.native_state(Path(value['runtime_root'])/recovery.NATIVE_DB)
        self.assertEqual(before,recovery.file_inventory(self.original))
        self.assertEqual(before,recovery.file_inventory(self.target/'evidence/original-byte-copy'))
        self.assertEqual(after['screen_contexts'],original_state['screen_contexts'])
        self.assertEqual(after['ocr_submissions'],original_state['ocr_submissions'])
        self.assertEqual(after['screen_batches'][0]['deadline'],original_state['screen_batches'][0]['deadline'])
        self.assertIsNone(after['screen_batches'][0]['paused_at'])
        self.assertEqual(after['screen_batches'][0]['status'],'paused')
        tasks = {row['id']:row for row in after['tasks']}
        self.assertEqual(tasks['primary-a']['status'],'interrupted')
        self.assertEqual(tasks['primary-a']['finished'],self.stopped.isoformat())
        self.assertEqual(tasks['primary-a']['started'],original_state['tasks'][1]['started'])
        self.assertEqual(tasks['primary-b']['status'],'paused')
        self.assertEqual(len(after['tasks']),4)
        for old,new in zip(original_state['screen_tasks'],after['screen_tasks']):
            self.assertEqual(new['spent_seconds'],7.25)
            old_meta,new_meta=json.loads(old['metadata']),json.loads(new['metadata'])
            for key in ('model_identity','cache_key','budget_reservation_seconds','attempt','source_to_prepared','prepared_to_source','source_polygon'):
                self.assertEqual(old_meta[key],new_meta[key])
        db = recovery.connect(Path(value['runtime_root'])/'agent/operations.sqlite3',readonly=True)
        self.assertEqual(db.execute('SELECT status FROM runs').fetchone()[0],'needs_verification')
        self.assertEqual(db.execute('SELECT state FROM operations').fetchone()[0],'pending')
        self.assertTrue(json.loads(db.execute('SELECT body FROM registered_inputs').fetchone()[0])['file'].startswith(value['runtime_root']))
        db.close()
        self.assertEqual(value['primary_tasks_created'],0)
        self.assertFalse(value['model_invoked'])
        self.assertFalse(value['execution_costs_reset'])

    def test_empty_directory_structure_is_preserved_and_bound(self):
        source = freeze(self.root)
        value = recovery.prepare(self.original,self.target,self.receipt,source_freeze=source)
        for directory in ('blobs','empty','empty/nested'):
            self.assertTrue((self.target/'evidence/original-byte-copy'/directory).is_dir())
            self.assertTrue((self.target/'runtime'/directory).is_dir())
        (self.target/'runtime/empty/nested').rmdir()
        with self.assertRaisesRegex(ValueError,'directory structure'):
            recovery.validate_launch(self.target/'prepared-recovery.json')

    def test_changed_frozen_directory_inventory_refuses_copy(self):
        expected = recovery.file_inventory(self.original)
        directories = recovery.directory_inventory(self.original)
        (self.original/'empty/nested').rmdir()
        with self.assertRaisesRegex(ValueError,'directory structure'):
            recovery.copy_bytes(self.original,self.target,expected,directories)
        self.assertFalse(self.target.exists())

    def followup_fixture(self):
        recovery.prepare(self.original,self.target,self.receipt)
        runtime=self.target/'runtime'
        (runtime/'blobs').rmdir()
        db=sqlite3.connect(runtime/recovery.NATIVE_DB)
        db.execute("UPDATE tasks SET status='queued' WHERE id='primary-b'")
        db.execute("UPDATE screen_batches SET status='running'")
        db.commit();db.close()
        state=recovery.native_state(runtime/recovery.NATIVE_DB)
        evidence=self.root/'followup-proof';evidence.mkdir()
        frozen_json(evidence/'files.json',recovery.file_inventory(runtime))
        frozen_json(evidence/'directories.json',recovery.directory_inventory(runtime))
        frozen_json(evidence/'processes.json',{'matching_python_or_llama_processes':[]})
        result={'status':'failed','error':{'type':'AssetUnavailable'},'agent_reconciled':False,
            'new_native_batches':0,'actual_primary_task_count':2,'native_batch_id':'batch1','scope_inputs':2}
        result.update({key:True for key in
            ('original_context_tasks_unchanged_no_recomputation','original_primary_task_ids_preserved',
             'frozen_source_unchanged','model_assets_unchanged','original_run_unchanged',
             'original_byte_copy_unchanged','owned_service_exited','owned_job_drained_and_closed')})
        frozen_json(evidence/'result.json',result)
        frozen_json(evidence/'cleanup.json',{'queue_alive':False,'workers':[]})
        (evidence/'stages.jsonl').write_text(json.dumps({'stage':'cleanup_finished','utc':self.stopped.isoformat()})+'\n','utf-8')
        closed=[row for row in state['tasks'] if row['status']=='interrupted']
        receipt={'format':recovery.FOLLOWUP_FORMAT,'original_run':str(runtime),
            'all_original_run_files_unchanged':True,'sqlite_originals_opened':False,
            'model_invoked':False,'original_recovery_invoked':False,
            'original_inventory_before':recovery.bound(evidence/'files.json'),
            'failure':{'terminal_handle':{'session_id':1,'exit_code':1,'observation_timeout':False,'active_restart':False},
                'terminal_handle_source':'CPU-only fixture','matching_owned_python_or_llama_processes':0,
                'process_snapshot':recovery.bound(evidence/'processes.json')},
            'timeline':{'file':recovery.bound(evidence/'stages.jsonl')},'inputs':{'count':2},
            'native':{'batch':state['screen_batches'][0],'context_success_count':2,
                'context_tasks':state['screen_contexts'],'primary_tasks':state['screen_tasks']},
            'lineage':{'original_preparation':recovery.bound(self.target/'prepared-recovery.json'),
                'execution_result':recovery.bound(evidence/'result.json'),
                'native_cleanup':recovery.bound(evidence/'cleanup.json'),
                'original_directory_inventory':recovery.bound(evidence/'directories.json'),
                'base_primary_task_ids':['primary-a','primary-b'],'added_primary_task_ids':[],
                'closed_execution_segments_sha256':recovery.fingerprint(closed)},
            'restore_empty_directories':[{'relative':'blobs','source':str(self.original/'blobs')}]}
        frozen_json(evidence/'receipt.json',receipt)
        return runtime,state,evidence/'receipt.json'

    def test_followup_preserves_closed_costs_and_restores_only_proven_empty_directory(self):
        runtime,before,receipt=self.followup_fixture()
        output=self.root/'followup'
        value=recovery.prepare(runtime,output,receipt)
        after=recovery.native_state(Path(value['runtime_root'])/recovery.NATIVE_DB)
        self.assertEqual(value['preserved_primary_tasks'],['primary-a','primary-b'])
        self.assertEqual(value['primary_tasks_created'],0)
        self.assertEqual(value['continuation_kind'],'failed-recovery-followup')
        self.assertTrue((output/'runtime/blobs').is_dir())
        self.assertFalse((runtime/'blobs').exists())
        self.assertFalse((output/'evidence/original-byte-copy/blobs').exists())
        self.assertEqual([row for row in before['tasks'] if row['status']=='interrupted'],
                         [row for row in after['tasks'] if row['status']=='interrupted'])
        self.assertEqual([row['spent_seconds'] for row in before['screen_tasks']],
                         [row['spent_seconds'] for row in after['screen_tasks']])
        self.assertEqual(before['screen_batches'][0]['deadline'],after['screen_batches'][0]['deadline'])

    def test_followup_rejects_changed_closed_segment_lineage(self):
        runtime,before,receipt=self.followup_fixture()
        value=read_json(receipt)
        value['lineage']['closed_execution_segments_sha256']='wrong'
        bad=self.root/'bad-closed-lineage.json';frozen_json(bad,value)
        with self.assertRaisesRegex(ValueError,'historical execution costs'):
            recovery.prepare(runtime,self.root/'followup',bad)

    def test_followup_cannot_create_a_missing_unproven_asset_directory(self):
        runtime,before,receipt=self.followup_fixture()
        value=read_json(receipt)
        value['restore_empty_directories']=[{'relative':'inputs/new-assets','source':str(self.original/'blobs')}]
        bad=self.root/'bad-directory-lineage.json';frozen_json(bad,value)
        with self.assertRaisesRegex(ValueError,'Only the proven omitted'):
            recovery.prepare(runtime,self.root/'followup',bad)

    def test_missing_source_prepare_is_blocked_and_complete_binding_is_required(self):
        recovery.prepare(self.original,self.target,self.receipt)
        prepared = self.target/'prepared-recovery.json'
        with self.assertRaisesRegex(ValueError,'frozen sources'):
            recovery.validate_launch(prepared)
        source = freeze(self.root)
        recovery.bind_source(prepared,source,self.target/'source-bound-001.json')
        value = recovery.validate_launch(prepared,self.target/'source-bound-001.json')
        self.assertTrue(value['launch_source_and_scope_verified'])
        self.assertTrue(value['explicit_original_batch_resume_required'])
        self.assertFalse(value['service_or_model_started_by_this_helper'])

    def test_original_run_mutation_is_refused_before_copy(self):
        (self.original/'inputs/a.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'failure-scene inventory'):
            recovery.prepare(self.original,self.target,self.receipt)
        self.assertFalse(self.target.exists())

    def test_existing_and_nested_and_non_f_output_are_refused(self):
        self.target.mkdir()
        for target in (self.target,self.original/'nested',Path('C:/controlled-recovery-refused')):
            with self.subTest(target=str(target)),self.assertRaises(ValueError):
                recovery.prepare(self.original,target,self.receipt)

    def test_incomplete_terminal_evidence_and_live_process_snapshot_are_refused(self):
        receipt = read_json(self.receipt)
        receipt['failure']['terminal_handle']['observation_timeout']=True
        bad = self.root/'bad-receipt.json'
        frozen_json(bad,receipt)
        with self.assertRaisesRegex(ValueError,'terminal evidence'):
            recovery.prepare(self.original,self.target,bad)
        receipt['failure']['terminal_handle']['observation_timeout']=False
        receipt['failure']['matching_owned_python_or_llama_processes']=1
        live = self.root/'live-receipt.json'
        frozen_json(live,receipt)
        with self.assertRaises(ValueError):
            recovery.prepare(self.original,self.target,live)

    def test_future_or_pre_start_cleanup_cannot_zero_execution_segment(self):
        receipt = read_json(self.receipt)
        timeline = self.root/'wrong-timeline.json'
        frozen_json(timeline,{'events':[{'stage':'cleanup','event':'finished','utc_timestamp':datetime.now(timezone.utc).isoformat()}]})
        receipt['timeline']['file']=recovery.bound(timeline)
        bad = self.root/'bad-receipt.json'
        frozen_json(bad,receipt)
        with self.assertRaisesRegex(ValueError,'timeline binding'):
            recovery.prepare(self.original,self.target,bad)

    def test_frozen_source_tampering_and_mutable_tree_are_refused(self):
        source = freeze(self.root)
        member = source.parent/'ocr/src/ocr_workbench/hunyuan.py'
        original = member.read_bytes()
        member.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'complete manifest'):
            recovery.prepare(self.original,self.target,self.receipt,source_freeze=source)
        member.write_bytes(original)
        (source.parent/'ocr/.git').write_text('gitdir: F:/fixture-only','utf-8')
        with self.assertRaisesRegex(ValueError,'mutable worktrees'):
            recovery.prepare(self.original,self.target,self.receipt,source_freeze=source)

    def test_cleanup_before_abandoned_task_start_is_refused(self):
        db = sqlite3.connect(self.original/recovery.NATIVE_DB)
        db.execute('UPDATE tasks SET started=? WHERE id=?',
            ((self.stopped+timedelta(seconds=1)).isoformat(),'primary-a'))
        db.commit();db.close()
        proof_root=self.root/'new-start-proof';proof_root.mkdir()
        receipt=stop_receipt(proof_root,self.original)
        with self.assertRaisesRegex(ValueError,'Cleanup bound precedes'):
            recovery.prepare(self.original,self.target,receipt)

    def test_already_closed_running_segment_is_not_rewritten(self):
        db = sqlite3.connect(self.original/recovery.NATIVE_DB)
        db.execute('UPDATE tasks SET finished=? WHERE id=?',(self.stopped.isoformat(),'primary-a'))
        db.commit();db.close()
        proof_root=self.root/'closed-segment-proof';proof_root.mkdir()
        receipt=stop_receipt(proof_root,self.original)
        with self.assertRaisesRegex(ValueError,'unique open execution segment'):
            recovery.prepare(self.original,self.target,receipt)

    def test_runtime_cost_or_pixel_tampering_blocks_launch(self):
        source = freeze(self.root)
        recovery.prepare(self.original,self.target,self.receipt,source_freeze=source)
        runtime = self.target/'runtime'
        db = sqlite3.connect(runtime/recovery.NATIVE_DB)
        db.execute('UPDATE screen_tasks SET spent_seconds=0')
        db.commit()
        db.close()
        with self.assertRaisesRegex(ValueError,'runtime changed'):
            recovery.validate_launch(self.target/'prepared-recovery.json')

    def test_expired_deadline_is_preserved_and_launch_refused(self):
        db = sqlite3.connect(self.original/recovery.NATIVE_DB)
        db.execute('UPDATE screen_batches SET deadline=?',(datetime.now(timezone.utc).timestamp()-1,))
        db.commit(); db.close()
        second = self.root/'failure-expired'
        second.mkdir()
        expired_receipt = stop_receipt(second,self.original)
        source = freeze(self.root)
        value = recovery.prepare(self.original,self.target,expired_receipt,source_freeze=source)
        self.assertLess(value['deadline'],datetime.now(timezone.utc).timestamp())
        with self.assertRaisesRegex(ValueError,'wall allowance expired'):
            recovery.validate_launch(self.target/'prepared-recovery.json')

    def test_opaque_old_scope_text_is_refused(self):
        with self.assertRaisesRegex(ValueError,'Opaque text'):
            recovery.rewrite_text('read '+str(self.original/'inputs/a.png'),self.original,self.target)
        with self.assertRaisesRegex(ValueError,'no actual copied target'):
            recovery.remap_value(str(self.original/'missing.png'),self.original,self.target)

    def test_wal_only_commits_are_included_without_opening_original_db(self):
        db = sqlite3.connect(self.original/recovery.NATIVE_DB)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE screen_items SET stage='wal-committed-stage'")
        db.commit()
        proof_root=self.root/'wal-proof';proof_root.mkdir()
        try:
            receipt=stop_receipt(proof_root,self.original)
            before=recovery.file_inventory(self.original)
            value=recovery.prepare(self.original,self.target,receipt)
            self.assertEqual(before,recovery.file_inventory(self.original))
            after=recovery.native_state(Path(value['runtime_root'])/recovery.NATIVE_DB)
            self.assertTrue(all(row['stage']=='wal-committed-stage' for row in after['screen_items']))
            self.assertTrue(any(name.endswith('-wal') for name in before))
        finally:
            db.close()


def main():
    global OUTPUT
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    OUTPUT=fpath(args.output)
    if OUTPUT.exists():raise ValueError('CPU test output must have a new identity')
    OUTPUT.mkdir(parents=True,exist_ok=False)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ControlledRecovery))
    frozen_json(OUTPUT/'result.json',{'format':'controlled-recovery-cpu-tests-v1','tests':result.testsRun,
        'errors':len(result.errors),'failures':len(result.failures),'success':result.wasSuccessful(),
        'source_sha256':recovery.file_sha(Path(recovery.__file__)),
        'test_sha256':recovery.file_sha(Path(__file__)),'fixture_only':True,'original_actual_h60_databases_opened':False,
        'service_or_model_started':False,'actual_recovery_proven':False})
    if not result.wasSuccessful():raise SystemExit(1)


if __name__=='__main__':main()
