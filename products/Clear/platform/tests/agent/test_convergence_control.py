"""Transactional cancellation and exact native pause intent, on isolated F data."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json, tempfile, unittest
from unittest.mock import Mock, patch

from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore, Conflict, RunSuperseded, canonical
from workbench_platform.agent.tasks import AuditTasks
from workbench_platform.agent.registry import ToolContext
from workbench_platform.application_services import job_lifecycle as lifecycle

ROOT = Path('F:/OCR/output/platform-convergence-dev-20261005/evidence/control')


class ControlTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.store = AgentStore(Path(self.temporary.name)/'agent')
        self.session = self.store.create_session('wtest','Control',[],'session')
        self.run, _ = self.store.start_run(self.session['id'],'Goal','run',1)
        self.services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _:object()))
        self.runtime = object.__new__(AgentRuntime)
        self.runtime.store, self.runtime.services = self.store, self.services
        self.runtime.graph = None
        self.tasks = AuditTasks(self.runtime)
        self.ctx = ToolContext('wtest',self.session['id'],self.run['id'],1,0,'',(),self.services,self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, revision=1):
        return {'request_id':'takeover','expected_revision':revision,'action':'take_over'}

    def test_repeated_cancel_fences_once_and_completed_run_is_not_stopped(self):
        first = self.runtime.cancel(self.run['id'])
        self.assertEqual(first['generation'],2)
        self.assertEqual(self.runtime.cancel(self.run['id']),first)
        self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE kind='cancelled'")),1)
        second, _ = self.store.start_run(self.session['id'],'Done','second',1)
        self.store.status(second['id'],1,'answered')
        self.runtime.cancel(second['id'])
        self.assertEqual(self.store.run(second['id'])['status'],'answered')
        self.assertEqual(self.store.run(second['id'])['generation'],1)
        self.assertFalse(self.store.rows("SELECT * FROM events WHERE run_id=? AND kind='cancelled'",(second['id'],)))

    def test_takeover_revision_conflict_and_post_fence_failure_roll_back_all_updates(self):
        self.tasks.get(self.session['id'])
        with self.store.transaction() as db:
            body, rev = self.tasks._body(db,self.session['id'])
            body['pending_scope']={'selected_assets':['new_asset'],'initial_refs':[]}
            db.execute('UPDATE audit_tasks SET body=? WHERE session_id=?',(canonical(body),self.session['id']))
        with self.assertRaises(Conflict):
            self.tasks.control(self.session['id'],self.command(9))
        with patch.object(self.tasks,'_receipt',side_effect=RuntimeError('receipt write failed')):
            with self.assertRaises(RuntimeError):
                self.tasks.control(self.session['id'],self.command())
        self.assertEqual(self.store.run(self.run['id'])['generation'],1)
        self.assertEqual(self.store.run(self.run['id'])['status'],'running')
        self.assertEqual(self.store.session(self.session['id'])['selected_assets'],[])
        task=self.tasks.get(self.session['id'])
        self.assertFalse(task['manual']); self.assertEqual(task['scope_revision'],1)
        self.assertIsNotNone(task['pending_scope'])
        first=self.tasks.control(self.session['id'],self.command())
        self.assertEqual(self.tasks.control(self.session['id'],self.command()),first)
        task=self.tasks.get(self.session['id'])
        self.assertTrue(task['manual']);self.assertEqual(task['scope_revision'],2)
        self.assertEqual(self.store.session(self.session['id'])['selected_assets'],['new_asset'])
        self.assertEqual(self.store.run(self.run['id'])['generation'],2)
        with self.assertRaises(RunSuperseded):
            self.store.status(self.run['id'],1,'succeeded')

    def reference(self, application='ocr', kind=None):
        return {'kind':'job','workspace_id':'wtest','object_id':'job','parent_id':'handoff',
                'application':application,'native_id':'document','coverage':{'kind':kind} if kind else {}}

    def test_ocr_recovers_only_verified_interruption_without_resuming_human_or_unknown_pause(self):
        tasks = [SimpleNamespace(native_task_id=name,native_status=state,phase=phase) for name,state,phase in
                 [('human','paused','已暂停'),('old','paused',''),('cold','paused','等待继续'),
                  ('interrupted','interrupted','等待继续')]]
        request=Mock()
        native=SimpleNamespace(context=SimpleNamespace(module_project_id='project'),
                               batch=lambda _:SimpleNamespace(tasks=tasks),_request=request)
        self.services.job_service=SimpleNamespace(_context=lambda *args:object(),_adapter=lambda _:native)
        with patch.object(lifecycle,'_native_read',return_value={'status':'paused','link_id':'link',
                                                               'native_job_ids':[t.native_task_id for t in tasks]}):
            for kind in (None,'fusion','visual_review'):
                ref=self.reference(kind=kind)
                self.assertIsNone(lifecycle.adapter(ref).settle(ref,self.ctx))
        self.assertEqual(request.call_count,3)
        self.assertEqual(request.call_args.args[2],{'task_ids':['cold','interrupted']})
        self.assertTrue(lifecycle.adapter(self.reference()).progress({'status':'paused'})['needs_resume'])

    def test_classification_pause_stays_paused_and_waiting_never_resubmits(self):
        action=Mock(side_effect=AssertionError('human pause must stay paused'))
        self.services.job_service=SimpleNamespace(action=action)
        ref=self.reference('classification')
        with patch('workbench_platform.application_services.operations.classification.read_job',
                   return_value={'status':'paused','capabilities':['resume']}):
            self.assertIsNone(lifecycle.adapter(ref).settle(ref,self.ctx))
        action.assert_not_called()

    def test_pdf_mixed_pause_intent_does_not_resume_all_owned_stages(self):
        from workbench_platform.application_services.operations import pdf
        ref=self.reference(kind='pdf_pages')
        page_request=Mock()
        for stages in ([{'status':'paused','phase':'已暂停','owned':True}],
                       [{'status':'paused','phase':'','owned':True}],
                       [{'status':'interrupted','phase':'等待继续','owned':True},
                        {'status':'paused','phase':'已暂停','owned':True}]):
            with patch.object(pdf,'status',return_value={'submitted':True,'stages':stages}),\
                 patch.object(pdf,'bound',return_value=(None,None,SimpleNamespace(page_request=page_request),None)):
                self.assertIsNone(lifecycle.adapter(ref).settle(ref,self.ctx))
        page_request.assert_not_called()
        with patch.object(pdf,'status',return_value={'submitted':True,'stages':[
                {'status':'interrupted','phase':'等待继续','owned':True}]}),\
             patch.object(pdf,'bound',return_value=(None,None,SimpleNamespace(page_request=page_request),None)):
            self.assertIsNone(lifecycle.adapter(ref).settle(ref,self.ctx))
        page_request.assert_called_once_with('document','job',action='resume')

    def test_cancel_during_ocr_read_fences_resume_and_unregistered_kind_is_rejected(self):
        def read(ref,ctx):
            self.runtime.cancel(self.run['id'])
            return {'status':'paused'}
        with patch.object(lifecycle,'_native_read',side_effect=read):
            with self.assertRaises(RunSuperseded):
                lifecycle.adapter(self.reference()).settle(self.reference(),self.ctx)
        with self.assertRaises(ValueError):
            lifecycle.adapter(self.reference(kind='unregistered'))
