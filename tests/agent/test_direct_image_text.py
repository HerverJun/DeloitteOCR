"""Durable direct actions: no model, fixed scope and unknown-write fencing."""
import asyncio
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from workbench_platform.agent.store import AgentStore, Conflict, RunSuperseded
from workbench_platform.application_services.context import ToolContext
from workbench_platform.application_services.image_text import ImageTextRequest, ReceiptUnknown, submit, _operation, _context
from scripts.product_compatibility import check, DATA_COMPATIBILITY


class DirectImageTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=r'F:\OCR\output\architecture-revision-20261017\temp')
        self.root = Path(self.tmp.name)
        self.store = AgentStore(self.root / 'agent')
        self.asset = SimpleNamespace(media_type='image/png', revision=1, blob_hash='a' * 64)
        self.session = self.store.create_session('workspace', '文字', ['image'], 'session')
        self.task = {'scope_revision': 1, 'manual': False}
        self.services = SimpleNamespace(asset_library=(SimpleNamespace(get=lambda *args: self.asset), None, None),
            workspace_store=SimpleNamespace(get_workspace=lambda identifier: SimpleNamespace(name='project')))
        self.scheduled = []
        self.runtime = SimpleNamespace(store=self.store, services=self.services,
            tasks=SimpleNamespace(get=lambda sid: self.task, record_goal=lambda *args: None),
            schedule=lambda run, state: self.scheduled.append(run))
        self.services.agent = self.runtime

    def tearDown(self):
        self.tmp.cleanup()

    async def make(self, request_id='request', **kwargs):
        return await submit(self.runtime, self.session['id'], ImageTextRequest(request_id=request_id, **kwargs))

    async def test_accepted_request_replay_ignores_later_scope_but_rejects_changed_parameters(self):
        run = await self.make(expected_scope_revision=1)
        self.task['scope_revision'] = 2
        replay = await self.make(expected_scope_revision=1)
        self.assertEqual(replay['id'], run['id'])
        self.assertEqual(len(self.scheduled), 1)
        with self.assertRaises(Conflict):
            await self.make(expected_scope_revision=1, engines=['other'])
        with self.assertRaises(Conflict):
            await self.make('new', expected_scope_revision=1)
        self.assertEqual(run['driver'], 'image-text-v1')
        self.assertEqual(self.store.rows('SELECT * FROM model_requests'), [])

    async def test_later_asset_revision_and_scope_cannot_cross_write_boundary(self):
        run = await self.make()
        state = json.loads(run['driver_state'])
        _context(self.runtime, run, state)
        self.asset.revision = 2
        with self.assertRaises(Conflict):
            _context(self.runtime, run, state)
        self.asset.revision = 1
        self.task['scope_revision'] = 2
        with self.assertRaises(Conflict):
            _context(self.runtime, run, state)

    async def test_scope_queued_during_prepare_fences_the_next_native_write(self):
        run = await self.make()
        ctx = _context(self.runtime, run, json.loads(run['driver_state']))
        ctx.current()
        self.task['pending_scope'] = {'selected_assets': []}
        with self.assertRaises(Conflict):
            ctx.current()
        self.assertEqual(self.store.rows('SELECT * FROM operations'), [])

    async def test_native_accept_then_lost_reply_recovers_original_receipt_once(self):
        run = await self.make()
        ctx = _context(self.runtime, run, json.loads(run['driver_state']))
        accepted = {}
        submissions = []
        def handler(args, current):
            submissions.append(current.operation_id)
            accepted[current.operation_id] = {'status': 'success', 'summary': 'accepted', 'data': {'receipt': 'native1'}}
            raise OSError('reply lost')
        def lookup(args, current):
            return accepted.get(current.operation_id)
        first, _ = await _operation(self.runtime, ctx, 'native', {'version': 1}, handler, lookup=lookup)
        # Re-open the durable store as a restarted process would.
        self.runtime.store = AgentStore(self.root / 'agent')
        ctx = ToolContext(ctx.workspace_id, ctx.session_id, ctx.run_id, ctx.generation, 0, '', ctx.selected_assets,
                          self.services, self.runtime.store)
        second, _ = await _operation(self.runtime, ctx, 'native', {'version': 1}, handler, lookup=lookup)
        self.assertEqual(first, second)
        self.assertEqual(len(submissions), 1)

    async def test_unknown_write_never_resubmits_on_restart(self):
        run = await self.make()
        ctx = _context(self.runtime, run, json.loads(run['driver_state']))
        submissions = []
        def handler(args, current):
            submissions.append(current.operation_id)
            raise OSError('unknown acceptance')
        for _ in range(2):
            with self.assertRaises(ReceiptUnknown):
                await _operation(self.runtime, ctx, 'native', {}, handler, lookup=lambda *args: None)
        self.assertEqual(len(submissions), 1)

    async def test_stop_after_native_accept_blocks_late_result(self):
        run = await self.make()
        ctx = _context(self.runtime, run, json.loads(run['driver_state']))
        def handler(args, current):
            with self.store.transaction() as db:
                self.store.cancel_in_transaction(db, run_id=run['id'])
            return {'status': 'success', 'summary': 'late'}
        with self.assertRaises(RunSuperseded):
            await _operation(self.runtime, ctx, 'native', {}, handler)
        self.assertIsNone(self.store.rows('SELECT result FROM operations')[0]['result'])

    async def accepted_native_run(self):
        run = await self.make(engines=['ppocr'])
        saved = json.loads(run['driver_state'])
        arguments = {'link_id': 'link', 'asset_set_id': 'fixed', 'asset_set_revision': 1, 'engines': ['ppocr']}
        reference = {'kind': 'job', 'application': 'ocr', 'workspace_id': 'workspace',
                     'object_id': 'original-job', 'parent_id': 'original-handoff', 'native_id': 'original-native', 'revision': 1}
        pending, ctx = await _operation(self.runtime, _context(self.runtime, run, saved), 'ocr_run', arguments,
            lambda *args: {'status': 'pending', 'summary': 'accepted', 'job_refs': [reference]})
        saved['receipts'] = {'prepared': {'link_id': 'link', 'engines': [{'id': 'ppocr'}]},
            'input_set': {'asset_set_id': 'fixed', 'revision': 1}, 'submitted': pending,
            'submitted_operation_id': ctx.operation_id, 'submit_arguments': arguments}
        saved['stage'] = 'waiting'
        self.store.save_driver_state(run['id'], run['generation'], saved)
        self.runtime.current = lambda state: self.store.assert_current(state['run_id'], state['generation'])
        from workbench_platform.agent.runtime import AgentRuntime
        self.runtime.settle = MethodType(AgentRuntime.settle, self.runtime)
        return run, reference

    async def test_missing_native_service_restores_original_accepted_receipt_before_settlement(self):
        from workbench_platform.application_services import image_text, job_lifecycle
        run, reference = await self.accepted_native_run()
        live, calls = False, []
        def progress(ref, ctx):
            return {'status': 'running' if live else 'unknown', 'observation': 'native' if live else 'unavailable'}
        def restore(ref, ctx):
            nonlocal live
            calls.append(('prepare', ref['native_id']))
            live = True
        def settle(ref, ctx):
            self.assertTrue(live, 'settlement cannot read a service that was never restored')
            calls.append(('settle', ref['native_id']))
            return {'status': 'success', 'summary': 'same native result', 'result_refs': [], 'coverage': {'succeeded': 1}}
        lifecycle = job_lifecycle.Lifecycle(None, settle, None, None, restore)
        async def publish(runtime, ctx, name, arguments, handler, **kwargs):
            self.assertEqual(name, 'image_text_publish', 'accepted work must never enter a submit handler')
            return {'status': 'success', 'summary': 'ready'}, ctx
        with patch.object(job_lifecycle, 'adapter', return_value=lifecycle), \
             patch('workbench_platform.application_services.job_observations.progress_for', side_effect=progress), \
             patch('workbench_platform.application_services.job_observations.publish_settled_progress'), \
             patch.object(image_text, '_operation', side_effect=publish), \
             patch('workbench_platform.application_services.operations.ocr.run', side_effect=AssertionError('resubmitted')):
            await asyncio.wait_for(image_text.drive(self.runtime, run['id']), timeout=8)
        self.assertEqual(self.store.run(run['id'])['status'], 'succeeded')
        self.assertEqual(calls, [('prepare', 'original-native'), ('prepare', 'original-native'), ('settle', 'original-native')])
        operations = self.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],))
        self.assertEqual(len(operations), 1)
        self.assertEqual(json.loads(operations[0]['result'])['job_refs'][0]['native_id'], reference['native_id'])

    async def test_pause_during_native_service_restore_fences_the_late_acknowledgement(self):
        from workbench_platform.application_services import image_text, job_lifecycle
        run, _ = await self.accepted_native_run()
        def restore(ref, ctx):
            with self.store.transaction() as db:
                db.execute("UPDATE runs SET status='paused',generation=generation+1 WHERE id=?", (run['id'],))
        lifecycle = job_lifecycle.Lifecycle(None, lambda *args: self.fail('late settlement'), None, None, restore)
        with patch.object(job_lifecycle, 'adapter', return_value=lifecycle), \
             patch('workbench_platform.application_services.job_observations.progress_for',
                   return_value={'status': 'unknown', 'observation': 'unavailable'}):
            with self.assertRaises(RunSuperseded):
                await asyncio.wait_for(image_text.drive(self.runtime, run['id']), timeout=8)
        operation = self.store.rows('SELECT state FROM operations WHERE run_id=?', (run['id'],))[0]
        self.assertEqual(operation['state'], 'pending')
        self.assertEqual(self.store.rows('SELECT * FROM events WHERE run_id=? AND kind="delivery"', (run['id'],)), [])

    async def test_native_restore_transport_failure_keeps_observation_without_ignoring_guards(self):
        from workbench_platform.application_services import job_lifecycle
        from workbench_platform.catalog.launch import LaunchStatus
        run, ref = await self.accepted_native_run()
        ctx = _context(self.runtime, run, json.loads(self.store.run(run['id'])['driver_state']))
        for error in (OSError('offline'), LaunchStatus(503, 'APP_UNAVAILABLE')):
            lifecycle = job_lifecycle.Lifecycle(None, None, None, None, lambda *args: (_ for _ in ()).throw(error))
            with patch.object(job_lifecycle, 'adapter', return_value=lifecycle):
                self.assertFalse(job_lifecycle.prepare_observation(ref, ctx))
        for error in (PermissionError('denied'), LaunchStatus(409, 'IDEMPOTENCY_CONFLICT')):
            lifecycle = job_lifecycle.Lifecycle(None, None, None, None, lambda *args: (_ for _ in ()).throw(error))
            with patch.object(job_lifecycle, 'adapter', return_value=lifecycle), self.assertRaises(type(error)):
                job_lifecycle.prepare_observation(ref, ctx)
        self.assertEqual(len(self.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],))), 1)

    async def test_old_reader_rejects_direct_record_even_if_marker_missing(self):
        await self.make()
        (self.root / 'product-reader-requirements.json').unlink()
        descriptor = {**DATA_COMPATIBILITY, 'reader_features': [name for name in DATA_COMPATIBILITY['reader_features']
                                                                if name != 'direct-image-text-v1']}
        with self.assertRaisesRegex(ValueError, '直接执行'):
            check(self.root, descriptor)
        self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])

    async def test_disabled_agent_starts_direct_runtime_without_harness_installation(self):
        from workbench_platform.agent.runtime import AgentRuntime
        with patch.dict('os.environ', {'WORKBENCH_AGENT_ENABLED': '0', 'WORKBENCH_AGENT_LAYER': 'F:\\missing'}), \
             patch('workbench_platform.agent.runtime.HarnessEngine', side_effect=AssertionError('Harness created')):
            runtime = AgentRuntime(self.root / 'disabled' / 'agent', self.services)
            self.services.agent = runtime
            try:
                await runtime.start()
                self.assertIsNone(runtime.graph)
                self.assertFalse(runtime.agent_enabled)
                session = runtime.store.create_session('workspace', 'local', ['image'], 'direct-local')
                with patch.object(runtime, 'schedule') as scheduled:
                    run = await runtime.submit(session['id'], '为我提取图片中的文字。', 'local1')
                    self.assertEqual(run['driver'], 'image-text-v1')
                    scheduled.assert_called_once()
                    # The HTTP message adapter repeats record_goal after
                    # submit. Its identity must bind the original user text.
                    runtime.tasks.record_goal(session['id'], '为我提取图片中的文字。', 'local1')
                    replay = await runtime.submit(session['id'], '为我提取图片中的文字。', 'local1')
                    self.assertEqual(replay['id'], run['id'])
            finally:
                await runtime.close()

    async def test_missing_optional_agent_layer_or_harness_keeps_direct_runtime_available(self):
        from workbench_platform.agent.runtime import AgentRuntime
        for missing in ('support', 'harness'):
            support = patch('workbench_platform.agent.runtime.load_support_dependencies',
                            side_effect=FileNotFoundError('missing optional layer')) if missing == 'support' else \
                      patch('workbench_platform.agent.runtime.load_support_dependencies')
            with patch.dict('os.environ', {'WORKBENCH_AGENT_ENABLED': '1'}), support, \
                 patch('workbench_platform.agent.runtime.HarnessEngine', side_effect=FileNotFoundError('missing Harness')):
                runtime = AgentRuntime(self.root / missing / 'agent', self.services)
                self.services.agent = runtime
                try:
                    await runtime.start()
                    self.assertFalse(runtime.agent_enabled)
                    self.assertIsNone(runtime.graph)
                    session = runtime.store.create_session('workspace', 'local', ['image'], 's-' + missing)
                    with patch.object(runtime, 'schedule'):
                        run = await submit(runtime, session['id'], ImageTextRequest(request_id='r-' + missing))
                    self.assertEqual(run['driver'], 'image-text-v1')
                    self.assertEqual(runtime.store.rows('SELECT * FROM model_requests'), [])
                finally:
                    await runtime.close()
