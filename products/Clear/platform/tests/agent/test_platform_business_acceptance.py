"""The acceptance oracle rejects fluent answers without native evidence."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.platform_tool_acceptance import (Client, PROMPTS, artifact_identity, audio_minutes, delivery_files,
                                            evaluate, leaf_assets, native_evidence, native_file_identity, prompt, sha256)
from scripts.platform_business_oracle import extract_entries


class BusinessAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'scenario': 'audit-01', 'oracle': {'required_applications': ['ocr', 'data'],
            'minimum_artifacts': 1, 'required_text': ['INV-1002', '850.00'], 'source_coverage': 'all'}}
        self.intent = {'assets': [{'asset_id': 'aOne'}, {'asset_id': 'aTwo'}], 'content': prompt('audit-01')}
        self.delivery = {'status': 'success', 'data': {'verified': True, 'finalized': True},
            'artifact_refs': [{'kind': 'artifact', 'sources': [
                {'kind': 'asset', 'object_id': 'aOne'}, {'kind': 'asset', 'object_id': 'aTwo'}]}]}
        self.records = {'runs': [{'status': 'succeeded', 'connection_revision': 1}],
            'events': [{'kind': 'model_request', 'payload': {'engineering_provider': False}},
                       {'kind': 'message', 'payload': {'role': 'user', 'content': self.intent['content']}}],
            'model_requests': [{'state': 'received', 'response': '{}'}],
            'operations': [{'tool': 'delivery_publish', 'result': self.delivery}]}
        self.files = [{'name': 'differences.csv', 'bytes': 20, 'sha256': 'a'*64,
                       'hash_verified': True, 'text': 'INV-1002,850.00',
                       'entries': extract_entries('differences.csv', b'id,invoice_amount,ledger_amount,difference,contract_limit\nINV-1002,800,850,50,\nCT-1024,,,,2000\nTotal,2000,2050,50,\n')}]
        self.jobs = [{'application': app, 'status': 'succeeded', 'authoritative': True} for app in ('ocr', 'data')]

    def evaluate(self):
        return evaluate(self.manifest, self.intent, self.records, self.files, self.jobs)

    def test_complete_independent_native_and_file_evidence_passes(self):
        self.assertTrue(self.evaluate()['passed'])

    def test_engineering_provider_never_counts_as_live_model(self):
        self.records['events'][0]['payload']['engineering_provider'] = True
        self.assertFalse(self.evaluate()['passed'])

    def test_missing_provider_origin_is_not_assumed_real(self):
        del self.records['events'][0]['payload']['engineering_provider']
        self.assertFalse(self.evaluate()['passed'])

    def test_unrecorded_coaching_message_does_not_count_as_blind_business_acceptance(self):
        self.records['events'].append({'kind': 'message', 'payload': {'role': 'user', 'content': '请调用 data_import，再 delivery_publish。'}})
        self.assertFalse(self.evaluate()['passed'])

    def test_final_response_cannot_substitute_for_downloaded_evidence(self):
        self.delivery['summary'] = 'INV-1002 850.00 已完成'
        self.files[0]['text'] = ''
        self.assertFalse(self.evaluate()['passed'])

    def test_cached_projection_cannot_substitute_for_authoritative_native_state(self):
        self.jobs[0]['authoritative'] = False
        self.assertFalse(self.evaluate()['passed'])

    def test_cpu_diagnostic_is_not_counted_as_a_business_algorithm_execution(self):
        self.jobs[0]['purpose'] = 'preparation'
        self.assertFalse(self.evaluate()['passed'])

    def test_source_missing_and_out_of_scope_are_both_rejected(self):
        self.delivery['artifact_refs'][0]['sources'].pop()
        self.assertFalse(self.evaluate()['passed'])
        self.delivery['artifact_refs'][0]['sources'].append({'kind': 'asset', 'object_id': 'otherProjectAsset'})
        self.assertFalse(self.evaluate()['passed'])

    def test_hash_mismatch_and_empty_artifacts_are_rejected(self):
        self.files[0]['hash_verified'] = False
        self.assertFalse(self.evaluate()['passed'])
        self.files[0].update(hash_verified=True, bytes=0)
        self.assertFalse(self.evaluate()['passed'])

    def test_empty_diagnostic_log_is_verified_but_cannot_replace_business_files(self):
        log={'name':'worker.log','bytes':0,'sha256':sha256(b''),'hash_verified':True,'text':''}
        self.files.append(log)
        self.assertTrue(self.evaluate()['passed'])
        log['hash_verified']=False
        self.assertFalse(self.evaluate()['passed'])
        log['hash_verified']=True
        self.files=[log]
        self.assertFalse(self.evaluate()['passed'])

    def test_algorithm_failure_never_counts_as_completed_delivery(self):
        self.jobs[1]['status'] = 'failed'
        self.assertFalse(self.evaluate()['passed'])

    def test_waiting_user_is_not_reported_as_completed_acceptance(self):
        self.records['runs'][0]['status'] = 'waiting_user'
        self.assertFalse(self.evaluate()['passed'])

    def test_business_prompts_do_not_disclose_operation_names_or_expected_answers(self):
        for value in [*PROMPTS.values(), prompt('audit-03')]:
            for forbidden in ('tools_list', 'tools_describe', 'data_import', 'delivery_publish',
                              'ocr_submit', 'multimodal_submit', 'INV-1002', '850.00'):
                self.assertNotIn(forbidden, value)

    def test_deep_heatmap_business_requirement_does_not_coach_tool_selection(self):
        context='本次除基础统计线索外，还需要能定位疑似编辑区域的深度模型热图，请分别说明两者的检测范围。'
        value=prompt('catnet',context)
        self.assertIn(context,value)
        for forbidden in ('tools_list','tools_describe','catnet_analyze','multimodal_prepare',
                          'multimodal_export','forensics_inspect','CAT-Net','106','expected_model'):
            self.assertNotIn(forbidden,value)

    def test_face_requires_explicit_nonempty_authorized_reference_scope(self):
        self.manifest['scenario'] = 'face'
        self.manifest['oracle']['required_applications'] = ['multimodal']
        self.intent['authorized_reference_ids'] = ['rAllowed']
        self.jobs = [{'application': 'multimodal', 'kind': 'face', 'status': 'succeeded',
                      'authoritative': True, 'result_hash': 'a'*64, 'action': 'search', 'reference_ids': ['rAllowed']}]
        self.assertTrue(self.evaluate()['passed'])
        self.jobs[0]['reference_ids'] = ['rOther']
        self.assertFalse(self.evaluate()['passed'])
        self.jobs[0]['reference_ids'] = []
        self.assertFalse(self.evaluate()['passed'])

    def test_audio_timestamps_must_be_inside_native_recording(self):
        self.manifest['scenario'] = 'audio'
        self.manifest['oracle']['required_applications'] = ['multimodal']
        self.jobs = [{'application': 'multimodal', 'kind': 'audio', 'status': 'succeeded',
                      'authoritative': True, 'result_hash': 'a'*64, 'duration_ms': 5000,
                      'segments': [{'start_ms': 100, 'end_ms': 4000}]}]
        self.files.append({'name':'minutes.md','bytes':200,'sha256':'b'*64,'hash_verified':True,
                           'text':'录音纪要草稿\n要点：00:00.100–00:04.000讨论事项；没有明确待办，语义不完整待确认。'})
        self.assertTrue(self.evaluate()['passed'])
        self.jobs[0]['segments'][0]['end_ms'] = 5001
        self.assertFalse(self.evaluate()['passed'])

    def test_audio_transcript_and_subtitles_cannot_substitute_for_minutes(self):
        files=[{'name':'transcript.txt','text':'会议纪要：00:00.1 摘要及待办未复核'},
               {'name':'transcript.srt','text':'00:00:01,000 --> 00:00:02,000\n对话内容'},
               {'name':'result.json','text':'{"goal":"交付会议纪要、事项和待办，00:00.1未复核"}'}]
        self.assertEqual(audio_minutes(files),[])

    def test_catnet_native_success_must_deliver_its_actual_heatmap(self):
        self.manifest['scenario']='catnet'
        self.manifest['oracle']['required_applications']=['multimodal']
        self.jobs=[{'application':'multimodal','kind':'catnet','status':'succeeded','authoritative':True,
                    'result_hash':'a'*64,'output_files':{'heatmap.png':{'sha256':'c'*64,'bytes':500}}}]
        self.assertFalse(self.evaluate()['passed'])
        self.files.append({'name':'deep-heatmap.png','bytes':500,'sha256':'c'*64,'hash_verified':True,'text':''})
        self.assertTrue(self.evaluate()['passed'])
        self.files[-1]['sha256']='d'*64
        self.assertFalse(self.evaluate()['passed'])

    def test_existing_native_export_must_be_bound_to_final_result(self):
        operation={'tool':'multimodal_export','result':{'status':'success','data':{'snapshot_id':'msOne','files':[{'name':'result.json'}]}}}
        records={'operations':[operation]}
        self.assertEqual(delivery_files(records,{'data':{},'result_refs':[]}),[])
        delivery={'data':{},'result_refs':[{'application':'multimodal','kind':'result','object_id':'msOne'}]}
        self.assertEqual(len(delivery_files(records,delivery)),1)
        delivery['artifact_refs']=[{'application':'multimodal','coverage':{'snapshot_id':'msOne'}}]
        self.assertEqual(delivery_files(records,delivery),[])

    def test_production_url_is_rejected(self):
        with self.assertRaises(ValueError):
            Client('http://127.0.0.1:8879')
        with self.assertRaises(ValueError):
            Client('https://example.com')

    def test_classification_uses_frozen_model_and_preserves_saved_human_labels(self):
        self.manifest['scenario'] = 'classification'
        self.manifest['oracle'].update(required_applications=['classification'], expected_model_id='mFixed',
            expected_model_sha256='f'*64, expected_annotation_state={'nativeAsset': {'label_id': 'Invoice', 'group_id': 'g1'}})
        self.jobs = [{'application': 'classification', 'status': 'succeeded', 'authoritative': True,
                      'model_id': 'mFixed', 'model_sha256': 'f'*64,
                      'annotation_state': {'nativeAsset': {'label_id': 'Invoice', 'group_id': 'g1'}}}]
        self.assertTrue(self.evaluate()['passed'])
        self.jobs[0]['model_sha256'] = 'a'*64
        self.assertFalse(self.evaluate()['passed'])
        self.jobs[0]['model_sha256'] = 'f'*64
        self.jobs[0]['annotation_state']['nativeAsset']['label_id'] = 'Note'
        self.assertFalse(self.evaluate()['passed'])

    def test_nested_leaf_assets_retain_original_scope(self):
        self.assertEqual(leaf_assets({'sources': [{'kind': 'result', 'sources': [
            {'kind': 'asset', 'object_id': 'aOne'}]}]}), {'aOne'})


class ArtifactLedgerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        parent=Path('F:/OCR/output/clear-platform-tools-20261016/tests/acceptance-artifacts')
        parent.mkdir(parents=True,exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(dir=parent)
        self.root=Path(self.temporary.name)
        (self.root/'artifact-center').mkdir();(self.root/'agent').mkdir()
        with closing(sqlite3.connect(self.root/'artifact-center/artifacts.sqlite3')) as db,db:
            db.execute('CREATE TABLE published_artifact(artifact_id TEXT,workspace_id TEXT,blob_hash TEXT,size_bytes INTEGER,revision INTEGER,availability TEXT,source_refs TEXT)')
            db.execute('INSERT INTO published_artifact VALUES(?,?,?,?,?,?,?)',('artifact1','wTest','a'*64,12,1,'available',json.dumps([{'asset_id':'derived1'}])))
        with closing(sqlite3.connect(self.root/'agent/operations.sqlite3')) as db,db:
            db.execute('CREATE TABLE derived_inputs(workspace_id TEXT,asset_id TEXT,leaf_assets TEXT)')
            db.execute('INSERT INTO derived_inputs VALUES(?,?,?)',('wTest','derived1','["original1"]'))

    def tearDown(self):
        self.temporary.cleanup()

    def test_public_delivery_without_sha_uses_published_hash_and_original_sources(self):
        identity=artifact_identity(self.root,'wTest',{'artifact_id':'artifact1','revision':1})
        self.assertEqual(identity,{'sha256':'a'*64,'bytes':12,'source_assets':['original1']})

    def test_stale_revision_and_cross_workspace_do_not_supply_hash_evidence(self):
        with self.assertRaises(ValueError):
            artifact_identity(self.root,'wTest',{'artifact_id':'artifact1','revision':2})
        with self.assertRaises(ValueError):
            artifact_identity(self.root,'wOther',{'artifact_id':'artifact1','revision':1})

    def native_fixture(self):
        payload=b'{"status":"succeeded"}'
        folder=self.root/'manual-analysis/jobFixed/snapshot-exports/msFixed'
        folder.mkdir(parents=True)
        (folder/'result.json').write_bytes(payload)
        snapshot={'workspace_id':'wTest','job_id':'jobFixed','snapshot_id':'msFixed','generation':1,
                  'sources':[{'asset_id':'original1'}],'analysis':{'result':{'output_files':{}}}}
        snapshot_hash=sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
        snapshot['snapshot_hash']=snapshot_hash
        with closing(sqlite3.connect(self.root/'jobs.sqlite3')) as db,db:
            db.execute('CREATE TABLE manual_result_snapshot(workspace_id TEXT,job_id TEXT,snapshot_id TEXT,body TEXT)')
            db.execute('INSERT INTO manual_result_snapshot VALUES(?,?,?,?)',('wTest','jobFixed','msFixed',json.dumps(snapshot)))
        file={'name':'result.json','sha256':sha256(payload),'size_bytes':len(payload),
              'download_url':'/api/platform/v1/workspaces/wTest/multimodal/jobs/jobFixed/snapshots/msFixed/files/result.json'}
        refs=[{'workspace_id':'wTest','kind':'result','application':'multimodal','object_id':'msFixed',
               'parent_id':'jobFixed','sha256':snapshot_hash}]
        return file,refs,folder

    def test_native_only_delivery_uses_snapshot_hash_and_persistent_export_bytes(self):
        file,refs,_=self.native_fixture()
        identity=native_file_identity(self.root,'wTest',file,refs)
        self.assertEqual(identity['sha256'],file['sha256'])
        self.assertEqual(identity['source_assets'],['original1'])
        with self.assertRaises(ValueError):
            native_file_identity(self.root,'wTest',file,[])

    def test_native_tampering_wrong_workspace_and_export_identity_are_rejected(self):
        file,refs,folder=self.native_fixture()
        wrong=dict(file,download_url=file['download_url'].replace('/wTest/','/wOther/'))
        with self.assertRaises(ValueError):native_file_identity(self.root,'wTest',wrong,refs)
        (folder/'export.identity.json').write_text(json.dumps({'snapshot_hash':'b'*64,'files':{}}),encoding='utf-8')
        with self.assertRaises(ValueError):native_file_identity(self.root,'wTest',file,refs)
        (folder/'export.identity.json').unlink()
        (folder/'result.json').write_bytes(b'changed')
        with self.assertRaises(ValueError):native_file_identity(self.root,'wTest',file,refs)


class NativeLedgerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        parent=Path('F:/OCR/output/clear-platform-tools-20261016/tests/acceptance-native-ledgers')
        parent.mkdir(parents=True,exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(dir=parent)
        self.root=Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def multimodal_fixture(self):
        job={'workspace_id':'wTest','job_id':'jFixed','kind':'face','status':'succeeded','generation':1,
             'revision':2,'purpose':'analysis','result_hash':'a'*64,'options':{'action':'search'},'result':{}}
        snapshot={'workspace_id':'wTest','job_id':'jFixed','snapshot_id':'msFixed','generation':1,'revision':2,
                  'result_hash':'a'*64,'analysis':deepcopy(job),
                  'reference_scope':[{'reference_id':'rAllowed','model_hash':'mFixed'}]}
        snapshot['snapshot_hash']=sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
        current=dict(job,status='failed',generation=2)
        with closing(sqlite3.connect(self.root/'jobs.sqlite3')) as db,db:
            db.execute('CREATE TABLE manual_analysis(workspace_id TEXT,body TEXT)')
            db.execute('CREATE TABLE manual_result_snapshot(workspace_id TEXT,job_id TEXT,snapshot_id TEXT,body TEXT)')
            db.execute('INSERT INTO manual_analysis VALUES(?,?)',('wTest',json.dumps(current)))
            db.execute('INSERT INTO manual_result_snapshot VALUES(?,?,?,?)',('wTest','jFixed','msFixed',json.dumps(snapshot)))
        ref={'application':'multimodal','kind':'result','workspace_id':'wTest','object_id':'msFixed',
             'parent_id':'jFixed','sha256':snapshot['snapshot_hash']}
        records={'operations':[{'tool':'face_analyze','result':{'job_refs':[{'application':'multimodal','object_id':'jFixed'}]}},
                               {'tool':'delivery_publish','result':{'data':{'finalized':True},'result_refs':[ref]}}]}
        return records,snapshot

    def test_delivered_snapshot_survives_later_retry_but_preserves_current_native_state(self):
        records,_=self.multimodal_fixture()
        jobs=native_evidence(self.root,'wTest',records)
        self.assertEqual(len(jobs),1)
        self.assertTrue(jobs[0]['authoritative'])
        self.assertEqual((jobs[0]['status'],jobs[0]['current_status'],jobs[0]['generation']),('succeeded','failed',1))
        self.assertEqual(jobs[0]['reference_ids'],['rAllowed'])
        self.assertEqual(jobs[0]['reference_models'],['mFixed'])
        self.assertEqual(native_evidence(self.root,'wTest',{'operations':records['operations'][1:]}),[])

    def test_corrupt_or_foreign_delivery_snapshot_is_not_authoritative(self):
        records,_=self.multimodal_fixture()
        ref=records['operations'][-1]['result']['result_refs'][0]
        ref['workspace_id']='wOther'
        self.assertFalse(native_evidence(self.root,'wTest',records)[0]['authoritative'])
        ref['workspace_id']='wTest'
        with closing(sqlite3.connect(self.root/'jobs.sqlite3')) as db,db:
            snapshot=json.loads(db.execute('SELECT body FROM manual_result_snapshot').fetchone()[0])
            snapshot['analysis']['result_hash']='b'*64
            db.execute('UPDATE manual_result_snapshot SET body=?',(json.dumps(snapshot),))
        self.assertFalse(native_evidence(self.root,'wTest',records)[0]['authoritative'])

    def forensic_fixture(self):
        root=self.root/'forensics'
        output=root/'iFixed'/'attempt-fixed'
        output.mkdir(parents=True)
        report={'input':{'sha256':'a'*64},'verdict':None,
                'evidence':[{'tool':'noise','status':'ok'},{'tool':'frequency','status':'ok'}]}
        raw=json.dumps(report).encode()
        (output/'report.json').write_bytes(raw)
        job={'status':'succeeded','report':report,'report_hash':sha256(raw),'blob_hash':'a'*64,
             'files':[{'name':'report.json','sha256':sha256(raw),'size_bytes':len(raw)}]}
        with closing(sqlite3.connect(root/'forensics.sqlite3')) as db,db:
            db.execute('CREATE TABLE inspections(workspace_id TEXT,inspection_id TEXT,body TEXT)')
            db.execute('CREATE TABLE output_locations(inspection_id TEXT,path TEXT)')
            db.execute('INSERT INTO inspections VALUES(?,?,?)',('wTest','iFixed',json.dumps(job)))
            db.execute('INSERT INTO output_locations VALUES(?,?)',('iFixed','iFixed/attempt-fixed'))
        return output,{'operations':[{'tool':'forensics_inspect','result':{'job_refs':[{'application':'forensics','object_id':'iFixed'}]}}]}

    def test_basic_forensic_success_requires_session_job_and_verified_original_report(self):
        output,records=self.forensic_fixture()
        jobs=native_evidence(self.root,'wTest',records)
        self.assertEqual((jobs[0]['application'],jobs[0]['kind']),('forensics','basic_forensics'))
        self.assertTrue(jobs[0]['authoritative'])
        self.assertEqual(len(jobs[0]['algorithms']),2)
        self.assertEqual(native_evidence(self.root,'wOther',records),[])
        self.assertEqual(native_evidence(self.root,'wTest',{'operations':[]}),[])
        (output/'report.json').write_bytes(b'changed')
        self.assertFalse(native_evidence(self.root,'wTest',records)[0]['authoritative'])

    def pdf_fixture(self):
        parameters={'page_numbers':[1],'mode':'auto','engine':'ppocr','force':False}
        source={'item_id':'iFixed','asset_id':'aFixed','blob_hash':'a'*64}
        with closing(sqlite3.connect(self.root/'ocr-handoffs.sqlite3')) as db,db:
            db.execute('CREATE TABLE ocr_handoff(handoff_id TEXT,workspace_id TEXT,request_id TEXT,manifest_json TEXT)')
            db.execute('CREATE TABLE ocr_page_submission(handoff_id TEXT,document_id TEXT,request_id TEXT,parameters_json TEXT)')
            db.execute('INSERT INTO ocr_handoff VALUES(?,?,?,?)',('hFixed','wTest','importFixed',json.dumps({'items':[source]})))
            db.execute('INSERT INTO ocr_page_submission VALUES(?,?,?,?)',('hFixed','dFixed','requestFixed',json.dumps(parameters)))
        folder=self.root/'native/ocr'
        folder.mkdir(parents=True)
        with closing(sqlite3.connect(folder/'workbench.sqlite3')) as db,db:
            db.execute('CREATE TABLE platform_import_items(project_id TEXT,request_id TEXT,item_id TEXT,asset_id TEXT,blob_hash TEXT,status TEXT)')
            db.execute('CREATE TABLE platform_import_documents(project_id TEXT,request_id TEXT,item_id TEXT,document_id TEXT)')
            db.execute('CREATE TABLE documents(id TEXT,project_id TEXT,sha256 TEXT)')
            db.execute('CREATE TABLE platform_page_requests(document_id TEXT,request_id TEXT,body TEXT,stage_ids TEXT)')
            db.execute('CREATE TABLE pages(id TEXT,document_id TEXT,page_number INTEGER)')
            db.execute('CREATE TABLE document_stages(id TEXT,page_id TEXT,status TEXT,output TEXT)')
            db.execute('CREATE TABLE tasks(id TEXT,project_id TEXT,status TEXT)')
            db.execute('CREATE TABLE results(id TEXT,task_id TEXT)')
            db.execute('INSERT INTO platform_import_items VALUES(?,?,?,?,?,?)',('pFixed','importFixed','iFixed','aFixed','a'*64,'imported'))
            db.execute('INSERT INTO platform_import_documents VALUES(?,?,?,?)',('pFixed','importFixed','iFixed','dFixed'))
            db.execute('INSERT INTO documents VALUES(?,?,?)',('dFixed','pFixed','a'*64))
            db.execute('INSERT INTO platform_page_requests VALUES(?,?,?,?)',('dFixed','requestFixed',json.dumps(parameters),'["stageFixed"]'))
            db.execute('INSERT INTO pages VALUES(?,?,?)',('pageFixed','dFixed',1))
            db.execute('INSERT INTO document_stages VALUES(?,?,?,?)',('stageFixed','pageFixed','succeeded','{"result_id":"resultFixed"}'))
            db.execute('INSERT INTO tasks VALUES(?,?,?)',('taskFixed','pFixed','succeeded'))
            db.execute('INSERT INTO results VALUES(?,?)',('resultFixed','taskFixed'))
        return {'operations':[{'tool':'ocr_process_pages','result':{'job_refs':[{'application':'ocr','object_id':'requestFixed',
                'native_id':'dFixed','parent_id':'hFixed','coverage':{'kind':'pdf_pages'}}]}}]}

    def test_pdf_request_needs_exact_handoff_source_page_set_and_native_result(self):
        records=self.pdf_fixture()
        jobs=native_evidence(self.root,'wTest',records)
        self.assertEqual((jobs[0]['kind'],jobs[0]['status']),('pdf_pages','succeeded'))
        self.assertTrue(jobs[0]['authoritative'])
        self.assertFalse(native_evidence(self.root,'wOther',records)[0]['authoritative'])
        with closing(sqlite3.connect(self.root/'native/ocr/workbench.sqlite3')) as db,db:
            db.execute("UPDATE pages SET page_number=2")
        self.assertFalse(native_evidence(self.root,'wTest',records)[0]['authoritative'])

    def test_pdf_stage_claim_without_successful_native_result_is_rejected(self):
        records=self.pdf_fixture()
        with closing(sqlite3.connect(self.root/'native/ocr/workbench.sqlite3')) as db,db:
            db.execute('DELETE FROM results')
        self.assertFalse(native_evidence(self.root,'wTest',records)[0]['authoritative'])


if __name__ == '__main__':
    unittest.main()
