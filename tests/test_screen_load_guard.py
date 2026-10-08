"""Persistent cold-load streaks through the real queue, with a CPU fake adapter."""
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from ocr_workbench.adapter import EngineLoadTimeout
from ocr_workbench.imaging import add_image
from ocr_workbench.screen_load_guard import cold_load_state
from ocr_workbench.store import Store,encoded
from ocr_workbench.task_queue import TaskQueue


class ColdLoadGuardTests(unittest.TestCase):
    def setUp(self):
        self.module=runpy.run_path(str(Path(__file__).with_name('test_screen_lineage_budget.py')))
        self.fixture=self.module['LineageBudgetTests']();self.fixture.setUp()
        self.store=self.fixture.store;self.manager=self.fixture.manager;self.queue=self.fixture.queue
        self.adapter_type=self.module['Adapter'];self.loads=[]
        self.versions=[self.fixture.image() for _ in range(4)]

    def tearDown(self):self.fixture.tearDown()

    def submit(self,**kwargs):
        # Four independent 500-second reservations must fit the root budget;
        # otherwise the shared budget correctly defers later queue inputs.
        batch=self.fixture.batch(self.versions,primary_engine='paddlevl',budget_seconds=500,
            batch_budget_seconds=10000,**kwargs)
        self.manager.step();return batch

    def failing_load(self,adapter):
        self.loads.append(adapter.engine)
        raise EngineLoadTimeout(adapter.engine,240,self.fixture.root/'controlled-ipc',{'stage':'initializing-model'})

    def tasks(self,batch):
        return self.store.rows('SELECT t.*,s.metadata FROM tasks t JOIN screen_tasks s ON s.task_id=t.id '
            'WHERE s.batch_id=? ORDER BY t.created,t.id',(batch,))

    def single_root(self):
        root=self.fixture.batch(self.versions[:1],primary_engine='paddlevl',budget_seconds=500,
            batch_budget_seconds=10000)
        self.manager.step();return root

    def child(self,parent,request):
        policy=self.manager.get(self.fixture.project,parent)['policy']
        child=self.manager.submit(self.fixture.project,{'version_ids':self.versions[:1],
            'request_id':request,'policy':policy},parent_batch_id=parent)['batch_id']
        self.manager.step();return child

    def test_fourth_native_input_stops_after_three_real_cold_timeout_outcomes(self):
        batch=self.submit()
        with patch.object(self.adapter_type,'load',lambda adapter:self.failing_load(adapter)):
            for _ in range(4):self.assertTrue(self.queue.step())
        self.assertEqual(self.loads,['paddlevl']*3)
        tasks=self.tasks(batch)
        self.assertTrue(all(task['status']=='failed' for task in tasks))
        blocked=next(task for task in tasks if task['phase']=='引擎暂不可用')
        self.assertIn('连续 3 次',blocked['error'])
        self.assertEqual(cold_load_state(self.store,blocked['id'])['consecutive_timeouts'],3)
        events=[event for task in tasks for event in json.loads(task['metadata']).get('engine_load_events',[])]
        self.assertEqual(len(events),3)
        self.assertTrue(all(event['outcome']=='timeout' and event['last_stage']['stage']=='initializing-model' for event in events))
        self.assertEqual(len(self.store.rows('SELECT * FROM images')),4)
        self.assertEqual(self.store.rows('SELECT * FROM results'),[])

    def test_actual_ready_return_resets_streak_before_next_cold_failure(self):
        batch=self.submit()
        outcomes=iter(['timeout','timeout','ready','timeout'])
        def load(adapter):
            outcome=next(outcomes);self.loads.append(outcome)
            if outcome=='timeout':raise EngineLoadTimeout(adapter.engine,240,self.fixture.root/'controlled-ipc')
        with patch.object(self.adapter_type,'load',load):
            for _ in range(3):self.queue.step()
            self.queue.unload();self.queue.step()
        self.assertEqual(self.loads,['timeout','timeout','ready','timeout'])
        self.assertEqual(cold_load_state(self.store,self.tasks(batch)[-1]['id'])['consecutive_timeouts'],1)
        self.assertEqual(sum(task['status']=='succeeded' for task in self.tasks(batch)),1)

    def test_recognition_timeouts_and_ordinary_load_failures_do_not_count_as_cold_timeout(self):
        batch=self.submit()
        with patch.object(self.adapter_type,'recognize',side_effect=TimeoutError('controlled recognition timeout')):
            for _ in range(4):self.queue.step()
        self.assertEqual(cold_load_state(self.store,self.tasks(batch)[-1]['id'])['consecutive_timeouts'],0)
        self.assertTrue(all(task['phase']=='识别失败' for task in self.tasks(batch)))
        self.manager.action(self.fixture.project,batch,{'action':'retry_failed','request_id':'retry-ordinary-load'})
        self.manager.step()
        self.queue.unload()
        with patch.object(self.adapter_type,'load',side_effect=RuntimeError('controlled ordinary initialization error')):
            for _ in range(4):self.queue.step()
        self.assertEqual(cold_load_state(self.store,self.tasks(batch)[-1]['id'])['consecutive_timeouts'],0)

    def test_pause_cold_reopen_and_new_retry_identity_do_not_clear_streak(self):
        batch=self.submit()
        with patch.object(self.adapter_type,'load',lambda adapter:self.failing_load(adapter)):
            for _ in range(3):self.queue.step()
            self.store.recover()
            reopened=TaskQueue(self.store,self.fixture.root,adapter_factory=self.queue.factory)
            reopened.action(self.fixture.project,'resume',[task['id'] for task in self.tasks(batch) if task['status']=='paused'])
            try:self.assertTrue(reopened.step())
            finally:reopened.unload()
            self.manager.step()
            self.manager.action(self.fixture.project,batch,{'action':'retry_failed','request_id':'retry-new-identity'})
            self.manager.step()
            for _ in range(4):self.queue.step()
        self.assertEqual(len(self.loads),3)
        self.assertTrue(all(task['status']=='failed' for task in self.tasks(batch)))
        self.assertEqual(cold_load_state(self.store,self.tasks(batch)[-1]['id'])['consecutive_timeouts'],3)

    def test_another_batch_and_changed_model_identity_remain_independent(self):
        batch=self.submit()
        with patch.object(self.adapter_type,'load',lambda adapter:self.failing_load(adapter)):
            for _ in range(4):self.queue.step()
            self.manager.step()
            self.manager.model_identity='controlled-new-weight-identity'
            selected=self.versions[0]
            self.manager.action(self.fixture.project,batch,{'action':'retry_failed','version_ids':[selected],'request_id':'retry-new-weights'})
            self.manager.step();self.queue.step()
        new=self.fixture.task(batch,selected,1)
        self.assertEqual(cold_load_state(self.store,new['id'])['consecutive_timeouts'],1)
        extra=self.fixture.image()
        another=self.fixture.batch([extra],primary_engine='paddlevl',budget_seconds=500)
        self.manager.step()
        with patch.object(self.adapter_type,'load',lambda adapter:self.failing_load(adapter)):self.queue.step()
        self.assertEqual(cold_load_state(self.store,self.fixture.task(another,extra)['id'])['consecutive_timeouts'],1)
        self.assertEqual(len(self.loads),5)

    def test_stopped_primary_inputs_use_existing_bounded_technical_fallback(self):
        batch=self.submit(fallback_engines=['glm'],max_upgrades=1,upgrade_trigger='failure_only')
        def load(adapter):
            if adapter.engine=='paddlevl':self.failing_load(adapter)
        with patch.object(self.adapter_type,'load',load):
            for _ in range(4):self.queue.step()
            self.manager.step()
            for _ in range(4):self.queue.step()
        self.manager.step()
        self.assertEqual(self.loads,['paddlevl']*3)
        items=self.manager._items(batch)
        self.assertTrue(all(item['status']=='needs_review' and item['result_id'] for item in items))
        self.assertTrue(all(item['review_state']!='accepted' for item in items))
        self.assertEqual(sum(task['engine']=='glm' for task in self.tasks(batch)),4)
        self.assertEqual(len(self.store.rows('SELECT * FROM images')),4)

    def test_parent_child_sibling_and_grandchild_share_root_streak(self):
        root=self.single_root()
        with patch.object(self.adapter_type,'load',lambda adapter:self.failing_load(adapter)):
            self.assertTrue(self.queue.step());self.manager.step()
            child=self.child(root,'first-child-cold-load')
            self.assertTrue(self.queue.step());self.manager.step()
            sibling=self.child(root,'second-child-cold-load')
            self.assertTrue(self.queue.step());self.manager.step()
            descendant=self.child(child,'grandchild-cold-load')
            self.assertTrue(self.queue.step());self.manager.step()
        self.assertEqual(self.loads,['paddlevl']*3)
        for batch in (root,child,sibling,descendant):
            state=cold_load_state(self.store,self.tasks(batch)[0]['id'])
            self.assertEqual(state['root_batch_id'],root)
            self.assertEqual(state['consecutive_timeouts'],3)
            self.assertTrue(state['blocked'])
            self.assertEqual(set(state['lineage_batch_ids']),{root,child,sibling,descendant})
        self.assertEqual(self.tasks(descendant)[0]['phase'],'引擎暂不可用')

    def test_ready_in_child_resets_root_and_sibling_streak(self):
        root=self.single_root()
        outcomes=iter(['timeout','ready','timeout'])
        def load(adapter):
            outcome=next(outcomes);self.loads.append(outcome)
            if outcome=='timeout':raise EngineLoadTimeout(adapter.engine,240,self.fixture.root/'controlled-ipc')
        with patch.object(self.adapter_type,'load',load):
            self.assertTrue(self.queue.step());self.manager.step()
            child=self.child(root,'ready-child-cold-load')
            self.assertTrue(self.queue.step());self.manager.step()
            self.queue.unload()
            # The completed child's exact pixels are cached by design. A
            # distinct source input requires a new real adapter load instead.
            sibling=self.manager.submit(self.fixture.project,{'version_ids':[self.versions[1]],
                'request_id':'timeout-sibling-new-source','policy':self.manager.get(self.fixture.project,root)['policy']},
                parent_batch_id=root)['batch_id']
            self.manager.step();self.assertTrue(self.queue.step());self.manager.step()
        self.assertEqual(self.loads,['timeout','ready','timeout'])
        for batch in (root,child,sibling):
            state=cold_load_state(self.store,self.tasks(batch)[0]['id'])
            self.assertEqual(state['consecutive_timeouts'],1)
            self.assertFalse(state['blocked'])

    def test_unrelated_root_does_not_reset_blocked_root_when_ready(self):
        root=self.submit()
        with patch.object(self.adapter_type,'load',lambda adapter:self.failing_load(adapter)):
            for _ in range(4):self.assertTrue(self.queue.step())
        self.manager.step()
        extra=self.fixture.image()
        another=self.fixture.batch([extra],primary_engine='paddlevl',budget_seconds=500)
        self.manager.step();self.assertTrue(self.queue.step());self.manager.step()
        self.assertEqual(cold_load_state(self.store,self.tasks(another)[0]['id'])['consecutive_timeouts'],0)
        self.assertTrue(cold_load_state(self.store,self.tasks(root)[0]['id'])['blocked'])
        self.assertEqual(len(self.loads),3)

    def test_ordinary_queue_without_screen_schema_is_unaffected(self):
        ordinary=Store(self.fixture.root/'ordinary-workspace')
        project=ordinary.project('普通OCR')['id']
        source=self.store.file(self.store.one('versions',self.versions[0])['path'])
        version=add_image(ordinary,project,'ordinary.png',source)['active_version']
        task_id=ordinary.enqueue(project,[version],['paddlevl'])[0]
        queue=TaskQueue(ordinary,self.fixture.root,adapter_factory=self.queue.factory)
        try:
            self.assertTrue(queue.step())
            self.assertEqual(ordinary.one('tasks',task_id)['status'],'succeeded')
            self.assertFalse(cold_load_state(ordinary,task_id)['blocked'])
            self.assertEqual(self.fixture.observed[-1]['timeout'],900)
        finally:queue.unload()


if __name__=='__main__':unittest.main()
