"""Finite agent job matrix using real stores/runtime and synthetic worker outputs.

No GPU, PDF subprocess or network model runs. Region creation uses the actual
page-processing transaction, with table preparation and region detection stubbed.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from ocr_workbench.agent.contracts import ProviderCall
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.visual import targets_for_result
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.documents import Documents
from ocr_workbench.imaging import add_image
from ocr_workbench.multimodal_store import complete_review, prepare_review
from ocr_workbench.page_processing import complete_region_task, process_stage
from ocr_workbench.store import Store

KINDS = ('ocr', 'pdf_stage', 'pdf_child_ocr', 'fusion', 'visual_review')
RECEIPTS = []


def tearDownModule():
    path = os.environ.get('OCR_AGENT_JOB_MATRIX_RECEIPT')
    if path:
        Path(path).write_text(json.dumps({'scope': 'finite synthetic worker matrix; no real GPU, provider or complete fault-window qualification',
            'completed_cases': len(RECEIPTS), 'cases': RECEIPTS}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class Fixture:
    async def open(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'workspace')
        self.project = self.store.project('本轮项目')['id']
        self.other = self.store.project('不应受影响项目')['id']
        source = self.root / 'tiny.png'
        Image.new('RGB', (20, 12), 'white').save(source)
        self.photo = add_image(self.store, self.project, 'tiny.png', source)
        Image.new('RGB', (20, 12), 'white').save(source)
        self.other_photo = add_image(self.store, self.other, 'other.png', source)
        self.raw = {'text': '原采用 00123', 'tables': [], 'blocks': [], 'engine': 'glm'}
        source_task = self.store.enqueue(self.project, [self.photo['active_version']], ['glm'])[0]
        assert self.store.claim()['id'] == source_task
        assert self.store.complete(source_task, self.raw)
        self.adopted = self.store.one('tasks', source_task)['result_id']
        self.bundle = self.root / 'synthetic-bundle'
        self.bundle.mkdir()
        self.documents = Documents(self.store, self.bundle)
        queue = lambda: SimpleNamespace(wake=threading.Event(), status=lambda: {'healthy': True, 'task_id': None})
        self.services = ApplicationServices(self.store, self.documents, SimpleNamespace(guard=self.store.file_lock),
            queue=queue(), fusion_queue=queue(), external_queue=queue(), require_recognition=lambda: None,
            registry=SimpleNamespace(engines=lambda: {'glm': {'package_id': 'builtin'}, 'ppocr': {'package_id': 'builtin'}}), bundle=self.bundle)
        self.visual_config = {'model': {'id': 'synthetic-visual', 'sha256': 'synthetic-fixed'}}
        self.services.visual_parameters = lambda model: (self.visual_config, self.services.queue)
        async def no_model(state, run):
            raise AssertionError('This matrix must not call a controller')
        self.runtime = await AgentRuntime(self.services, policy={'limits': {}}, model_override=no_model).open()
        self.agent = self.runtime.agent
        self.session = self.agent.create_session(self.project, {'client_request_id': 'session', 'title': '合成矩阵'})
        self.sequence = 0
        self.new_run('initial')
        return self

    def new_run(self, key):
        self.run = self.agent.create_run(self.project, self.session['id'], {'client_request_id': key, 'content': '合成任务'},
            context={'selection': {'pages': [], 'permissions': {'allow_retry_failed': True}}}, config={}, limits={})
        self.runtime._lease(self.run)
        self.run = self.agent.run(self.project, self.run['id'])

    def grant(self, permission, scope):
        return self.runtime.policy.grant(self.project, permission, scope, source='bound_ui_scope', expires=time.time() + 300, run_id=self.run['id'])

    async def execute(self, tool, args, *, replay=None):
        if replay is None:
            self.sequence += 1
            call = ProviderCall(call_id='call-' + str(self.sequence), tool=tool, arguments=args)
            self.agent.record_calls(self.project, self.run['id'], self.run['generation'], self.sequence, [call.model_dump()])
            context = {'run': self.run, 'generation': self.run['generation'], 'step': self.sequence, 'call_id': call.call_id}
        else:
            call, context = replay
        output = await self.runtime.registry.execute(context['run'], context['generation'], call, context)
        return output, (call, context)

    async def submit(self, kind):
        version = self.photo['active_version']
        scope = {'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']]}
        if kind in {'ocr', 'fusion'}:
            engines = ['glm', 'ppocr'] if kind == 'fusion' else ['glm']
            args = {'version_ids': [version], 'engines': engines}
            if kind == 'fusion':
                args['fusion_config_id'] = 'table:conservative'
            self.grant('scoped_processing', {**scope, 'version_ids': [version], 'engines': engines})
            tool = 'run_ocr'
        elif kind in {'pdf_stage', 'pdf_child_ocr'}:
            mode = ('auto' if getattr(self, 'child_count', 1) > 1 else 'ocr') if kind == 'pdf_child_ocr' else 'native'
            args = {'document_id': self.photo['id'], 'page_numbers': [1], 'mode': mode, 'force': True}
            if mode != 'native':
                args['engine'] = 'glm'
            self.grant('scoped_processing', {**scope, 'mode': mode, 'force': True, **({'engine': 'glm'} if mode != 'native' else {})})
            tool = 'process_pages'
        else:
            with self.store.transaction() as db:
                target = targets_for_result(db, self.adopted)[0]['id']
            args = {'result_id': self.adopted, 'revision': 0, 'version_id': version,
                    'target_ids': [target], 'visual_model_id': 'synthetic-visual'}
            self.grant('scoped_visual_review', {**scope, 'version_ids': [version], 'result_ids': [self.adopted],
                'target_ids': [target], 'revision': 0, 'visual_model_id': 'synthetic-visual'})
            tool = 'request_visual_review'
        output, replay = await self.execute(tool, args)
        self.kind, self.output, self.replay = kind, output, replay
        wanted = 'pdf_stage' if kind == 'pdf_child_ocr' else kind
        self.link = next(j for j in output['job_refs'] if j['kind'] == wanted)
        self.job_id = self.link['job_id']
        if kind == 'pdf_child_ocr':
            stage = self.store.claim_document_stage()
            assert stage['id'] == self.job_id
            def prepare(manager, page, doc, version, native, mode, cancelled):
                native['table_tool'] = {}
            regions = [{'id': None, 'kind': 'ocr-needed', 'polygon': box_polygon([n * 5, 0, n * 5 + 4, 10])}
                       for n in range(getattr(self, 'child_count', 1))]
            with patch('ocr_workbench.table_tool.prepare', side_effect=prepare), patch.object(self.store, 'page_regions', return_value=regions):
                process_stage(self.documents, stage, lambda: False)
            self.child_id = self.store.rows('SELECT task_id FROM page_ocr_inputs WHERE stage_id=?', (self.job_id,))[0]['task_id']
        self.operation_id = output['data']['operation_id']
        return output

    def row(self):
        return self.store.one('tasks' if self.kind != 'pdf_stage' else 'document_stages',
                              self.child_id if self.kind == 'pdf_child_ocr' else self.job_id)

    def set_state(self, state):
        with self.store.transaction() as db:
            table = 'document_stages' if self.kind == 'pdf_stage' else 'tasks'
            key = self.child_id if self.kind == 'pdf_child_ocr' else self.job_id
            db.execute(f'UPDATE {table} SET status=? WHERE id=?', (state, key))
            if self.kind == 'pdf_child_ocr':
                db.execute('UPDATE document_stages SET status=? WHERE id=?', ('failed' if state in {'failed', 'cancelled'} else 'waiting_gpu', self.job_id))

    def start_job(self):
        if self.kind == 'pdf_stage':
            claimed = self.store.claim_document_stage()
        elif self.kind == 'fusion':
            while parent := self.store.claim():
                assert self.store.complete(parent['id'], {**self.raw, 'engine': parent['engine']})
            claimed = self.store.claim(fusion=True)
        else:
            claimed = self.store.claim()
        expected = self.child_id if self.kind == 'pdf_child_ocr' else self.job_id
        assert claimed['id'] == expected, (claimed, expected)
        self.worker_row = claimed
        if self.kind == 'visual_review':
            self.visual_snapshot = prepare_review(self.store, self.job_id)

    async def reuse_from_previous_run(self):
        self.set_state('failed')
        previous_run = self.run
        await self.runtime.cancel(self.project, self.run['id'], self.run['generation'], 'stop_agent')
        self.new_run('reuse')
        args = {'previous_run_id': previous_run['id'], 'operation_id': self.operation_id, 'action': 'retry',
                'jobs': [{'kind': self.link['kind'], 'job_id': self.job_id}]}
        self.grant('scoped_failed_subset', {'jobs': [self.link['kind'] + ':' + self.job_id], 'previous_run_id': previous_run['id'],
                    'operation_id': self.operation_id, 'action': 'retry'})
        output, self.replay = await self.execute('retry_failed_jobs', args)
        self.link = output['job_refs'][0]
        self.operation_id = output['data']['operation_id']
        assert self.link['ownership'] == 'reused'

    def late_complete(self):
        if self.kind == 'pdf_stage':
            return self.store.finish_document_stage(self.job_id, {'synthetic': True})
        if self.kind == 'pdf_child_ocr':
            return complete_region_task(self.store, self.store.one('tasks', self.child_id), {**self.raw, 'text': '晚到区域结果'})
        if self.kind == 'visual_review':
            snapshot = prepare_review(self.store, self.job_id, require_running=False)
            return complete_review(self.store, self.job_id, {'summary': '合成晚到响应', 'items': [
                {'target_id': t['id'], 'decision': 'keep', 'after': t['before'], 'reason': '保留'} for t in snapshot['targets']]})
        raw = {**self.raw, 'text': '晚到 OCR/fusion 结果'}
        if self.kind == 'fusion':
            raw['fusion'] = {'units': []}
        return self.store.complete(self.job_id, raw)

    def assert_original_selection(self):
        assert self.store.rows('SELECT result_id FROM selections WHERE image_id=?', (self.photo['id'],))[0]['result_id'] == self.adopted

    async def close(self):
        await self.runtime.close()
        self.temp.cleanup()


class JobMatrixTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def fixture(self):
        fixture = await Fixture().open()
        try:
            yield fixture
        finally:
            await fixture.close()

    async def test_cross_kind_cancel_ownership_and_late_worker_completion(self):
        for kind in KINDS:
            for ownership in ('created', 'reused'):
                for state in ('queued', 'running'):
                    for mode in ('stop_agent', 'cancel_owned_jobs'):
                        case = {'matrix': 'cancel', 'kind': kind, 'ownership': ownership, 'state': state, 'mode': mode}
                        with self.subTest(**case):
                            async with self.fixture() as f:
                                await f.submit(kind)
                                if ownership == 'reused':
                                    await f.reuse_from_previous_run()
                                self.assertEqual(f.link['ownership'], ownership)
                                if state == 'running':
                                    f.start_job()
                                self.assertEqual(f.row()['status'], state)
                                unrelated = f.store.enqueue(f.other, [f.other_photo['active_version']], ['glm'])[0]
                                old_run, replay = f.run, f.replay
                                before_operations = len(f.store.rows('SELECT * FROM agent_operations'))
                                await f.runtime.cancel(f.project, old_run['id'], old_run['generation'], mode, request_id='cancel')
                                cancelled = ownership == 'created' and mode == 'cancel_owned_jobs'
                                self.assertEqual(f.row()['status'], 'cancelled' if cancelled else state)
                                self.assertEqual(f.store.one('tasks', unrelated)['status'], 'queued')
                                # Old tool context cannot replay after generation changed.
                                with self.assertRaises(ValueError):
                                    await f.execute(replay[0].tool, replay[0].arguments, replay=replay)
                                with self.assertRaises(ValueError):
                                    f.runtime.schedule(old_run, resume=True)
                                self.assertEqual(len(f.store.rows('SELECT * FROM agent_operations')), before_operations)
                                accepted = f.late_complete()
                                self.assertEqual(accepted, state == 'running' and not cancelled)
                                self.assertEqual(f.agent.run(f.project, old_run['id'])['status'], 'cancelled')
                                self.assertEqual(f.agent.run(f.project, old_run['id'])['generation'], old_run['generation'] + 1)
                                f.assert_original_selection()
                                RECEIPTS.append({**case, 'late_completion_accepted': accepted, 'run_remained_cancelled': True,
                                                 'stale_tool_rejected': True, 'stale_resume_rejected': True, 'unrelated_project_preserved': True})

    async def test_retry_only_failed_cancelled_and_replay_does_not_requeue_running_work(self):
        for kind in KINDS:
            for state in ('failed', 'cancelled', 'succeeded', 'queued', 'running', 'paused', 'interrupted'):
                case = {'matrix': 'retry', 'kind': kind, 'original_state': state}
                with self.subTest(**case):
                    async with self.fixture() as f:
                        await f.submit(kind)
                        f.set_state(state)
                        args = {'previous_run_id': f.run['id'], 'operation_id': f.operation_id, 'action': 'retry',
                                'jobs': [{'kind': f.link['kind'], 'job_id': f.job_id}]}
                        # PDF children are retried through the parent stage.
                        if kind == 'pdf_child_ocr' and state not in {'failed', 'cancelled'}:
                            with f.store.transaction() as db:
                                db.execute('UPDATE document_stages SET status=? WHERE id=?', (state, f.job_id))
                        before = len(f.store.rows('SELECT * FROM agent_operations'))
                        if state in {'failed', 'cancelled'}:
                            output, replay = await f.execute('retry_failed_jobs', args)
                            self.assertEqual(output['job_refs'][0]['ownership'], 'created')
                            self.assertEqual(f.row()['status'], 'queued')
                            f.start_job()
                            repeated, _ = await f.execute('retry_failed_jobs', args, replay=replay)
                            self.assertEqual(repeated['data']['operation_id'], output['data']['operation_id'])
                            self.assertEqual(f.row()['status'], 'running')
                            self.assertEqual(len(f.store.rows('SELECT * FROM agent_operations')), before + 1)
                        else:
                            with self.assertRaises(ValueError):
                                await f.execute('retry_failed_jobs', args)
                            self.assertEqual(len(f.store.rows('SELECT * FROM agent_operations')), before)
                            self.assertEqual(f.row()['status'], state)
                        f.assert_original_selection()
                        RECEIPTS.append({**case, 'retry_accepted': state in {'failed', 'cancelled'}, 'selection_preserved': True})

    async def test_atomic_effect_linkage_and_submission_replay(self):
        for kind in ('ocr', 'pdf_stage', 'fusion', 'visual_review'):
            with self.subTest(kind=kind):
                async with self.fixture() as f:
                    tables = ('tasks', 'document_stages', 'agent_operations', 'agent_job_links', 'multimodal_requests', 'agent_evidence')
                    before = {table: f.store.rows('SELECT * FROM ' + table) for table in tables}
                    original = f.runtime.operations.submit
                    def fault(window):
                        if window == 'after_effect':
                            raise OSError('synthetic crash before operation/job linkage commit')
                    def submit(*a, **kw):
                        return original(*a, **kw, fault=fault)
                    with patch.object(f.runtime.operations, 'submit', side_effect=submit):
                        with self.assertRaises(OSError):
                            await f.submit(kind)
                    self.assertEqual({table: f.store.rows('SELECT * FROM ' + table) for table in tables}, before)
                    self.assertTrue(all(row['operation_id'] is None for row in f.store.rows('SELECT * FROM agent_calls')))
                    first = await f.submit(kind)
                    count = {table: len(f.store.rows('SELECT * FROM ' + table)) for table in tables}
                    repeated, _ = await f.execute(f.replay[0].tool, f.replay[0].arguments, replay=f.replay)
                    self.assertEqual(first['data']['operation_id'], repeated['data']['operation_id'])
                    self.assertEqual({table: len(f.store.rows('SELECT * FROM ' + table)) for table in tables}, count)
                    self.assertTrue(f.store.rows('SELECT * FROM agent_job_links'))
                    f.assert_original_selection()
                    RECEIPTS.append({'matrix': 'atomic_effect_and_replay', 'kind': kind, 'rolled_back_after_effect': True,
                                     'replay_created_no_second_job': True, 'selection_preserved': True})

    async def test_cancellation_while_handler_waits_to_enter_submission_fences_all_kinds(self):
        for kind in ('ocr', 'pdf_stage', 'fusion', 'visual_review'):
            with self.subTest(kind=kind):
                async with self.fixture() as f:
                    entered, release = threading.Event(), threading.Event()
                    original = f.runtime.operations.submit
                    def waiting(*a, **kw):
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError('Test did not release submission barrier')
                        return original(*a, **kw)
                    before = {table: f.store.rows('SELECT * FROM ' + table) for table in
                              ('tasks', 'document_stages', 'agent_operations', 'agent_job_links', 'multimodal_requests')}
                    with patch.object(f.runtime.operations, 'submit', side_effect=waiting):
                        pending = asyncio.create_task(f.submit(kind))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                            await f.runtime.cancel(f.project, f.run['id'], f.run['generation'], 'cancel_owned_jobs')
                        finally:
                            release.set()
                        with self.assertRaises(ValueError):
                            await pending
                    self.assertEqual({table: f.store.rows('SELECT * FROM ' + table) for table in before}, before)
                    self.assertEqual(f.agent.run(f.project, f.run['id'])['status'], 'cancelled')
                    RECEIPTS.append({'matrix': 'cancel_before_transaction', 'kind': kind,
                                     'handler_started_before_cancel': True, 'new_business_effects_after_cancel': 0})

    async def test_mixed_retry_subset_rejects_entire_batch_before_requeueing_failure(self):
        async with self.fixture() as f:
            output = await f.submit('fusion')
            ocr = [job for job in output['job_refs'] if job['kind'] == 'ocr']
            self.assertEqual(len(ocr), 2)
            with f.store.transaction() as db:
                db.execute("UPDATE tasks SET status='failed' WHERE id=?", (ocr[0]['job_id'],))
                db.execute("UPDATE tasks SET status='succeeded' WHERE id=?", (ocr[1]['job_id'],))
            args = {'previous_run_id': f.run['id'], 'operation_id': f.operation_id, 'action': 'retry',
                    'jobs': [{'kind': 'ocr', 'job_id': row['job_id']} for row in ocr]}
            before = f.store.rows('SELECT * FROM agent_operations')
            with self.assertRaises(ValueError):
                await f.execute('retry_failed_jobs', args)
            self.assertEqual(f.store.one('tasks', ocr[0]['job_id'])['status'], 'failed')
            self.assertEqual(f.store.one('tasks', ocr[1]['job_id'])['status'], 'succeeded')
            self.assertEqual(f.store.rows('SELECT * FROM agent_operations'), before)
            args['jobs'] = args['jobs'][:1]
            retried, _ = await f.execute('retry_failed_jobs', args)
            self.assertEqual([job['job_id'] for job in retried['job_refs']], [ocr[0]['job_id']])
            self.assertEqual(f.store.one('tasks', ocr[0]['job_id'])['status'], 'queued')
            self.assertEqual(f.store.one('tasks', ocr[1]['job_id'])['status'], 'succeeded')
            RECEIPTS.append({'matrix': 'mixed_retry_subset', 'kinds': ['ocr', 'fusion'],
                             'invalid_mixed_batch_changed_no_task': True, 'only_requested_failed_task_requeued': True})

    async def test_pdf_child_retry_preserves_succeeded_output_and_requeues_failed_cancelled_only(self):
        async with self.fixture() as f:
            f.child_count = 3
            await f.submit('pdf_child_ocr')
            children = f.store.rows('SELECT t.* FROM tasks t JOIN page_ocr_inputs i ON i.task_id=t.id WHERE i.stage_id=? ORDER BY t.id', (f.job_id,))
            self.assertEqual(len(children), 3)
            with f.store.transaction() as db:
                for row, status in zip(children, ('running', 'failed', 'cancelled')):
                    db.execute('UPDATE tasks SET status=? WHERE id=?', (status, row['id']))
                db.execute("UPDATE document_stages SET status='failed' WHERE id=?", (f.job_id,))
            self.assertTrue(complete_region_task(f.store, children[0], f.raw))
            succeeded = f.store.one('tasks', children[0]['id'])
            saved_output = f.store.rows('SELECT output FROM page_ocr_inputs WHERE task_id=?', (children[0]['id'],))[0]['output']
            args = {'previous_run_id': f.run['id'], 'operation_id': f.operation_id, 'action': 'retry',
                    'jobs': [{'kind': 'pdf_stage', 'job_id': f.job_id}]}
            await f.execute('retry_failed_jobs', args)
            self.assertEqual(f.store.one('tasks', children[0]['id']), succeeded)
            self.assertEqual(f.store.rows('SELECT output FROM page_ocr_inputs WHERE task_id=?', (children[0]['id'],))[0]['output'], saved_output)
            self.assertEqual([f.store.one('tasks', row['id'])['status'] for row in children[1:]], ['queued', 'queued'])
            self.assertEqual(f.store.one('document_stages', f.job_id)['status'], 'waiting_gpu')
            RECEIPTS.append({'matrix': 'pdf_child_failed_subset', 'children': 3, 'successful_children_unchanged': 1,
                             'failed_or_cancelled_children_requeued': 2, 'new_child_tasks': 0})

    async def test_force_stage_produces_new_result_but_keeps_manual_adoption_and_replay_identity(self):
        async with self.fixture() as f:
            def prepare(manager, page, doc, version, native, mode, cancelled):
                native['table_tool'] = {}
            first = await f.submit('pdf_stage')
            first_stage = f.job_id
            with patch('ocr_workbench.table_tool.prepare', side_effect=prepare):
                process_stage(f.documents, f.store.claim_document_stage(), lambda: False)
            record = f.store.one('document_stages', first_stage)
            self.assertEqual(record['status'], 'succeeded')
            result_id = json.loads(record['output'])['result_id']
            self.assertNotEqual(result_id, f.adopted)
            f.assert_original_selection()
            replayed, _ = await f.execute(f.replay[0].tool, f.replay[0].arguments, replay=f.replay)
            self.assertEqual(first['data']['operation_id'], replayed['data']['operation_id'])
            self.assertEqual(len(f.store.rows('SELECT * FROM document_stages')), 1)
            await f.runtime.cancel(f.project, f.run['id'], f.run['generation'], 'stop_agent')
            f.new_run('explicit-new-force')
            await f.submit('pdf_stage')
            self.assertNotEqual(f.job_id, first_stage)
            with patch('ocr_workbench.table_tool.prepare', side_effect=prepare):
                process_stage(f.documents, f.store.claim_document_stage(), lambda: False)
            f.assert_original_selection()
            self.assertEqual(len(f.store.rows('SELECT * FROM document_stages')), 2)
            RECEIPTS.append({'matrix': 'force_stage_adoption', 'same_operation_stage_count': 1,
                             'explicit_new_run_stage_count': 2, 'new_results_generated': True, 'original_adoption_preserved': True})
