"""Version/record admission rejects unsafe readers before workspace writes."""
from pathlib import Path
from copy import deepcopy
from contextlib import closing
import json, sqlite3, tempfile, unittest
from scripts.product_compatibility import DATA_COMPATIBILITY, check, active_work
from workbench_platform.agent.store import AgentStore
from workbench_platform.audit.store import AuditStore
from workbench_platform.store.workspaces import WorkspaceStore, _create_v1

ROOT=Path('F:/OCR/output/platform-convergence-dev-20261005/evidence/product-entry')


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(dir=ROOT)
        self.root=Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def readonly_admission(self,root,reject=False,descriptor=DATA_COMPATIBILITY):
        before={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}
        if reject:
            with self.assertRaises(ValueError): check(root,descriptor)
        else:
            result=check(root,descriptor)
            self.assertTrue(result['compatible']);self.assertEqual(result['writes'],0)
        after={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}
        self.assertEqual(after,before)

    def test_fresh_preflight_does_not_create_workspace_and_active_read_is_exact(self):
        fresh=self.root/'uncreated'
        self.assertTrue(check(fresh,DATA_COMPATIBILITY)['compatible'])
        self.assertFalse(fresh.exists())
        store=AgentStore(self.root/'agent')
        session=store.create_session('wtest','Test',[],'s1')
        run,_=store.start_run(session['id'],'Goal','r1',1)
        self.assertEqual(active_work(self.root)['active_work'][0]['id'],run['id'])
        with store.transaction() as db:
            store.cancel_in_transaction(db,run_id=run['id'])
        self.assertEqual(active_work(self.root)['active_work'],[])
        self.assertTrue(check(self.root,DATA_COMPATIBILITY)['compatible'])

    def test_wrong_migration_or_incomplete_schema_is_rejected_without_changes(self):
        directory=self.root/'audit';directory.mkdir()
        path=directory/'audit.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE projects(project_id TEXT)')
            db.execute('PRAGMA user_version=99')
        before=path.read_bytes()
        with self.assertRaises(ValueError): check(self.root,DATA_COMPATIBILITY)
        self.assertEqual(path.read_bytes(),before)
        with closing(sqlite3.connect(path)) as db, db: db.execute('PRAGMA user_version=1')
        before=path.read_bytes()
        with self.assertRaises(ValueError): check(self.root,DATA_COMPATIBILITY)
        self.assertEqual(path.read_bytes(),before)

    def test_reader_capabilities_guard_is_independent_of_product_version_and_preserves_marker(self):
        marker=self.root/'product-reader-requirements.json'
        record={'format':'clear-product-reader-requirements-v1','reader_features':['formal-direct-evidence-v1']}
        marker.write_text(json.dumps(record),encoding='utf-8-sig')
        before=marker.read_bytes()
        old=deepcopy(DATA_COMPATIBILITY);old['reader_features']=[]
        with self.assertRaises(ValueError): check(self.root,old)
        self.assertTrue(check(self.root,DATA_COMPATIBILITY)['compatible'])
        self.assertEqual(marker.read_bytes(),before)
        with self.assertRaises(ValueError): check(Path('C:/unsafe'),DATA_COMPATIBILITY)

    def test_new_request_record_format_is_checked_without_rewriting_old_receipts(self):
        store=AgentStore(self.root/'agent')
        session=store.create_session('wtest','Test',[],'s1')
        store.event(session['id'],None,'legacy','model_request',{'input_state_version':'old_fixed_hash'})
        self.assertTrue(check(self.root,DATA_COMPATIBILITY)['compatible'])
        store.event(session['id'],None,'future','model_request',{'request_snapshot':{'format':'future-v9'}})
        before=store.rows('SELECT payload FROM events ORDER BY seq')
        with self.assertRaises(ValueError): check(self.root,DATA_COMPATIBILITY)
        self.assertEqual(store.rows('SELECT payload FROM events ORDER BY seq'),before)

    def test_multimodal_records_reject_old_reader_even_without_marker(self):
        path=self.root/'jobs.sqlite3'
        item={'schema_version':'1.0','job_id':'mTest','workspace_id':'wtest','request_id':'rTest',
              'kind':'audio','inputs':[],'options':{},'status':'needs_verification','stage':'restart',
              'revision':1,'generation':1,'created_at':'2026-10-13T00:00:00Z','updated_at':'2026-10-13T00:00:00Z',
              'result':None,'review':None,'error':None}
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE manual_analysis(job_id TEXT,workspace_id TEXT,request_id TEXT,payload_hash TEXT,body TEXT)')
            db.execute('INSERT INTO manual_analysis VALUES(?,?,?,?,?)',('mTest','wtest','rTest','a'*64,json.dumps(item)))
        old=deepcopy(DATA_COMPATIBILITY);old['reader_features'].remove('manual-multimodal-v1')
        before=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'多模态任务'):check(self.root,old)
        self.assertTrue(check(self.root,DATA_COMPATIBILITY)['compatible'])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(active_work(self.root)['active_work'],[])
        with closing(sqlite3.connect(path)) as db,db:
            item['status']='running'
            db.execute('UPDATE manual_analysis SET body=?',(json.dumps(item),))
        self.assertEqual(active_work(self.root)['active_work'][0]['id'],'mTest')
        with closing(sqlite3.connect(path)) as db,db:
            item['status']='future-state'
            db.execute('UPDATE manual_analysis SET body=?',(json.dumps(item),))
        self.readonly_admission(self.root,reject=True)

    def test_managed_audio_internal_state_and_large_upload_require_new_reader(self):
        item={'schema_version':'1.0','job_id':'mManaged','workspace_id':'wtest','request_id':'rManaged',
              'kind':'audio','inputs':[],'options':{'channel':None},'status':'needs_verification','stage':'restart',
              'revision':1,'generation':1,'created_at':'2026-10-14T00:00:00Z','updated_at':'2026-10-14T00:00:00Z',
              'result':None,'review':None,'error':None,'gpu_request_id':None,'worker_epoch':'epoch',
              'oom_retries':0,'active_seconds':0}
        path=self.root/'jobs.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE manual_analysis(job_id TEXT,workspace_id TEXT,request_id TEXT,payload_hash TEXT,body TEXT)')
            db.execute('INSERT INTO manual_analysis VALUES(?,?,?,?,?)',('mManaged','wtest','rManaged','a'*64,json.dumps(item)))
        self.assertTrue(check(self.root,DATA_COMPATIBILITY)['compatible'])
        old=deepcopy(DATA_COMPATIBILITY);old['reader_features'].remove('manual-long-audio-v1')
        with self.assertRaisesRegex(ValueError,'长录音'):check(self.root,old)
        with closing(sqlite3.connect(path)) as db,db:db.execute('DELETE FROM manual_analysis')
        with closing(sqlite3.connect(self.root/'assets.sqlite3')) as db,db:
            db.execute('CREATE TABLE asset_audio_upload(body TEXT)')
            db.execute('INSERT INTO asset_audio_upload VALUES(?)',('{}',))
        with self.assertRaisesRegex(ValueError,'大音频'):check(self.root,old)
        self.assertTrue(check(self.root,DATA_COMPATIBILITY)['compatible'])

    def test_cpu_readiness_records_reopen_only_for_capable_readers_without_writes(self):
        path=self.root/'jobs.sqlite3'
        item={'schema_version':'1.0','job_id':'mCpuReady','workspace_id':'wtest','request_id':'rCpuReady',
              'kind':'face','inputs':[],'options':{'device':'cpu'},'status':'succeeded','stage':'completed',
              'revision':1,'generation':1,'created_at':'2026-10-24T00:00:00Z','updated_at':'2026-10-24T00:00:00Z',
              'result':{'images':[]},'actual_device':'cpu','purpose':'analysis','review':None,'error':None}
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE manual_analysis(job_id TEXT,workspace_id TEXT,request_id TEXT,payload_hash TEXT,body TEXT)')
            db.execute('INSERT INTO manual_analysis VALUES(?,?,?,?,?)',('mCpuReady','wtest','rCpuReady','a'*64,json.dumps(item)))
        old=deepcopy(DATA_COMPATIBILITY);old['reader_features'].remove('agent-usability-v1')
        for private in ({'prepare_on_success':True}, {'prepare_on_success':False},
                        {'preparation_pending':True}, {'preparation_record_error':'Readiness record could not be saved'},
                        {'prepare_on_success':True,'preparation_pending':True,'preparation_record_error':'storage unavailable'}):
            with self.subTest(private=private):
                with closing(sqlite3.connect(path)) as db,db:
                    db.execute('UPDATE manual_analysis SET body=?',(json.dumps({**item,**private}),))
                self.readonly_admission(self.root)
                self.readonly_admission(self.root,reject=True,descriptor=old)
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('UPDATE manual_analysis SET body=?',(json.dumps({**item,'preparation_future_state':'ready'}),))
        self.readonly_admission(self.root,reject=True)
        self.readonly_admission(self.root,reject=True,descriptor=old)

    def test_formal_findings_origin_and_direct_refs_validate_without_migration(self):
        store=AuditStore(self.root/'audit')
        stamp='2026-10-05T00:00:00Z'
        finding={'procedure_id':'procedure1','content_revision':1,'finding':{'title':'旧正式发现'},
                 'evidence_refs':[],'records':{},'created_at':stamp,'updated_at':stamp}
        project={'project_id':'project1','workspace_id':'wtest','revision':1,'project_context_revision':1,
                 'findings':{'finding1':finding}}
        def save():
            with store.transaction() as db:
                db.execute('INSERT OR REPLACE INTO projects VALUES(?,?,?,?)',
                           ('project1','wtest',1,json.dumps(project)))
        def readonly_check(descriptor=DATA_COMPATIBILITY,reject=False):
            before=store.path.read_bytes()
            if reject:
                with self.assertRaises(ValueError): check(self.root,descriptor)
            else:
                result=check(self.root,descriptor)
                self.assertTrue(result['compatible']);self.assertEqual(result['writes'],0)
            self.assertEqual(store.path.read_bytes(),before)
        save();readonly_check()
        old=deepcopy(DATA_COMPATIBILITY);old['reader_features']=[]
        readonly_check(old)
        finding['origin']={'source':{'task_id':'task1','draft_id':'draft1','revision':1,
            'snapshot_hash':'a'*64,'scope_revision':1,'project_context_revision':0},
            'content':{'facts':'保留原文'},'author':{'kind':'agent'},
            'transfer_actor':{'id':'local1','display_name':'操作人','source':'local'},
            'transfer_kind':'human','transferred_at':stamp}
        save();readonly_check();readonly_check(old,reject=True)
        finding['origin']['source']['revision']=0
        save();readonly_check(reject=True)
        finding['origin']['source']['revision']=1
        finding['evidence_refs']=[{'task_id':'task1','source_evidence_id':'source1','snapshot_hash':'b'*64,
            'evidence_id':'formal1','project_id':'project1','workspace_id':'wtest','scenario':'direct_tool',
            'captured_at':stamp,'sources':[],'source_assets':[],'source_count':0}]
        save();readonly_check()
        missing=deepcopy(DATA_COMPATIBILITY);missing['reader_features'].remove('formal-direct-evidence-v1')
        readonly_check(missing,reject=True)
        finding['evidence_refs'][0]['project_id']='otherArchive'
        save();readonly_check(reject=True)
        finding['evidence_refs'][0]['project_id']='project1'
        finding['evidence_refs'][0]['workspace_id']='otherWorkspace'
        save();readonly_check(reject=True)
        finding['evidence_refs'][0]['workspace_id']='wtest'
        finding['evidence_refs'][0]['snapshot_hash']='corrupt'
        save();readonly_check(reject=True)

    def test_workspace_startup_versions_and_shapes_are_checked_without_writes(self):
        for case in ('empty0','unknown0','future','empty1','empty2','legacy1','malformed1','extra1','current2','malformed2'):
            with self.subTest(case=case):
                root=self.root/case;root.mkdir()
                path=root/'workspaces.sqlite3'
                if case=='current2':
                    store=WorkspaceStore(path)
                    store.create_workspace('合成当前工作区',workspace_id='wtest')
                else:
                    with closing(sqlite3.connect(path)) as db,db:
                        db.execute('PRAGMA user_version='+str(99 if case=='future' else 1 if case.endswith('1') else 2 if case.endswith('2') else 0))
                        if case=='unknown0': db.execute('CREATE TABLE legacy(id TEXT)')
                        if case in {'legacy1','malformed1','extra1'}:
                            _create_v1(db)
                            if case=='malformed1': db.execute('ALTER TABLE module_project_link DROP COLUMN display_name')
                            if case=='extra1': db.execute('ALTER TABLE workspace ADD COLUMN unknown TEXT')
                        if case=='malformed2':
                            db.execute('CREATE TABLE workspace(workspace_id TEXT,name TEXT)')
                            db.execute('CREATE TABLE module_project_link(link_id TEXT)')
                self.readonly_admission(root,reject=case not in {'empty0','legacy1','current2'})

    def test_forensic_startup_versions_and_shapes_are_checked_without_writes(self):
        for case in ('empty0','unknown0','future','empty1','valid1','malformed1','missing1'):
            with self.subTest(case=case):
                root=self.root/case;directory=root/'forensics';directory.mkdir(parents=True)
                path=directory/'forensics.sqlite3'
                with closing(sqlite3.connect(path)) as db,db:
                    db.execute('PRAGMA user_version='+str(99 if case=='future' else 1 if case.endswith('1') else 0))
                    if case=='unknown0': db.execute('CREATE TABLE legacy(id TEXT)')
                    if case in {'valid1','malformed1','missing1'}:
                        db.execute('CREATE TABLE inspections(inspection_id TEXT,workspace_id TEXT,request_id TEXT,payload_hash TEXT,body TEXT)')
                        if case!='missing1':
                            db.execute('CREATE TABLE review_requests(workspace_id TEXT,request_id TEXT,inspection_id TEXT,payload_hash TEXT,receipt TEXT)')
                        if case=='malformed1': db.execute('ALTER TABLE inspections DROP COLUMN body')
                self.readonly_admission(root,reject=case not in {'empty0','empty1','valid1'})

    def test_pending_wal_future_version_is_read_without_database_changes(self):
        path=self.root/'workspaces.sqlite3'
        with closing(sqlite3.connect(path)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('CREATE TABLE workspace(id TEXT,name TEXT)')
            writer.execute('PRAGMA user_version=99');writer.commit()
            wal=path.with_name(path.name+'-wal')
            before=(path.read_bytes(),wal.read_bytes())
            names={item.name for item in self.root.iterdir()}
            with self.assertRaisesRegex(ValueError,'数据库迁移版本不兼容'):
                check(self.root,DATA_COMPATIBILITY)
            self.assertEqual((path.read_bytes(),wal.read_bytes()),before)
            self.assertEqual({item.name for item in self.root.iterdir()},names)
