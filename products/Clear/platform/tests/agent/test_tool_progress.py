"""Public transitions and real native observations, without external inference."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_data_tools as base
import test_coverage_completion as native_tests
import test_graph as graph_tests
from packages.contracts.agent import (AgentModel, AgentRef, AgentToolResult,
    AgentPublicActivity, AgentToolActivity, AgentJobProgress)
from workbench_platform.agent.progress import progress_for
from workbench_platform.agent.registry import ToolContext, ToolRegistry, ToolSpec, tool_activity
from workbench_platform.agent.store import AgentStore, Conflict, RunSuperseded
from workbench_platform.agent.runtime import AgentRuntime


class Input(AgentModel):
    value: str


def validate_public_events(store, run_id):
    contracts = {'agent_activity': AgentPublicActivity,
                 'tool_activity': AgentToolActivity, 'job_progress': AgentJobProgress}
    for row in store.rows('SELECT kind,payload FROM events WHERE run_id=?', (run_id,)):
        if row['kind'] in contracts:
            contracts[row['kind']].model_validate(json.loads(row['payload']))


def validate_native_samples(reference, *values):
    for value in values:
        AgentJobProgress.model_validate({**value, 'job_ref': reference, 'sampled_at': 'engineering-sample'})


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = base.RUN/'evidence/tool-progress'
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.store = AgentStore(Path(self.temporary.name)/'agent')
        session = self.store.create_session('wtest', 'Transitions', [], 'session_progress')
        run, _ = self.store.start_run(session['id'], 'execute', 'run_progress', 1)
        services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
        self.ctx = ToolContext('wtest', session['id'], run['id'], 1, 0, '', (), services, self.store)
        self.registry = ToolRegistry()
        self.call = {'call_id': 'progress_call', 'tool': 'test_action', 'arguments': {'value': 'private-input'}}

    def tearDown(self):
        self.temporary.cleanup()

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60

    def events(self):
        validate_public_events(self.store, self.ctx.run_id)
        return [json.loads(row['payload']) for row in self.store.rows(
            "SELECT payload FROM events WHERE run_id=? AND kind='tool_activity' ORDER BY seq", (self.ctx.run_id,))]

    def register(self, handler, **options):
        self.registry.register(ToolSpec('test_action', 'Transition test', Input, handler,
            effect='derive', **options))

    def stop(self, *, cancelled=True):
        with self.store.transaction() as db:
            db.execute('UPDATE runs SET generation=generation+1,status=? WHERE id=?',
                       ('cancelled' if cancelled else 'running', self.ctx.run_id))

    async def test_started_is_visible_before_handler_and_completed_replay_executes_once(self):
        executed = []
        def handler(args, context):
            executed.append(context.operation_id)
            events = self.events()
            self.assertEqual([item['phase'] for item in events], ['started'])
            self.assertEqual(events[0]['operation_id'], context.operation_id)
            self.assertNotIn('private-input', json.dumps(events))
            return AgentToolResult(status='success', summary='Result ready').model_dump()
        self.register(handler, idempotent=True)
        result = await self.registry.dispatch(self.call, self.ctx)
        for _ in range(3):
            self.assertEqual(result, await self.registry.dispatch(self.call, self.ctx))
        events = self.events()
        self.assertEqual([item['phase'] for item in events], ['started', 'finished', 'restored'])
        self.assertEqual(len(executed), 1)
        self.assertEqual({item['operation_id'] for item in events}, set(executed))

    async def test_schema_skill_and_scope_denials_never_start_handler(self):
        executed = []
        self.register(lambda args, ctx: executed.append(args.value), idempotent=True)
        invalid = {**self.call, 'call_id': 'invalid', 'arguments': {'value': 'private-input', 'workspace_id': 'wother'}}
        with self.assertRaises(ValueError):
            await self.registry.dispatch(invalid, self.ctx)
        self.registry.tool_guard = lambda call, context: False
        denied = await self.registry.dispatch({**self.call, 'call_id': 'skill_denied'}, self.ctx)
        self.assertEqual(denied['error']['code'], 'skill_tool_denied')
        self.registry.tool_guard = None
        denied = await self.registry.dispatch({**self.call, 'call_id': 'scope_denied'}, replace(self.ctx, grants=frozenset({'read'})))
        self.assertEqual(denied['error']['code'], 'scope_denied')
        self.assertEqual(executed, [])
        self.assertEqual([item['phase'] for item in self.events()], ['blocked']*3)
        self.assertTrue(all(item['operation_id'] is None for item in self.events()))
        self.assertEqual(self.store.rows('SELECT * FROM operations WHERE run_id=?', (self.ctx.run_id,)), [])

    async def test_pending_replay_restores_original_identity_without_submission(self):
        executed = []
        def handler(args, context):
            executed.append(context.operation_id)
            return AgentToolResult(status='pending', summary='Native task queued', job_refs=[
                AgentRef(kind='job', workspace_id='wtest', object_id='jobPending', revision=1,
                         application='ocr', native_id='nativePending')]).model_dump()
        self.register(handler, idempotent=True)
        pending = await self.registry.dispatch(self.call, self.ctx)
        self.assertEqual(pending, await self.registry.dispatch(self.call, self.ctx))
        self.assertEqual([item['phase'] for item in self.events()], ['started', 'pending', 'restored'])
        self.assertEqual(len(executed), 1)
        self.assertEqual(self.events()[-1]['job_refs'], pending['job_refs'])
        with self.assertRaises(Conflict):
            await self.registry.dispatch({**self.call, 'arguments': {'value': 'different'}}, self.ctx)
        self.assertEqual(self.events()[-1]['phase'], 'blocked')
        self.assertEqual(len(executed), 1)

    async def test_native_lookup_restores_intent_and_response_lost_after_commit(self):
        receipt = AgentToolResult(status='success', summary='Native commit found').model_dump()
        calls = []
        def handler(args, context):
            calls.append(context.operation_id)
            raise OSError('reply lost after commit')
        self.register(handler, lookup_operation=lambda args, context: receipt)
        self.store.begin_operation(self.ctx.run_id, 1, self.call)
        self.assertEqual(await self.registry.dispatch(self.call, self.ctx), receipt)
        self.assertEqual(calls, [])
        self.assertEqual([item['phase'] for item in self.events()], ['restored'])
        second = {**self.call, 'call_id': 'response_lost'}
        self.assertEqual(await self.registry.dispatch(second, self.ctx), receipt)
        self.assertEqual(len(calls), 1)
        self.assertEqual([item['phase'] for item in self.events()], ['restored', 'started', 'restored'])

    async def test_stop_and_new_generation_fence_late_handler_and_public_callback(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(args, context):
            entered.set()
            await release.wait()
            return AgentToolResult(status='success', summary='Late reply').model_dump()
        self.register(handler, idempotent=True)
        pending = asyncio.create_task(self.registry.dispatch(self.call, self.ctx))
        await entered.wait()
        self.stop(cancelled=False)
        release.set()
        with self.assertRaises(RunSuperseded):
            await pending
        self.assertEqual([item['phase'] for item in self.events()], ['started'])
        self.assertEqual(self.store.rows('SELECT state FROM operations')[0]['state'], 'intent')
        with self.assertRaises(RunSuperseded):
            tool_activity(self.ctx, self.call, 'finished', {'status': 'success', 'summary': 'Late callback'})
        self.stop()
        with self.assertRaises(RunSuperseded):
            await self.registry.dispatch({**self.call, 'call_id': 'after_stop'}, self.ctx)
        self.assertEqual([item['phase'] for item in self.events()], ['started'])


class NativeProgressTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup helpers, not the full coverage suite or its model doubles.
    setUp = native_tests.CoverageTests.setUp
    async def asyncSetUp(self):
        await native_tests.CoverageTests.asyncSetUp(self)
        release = Path('F:/OCR/release/Clear/versions/0.10.0')
        self.native_ocr_source = release / 'ocr'
        sys.path.extend([str(self.native_ocr_source / 'src'),
            str(release / 'program/bundles/ocr/runtimes/control/Lib/site-packages')])
    tearDown = native_tests.CoverageTests.tearDown
    create = base.DataToolTests.create
    asset = base.DataToolTests.asset
    call = native_tests.CoverageTests.call
    serve = native_tests.CoverageTests.serve
    link = native_tests.CoverageTests.link
    jobs = native_tests.CoverageTests.jobs
    assert_success = native_tests.CoverageTests.assert_success

    async def test_real_data_worker_queued_and_completed_no_invented_percent(self):
        session = await self.create()
        asset = self.asset('suppliers-left.csv')
        gate = threading.Event()
        self.native.pool.submit(gate.wait)
        try:
            submitted = await self.call('data_import', {'data_session_id': session,
                'expected_version': 1, 'side': 'left', 'asset_id': asset.asset_id,
                'asset_revision': asset.revision, 'blob_hash': asset.blob_hash}, settle=False)
            self.assertEqual(submitted['status'], 'pending', submitted)
            ref = submitted['job_refs'][0]
            queued = progress_for(ref, self.ctx)
            self.assertEqual((queued['status'], queued['stage'], queued['observation']), ('queued', 'import', 'native'))
            self.assertIsNone(queued['progress_done'])
            self.assertIsNone(queued['progress_total'])
            self.assertEqual(queued, progress_for(ref, self.ctx))
            gate.set()
            for _ in range(600):
                completed = await asyncio.to_thread(progress_for, ref, self.ctx)
                if completed['status'] == 'succeeded':
                    break
                await asyncio.sleep(.05)
            else:
                self.fail('Real data worker did not complete')
            self.assertEqual(self.native.get(session)['version'], 2)
            self.assertEqual(self.native.page(session, 'table',
                self.native.get(session)['state']['tables']['left']['revision'], 0, 1)['rows'][0]['cells'][0]['value'], '000001')
            self.assertEqual(completed['observation'], 'native')
            self.assertNotIn('percent', completed)
            self.assertIsNone(completed['progress_total'])
            self.assertEqual(len(self.native.get(session)['uploads']), 1)
            validate_native_samples(ref, queued, completed)
            with self.assertRaises(ValueError):
                progress_for({**ref, 'parent_id': 'unownedSession'}, self.ctx)
            with self.assertRaises(ValueError):
                progress_for({**ref, 'coverage': {'kind': 'batch'}}, self.ctx)
            other = self.store.create_session('wtest', 'Unrelated', [], 'unrelated_progress')
            with self.assertRaises(ValueError):
                progress_for(ref, replace(self.ctx, session_id=other['id']))
        finally:
            gate.set()

    async def test_real_native_ocr_http_counts_queued_running_finished_and_unreachable(self):
        from PIL import Image
        from ocr_workbench.service import create_app
        from workbench_platform.jobs.ocr_handoff import OcrHandoff
        from workbench_platform.jobs.native_ocr import Capabilities, OcrAdapter, OcrUnavailable
        with patch.dict('os.environ', {'WORKBENCH_APP_ID': 'ocr',
            'WORKBENCH_INSTANCE_ID': 'instanceprogressocr', 'WORKBENCH_NONCE': 'b'*48}):
            app = create_app(self.native_ocr_source, self.root/'native-progress-ocr',
                'coverage-token-'+'a'*40, start_queue=False, review_only=True, agent_enabled=False)
        native = app.state.store
        project = native.project('Progress engineering check')
        url = self.serve(app)
        link = self.link('ocr', project['id'])
        self.jobs({'ocr': url}, {'ocr': 'instanceprogressocr'})
        self.services.ocr_handoff = OcrHandoff(self.root/'progress-handoffs.sqlite3',
            self.services.job_service, self.assets, self.sets)
        self.assets.roots['synthetic'] = self.root
        Image.new('RGB', (90,60), 'white').save(self.root/'progress-input.png')
        asset = self.assets.register('wtest', 'synthetic', 'progress-input.png', media_type='image/png', storage_mode='managed')
        fixed = self.assert_success(await self.call('assets_create_set', {'asset_ids': [asset.asset_id]}))
        app.state.application_services.require_recognition = lambda: None
        app.state.application_services.registry = SimpleNamespace(engines=lambda: {
            name: {'package_id': 'builtin', 'name': name} for name in ('ppocr', 'hunyuan')})
        with patch.object(OcrAdapter, 'capabilities', return_value=Capabilities(True, True, True, True, ('json',), None)):
            submitted = await self.call('ocr_run', {'link_id': link.link_id,
                'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision'],
                'engines': ['ppocr', 'hunyuan']}, settle=False, call_id='progress_ocr_once')
        self.assertEqual(submitted['status'], 'pending', submitted)
        ref = submitted['job_refs'][0]
        queued = progress_for(ref, self.ctx)
        self.assertEqual((queued['status'], queued['progress_done'], queued['progress_total']), ('queued', 0, 2))
        self.assertEqual(queued['counts'], {'total': 2, 'succeeded': 0, 'failed': 0, 'cancelled': 0, 'pending': 2})
        task = native.claim()
        self.assertIsNotNone(task)
        running = progress_for(ref, self.ctx)
        self.assertEqual((running['status'], running['progress_done'], running['progress_total']), ('running', 0, 2))
        self.assertEqual(running['observation'], 'native')
        with patch.object(self.services.job_service, '_snapshot', side_effect=OcrUnavailable()):
            unavailable = progress_for(ref, self.ctx)
        self.assertEqual((unavailable['status'], unavailable['observation'], unavailable['availability']),
                         ('unknown', 'unavailable', 'unreachable'))
        # Complete native task records with explicit engineering fixtures;
        # no OCR model or remote controller is invoked by this test.
        native.complete(task['id'], {'engine': task['engine'], 'text': 'Engineering fixture', 'tables': [], 'blocks': []})
        one = progress_for(ref, self.ctx)
        self.assertEqual((one['progress_done'], one['progress_total'], one['counts']['succeeded']), (1, 2, 1))
        task = native.claim()
        native.complete(task['id'], {'engine': task['engine'], 'text': 'Engineering fixture', 'tables': [], 'blocks': []})
        completed = progress_for(ref, self.ctx)
        self.assertEqual((completed['status'], completed['progress_done'], completed['progress_total']), ('succeeded', 2, 2))
        self.assertEqual(completed['capabilities'], [])
        self.assertEqual(completed, progress_for(ref, self.ctx))
        validate_native_samples(ref, queued, running, unavailable, one, completed)
        self.runtime.cancel(self.ctx.run_id)
        with self.assertRaises(RunSuperseded):
            progress_for(ref, self.ctx)


class GraphProgressTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.DataToolTests.setUp
    tearDown = base.DataToolTests.tearDown
    asset = base.DataToolTests.asset
    make_runtime = graph_tests.GraphTests.make_runtime
    wait_status = graph_tests.GraphTests.wait_status

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_invalid_batch_blocks_every_call_before_any_handler(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                value = graph_tests.response(state, 'data_create_session', {'name': 'Must not exist'})
                value['calls'].append({'call_id': 'invalid_import', 'tool': 'data_import', 'arguments': {'invalid': True}})
                return value
            return graph_tests.response(state, 'conversation_answer', {'content': 'The proposed batch was rejected before execution.'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], 'Check invalid batch', 'invalid_batch_progress')
            final = await self.wait_status(runtime, run['id'], {'answered', 'incomplete', 'failed'})
            self.assertEqual(final['status'], 'answered', runtime.store.events(session['id']))
            values = [json.loads(row['payload']) for row in runtime.store.rows(
                "SELECT payload FROM events WHERE run_id=? AND kind='tool_activity' ORDER BY seq", (run['id'],))]
            business = [item for item in values if item['tool'] in {'data_create_session', 'data_import'}]
            self.assertEqual([item['phase'] for item in business], ['blocked', 'blocked'])
            self.assertEqual(self.native.list('wtest'), [])
            self.assertEqual(runtime.store.rows("SELECT id FROM operations WHERE tool IN ('data_create_session','data_import')"), [])
            validate_public_events(runtime.store, run['id'])
        finally:
            await runtime.close()

    async def test_existing_observer_emits_real_progress_and_finished_without_extra_model_rounds(self):
        asset = self.asset('suppliers-left.csv')
        gate = threading.Event()
        self.native.pool.submit(gate.wait)
        observations = []
        async def provider(state, schemas):
            observations.append(state['step'])
            if state['step'] == 0:
                return graph_tests.response(state, 'data_create_session', {'name': 'Observed worker'})
            previous = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            if state['step'] == 1:
                return graph_tests.response(state, 'data_import', {'data_session_id': previous['data']['data_session_id'],
                    'expected_version': previous['data']['version'], 'side': 'left', 'asset_id': asset.asset_id,
                    'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
            self.assertEqual(previous['status'], 'success')
            if state['step'] == 2:
                self.assertEqual(previous['data']['version'], 2)
                return graph_tests.response(state, 'data_read_table', {'data_session_id': previous['data']['data_session_id'], 'side': 'left'})
            return graph_tests.response(state, 'delivery_publish', {'summary': 'Native import completed and its table was verified.',
                'result_refs': previous['result_refs']})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], 'Observe import', 'observer_progress')
            await self.wait_status(runtime, run['id'], {'waiting_job', 'failed'})
            for _ in range(100):
                events = runtime.store.rows("SELECT payload FROM events WHERE run_id=? AND kind='job_progress'", (run['id'],))
                if events:
                    break
                await asyncio.sleep(.1)
            else:
                self.fail('Native observer did not emit a progress observation')
            self.assertEqual(observations, [0, 1])
            first = json.loads(events[0]['payload'])
            self.assertEqual((first['status'], first['stage'], first['observation']), ('queued', 'import', 'native'))
            self.assertIsNone(first['progress_total'])
            self.assertEqual(first['job_ref']['object_id'], first['job_id'])
            gate.set()
            final = await self.wait_status(runtime, run['id'], {'succeeded', 'partial', 'failed', 'paused'})
            self.assertEqual(final['status'], 'succeeded', runtime.store.events(session['id']))
            self.assertEqual(observations, [0, 1, 2, 3])
            values = [json.loads(row['payload']) for row in runtime.store.rows(
                "SELECT payload FROM events WHERE run_id=? AND kind='tool_activity' ORDER BY seq", (run['id'],))]
            native = [item for item in values if item['tool'] == 'data_import']
            self.assertEqual([item['phase'] for item in native], ['started', 'pending', 'finished'])
            self.assertEqual(len({item['operation_id'] for item in native}), 1)
            self.assertEqual(native[-1]['status'], 'success')
            activities = [json.loads(row['payload'])['phase'] for row in runtime.store.rows(
                "SELECT payload FROM events WHERE run_id=? AND kind='agent_activity' ORDER BY seq", (run['id'],))]
            self.assertIn('waiting_job', activities)
            self.assertEqual(activities[-1], 'completed')
            validate_public_events(runtime.store, run['id'])
        finally:
            gate.set()
            await runtime.close()


    async def test_progress_event_write_failure_does_not_block_real_native_import(self):
        asset = self.asset('suppliers-left.csv')
        gate = threading.Event()
        self.native.pool.submit(gate.wait)
        observed, writes = [], []
        async def provider(state, schemas):
            observed.append(state['step'])
            if state['step'] == 0:
                return graph_tests.response(state, 'data_create_session', {'name': 'Display fault worker'})
            previous = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            if state['step'] == 1:
                return graph_tests.response(state, 'data_import', {'data_session_id': previous['data']['data_session_id'],
                    'expected_version': previous['data']['version'], 'side': 'left', 'asset_id': asset.asset_id,
                    'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
            self.assertEqual(previous['status'], 'success')
            if state['step'] == 2:
                self.assertEqual(previous['data']['version'], 2)
                return graph_tests.response(state, 'data_read_table', {'data_session_id': previous['data']['data_session_id'], 'side': 'left'})
            return graph_tests.response(state, 'delivery_publish', {'summary': 'Real import and table verification completed despite display write failure.',
                'result_refs': previous['result_refs']})
        runtime, session = await self.make_runtime(provider)
        original = runtime.store.public_event
        def fail_progress(run_id, generation, identity, kind, payload):
            if kind == 'job_progress':
                writes.append(payload)
                raise OSError('synthetic public progress write failure')
            return original(run_id, generation, identity, kind, payload)
        try:
            with patch.object(runtime.store, 'public_event', side_effect=fail_progress):
                run = await runtime.submit(session['id'], 'Import during display write failure', 'display_fault_import')
                await self.wait_status(runtime, run['id'], {'waiting_job', 'failed', 'needs_verification'})
                for _ in range(100):
                    if writes:
                        break
                    await asyncio.sleep(.1)
                else:
                    self.fail('Progress write fault was not exercised')
                self.assertEqual(runtime.store.run(run['id'])['status'], 'waiting_job')
                gate.set()
                final = await self.wait_status(runtime, run['id'], {'succeeded', 'partial', 'failed', 'paused', 'needs_verification'})
                self.assertEqual(final['status'], 'succeeded', runtime.store.events(session['id']))
            self.assertEqual(observed, [0, 1, 2, 3])
            native = self.native.get(self.native.list('wtest')[0]['id'])
            self.assertEqual((native['version'], len(native['uploads'])), (2, 1))
            self.assertEqual(runtime.store.rows("SELECT id FROM operations WHERE tool='data_import'")[0]['id'],
                next(json.loads(row['payload'])['operation_id'] for row in runtime.store.rows(
                    "SELECT payload FROM events WHERE kind='tool_activity'")
                    if json.loads(row['payload'])['tool'] == 'data_import'))
            validate_public_events(runtime.store, run['id'])
        finally:
            gate.set()
            await runtime.close()


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    setUp = ActivityTests.setUp
    tearDown = ActivityTests.tearDown
    asyncSetUp = ActivityTests.asyncSetUp

    async def test_cancel_after_business_completion_does_not_publish_stopped(self):
        runtime = object.__new__(AgentRuntime)
        runtime.store = self.store
        self.store.status(self.ctx.run_id, 1, 'answered')
        self.store.finish_public(self.ctx.run_id, 'answered')
        result = runtime.cancel(self.ctx.run_id)
        self.assertEqual((result['status'], result['generation']), ('answered', 1))
        phases = [json.loads(row['payload'])['phase'] for row in self.store.rows(
            "SELECT payload FROM events WHERE kind='agent_activity' ORDER BY seq")]
        self.assertEqual(phases, ['completed'])
        self.assertEqual(self.store.rows("SELECT seq FROM events WHERE kind='cancelled'"), [])

    async def test_progress_event_write_failure_still_settles_original_job(self):
        from unittest.mock import AsyncMock
        runtime = object.__new__(AgentRuntime)
        runtime.store, runtime.services = self.store, self.ctx.services
        reference = AgentRef(kind='job', workspace_id='wtest', object_id='jobDisplayFailure',
            revision=1, application='ocr').model_dump()
        state = {'session_id': self.ctx.session_id, 'workspace_id': 'wtest',
            'run_id': self.ctx.run_id, 'generation': 1,
            'results': [AgentToolResult(status='pending', summary='Queued', job_refs=[reference]).model_dump()]}
        runtime.graph = SimpleNamespace(save=lambda _state: None)
        settled = AgentToolResult(status='success', summary='Same original job finished').model_dump()
        runtime.settle = AsyncMock(side_effect=[None, settled])
        operation, _ = self.store.begin_operation(self.ctx.run_id, 1, self.call)
        self.store.finish_operation(operation['id'], state['results'][-1])
        original, attempted = self.store.public_event, []
        def progress_write(run_id, generation, identity, kind, payload):
            if kind == 'job_progress':
                attempted.append(payload)
                raise OSError('display storage unavailable')
            return original(run_id, generation, identity, kind, payload)
        with patch('workbench_platform.agent.progress.progress_for', return_value={'status': 'queued'}), \
             patch.object(self.store, 'public_event', side_effect=progress_write):
            actual = await asyncio.wait_for(runtime.harness_settle(state, self.call, state['results'][-1], operation['id']), 5)
        self.assertEqual(actual, {**settled, 'job_refs': [reference]})
        self.assertEqual(runtime.settle.await_count, 2)
        self.assertEqual(len(attempted), 2)
        self.assertEqual(len(self.store.rows('SELECT id FROM operations')), 1)
        self.assertEqual(self.store.run(self.ctx.run_id)['status'], 'running')


class SettleFenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = ActivityTests.setUp
    tearDown = ActivityTests.tearDown
    asyncSetUp = ActivityTests.asyncSetUp
    stop = ActivityTests.stop

    def reference(self, family):
        return AgentRef(kind='job', workspace_id='wtest', object_id='jobFence', revision=1,
            application=family, parent_id='nativeSession').model_dump()

    async def test_runtime_rejects_stale_generation_before_any_native_handler(self):
        from unittest.mock import Mock
        runtime = object.__new__(AgentRuntime)
        runtime.store, runtime.services = self.store, self.ctx.services
        handler = Mock(return_value={'status': 'success'})
        state = {'session_id': self.ctx.session_id, 'workspace_id': 'wtest',
                 'run_id': self.ctx.run_id, 'generation': 1}
        self.stop()
        with patch('workbench_platform.application_services.job_lifecycle.adapter', handler):
            for family in ('data', 'ocr', 'classification'):
                with self.assertRaises(RunSuperseded):
                    await runtime.settle({'job_refs': [self.reference(family)]}, state)
        handler.assert_not_called()

    async def test_data_cancel_during_native_read_never_resumes_interrupted_operation(self):
        from unittest.mock import Mock
        from workbench_platform.application_services.operations import data
        ref = self.reference('data')
        operation, _ = self.store.begin_operation(self.ctx.run_id, 1, self.call)
        self.store.finish_operation(operation['id'], AgentToolResult(status='pending', summary='Queued',
            job_refs=[AgentRef.model_validate(ref)]).model_dump())
        def read_job(session, job):
            self.stop()
            return {'status': 'interrupted', 'id': 'jobFence'}
        resume = Mock(side_effect=AssertionError('Stopped operation must not resume'))
        self.ctx.services.data_workbench = SimpleNamespace(
            get=lambda _: {'workspace_id': 'wtest', 'id': 'nativeSession'},
            job=read_job, resume_operation=resume)
        with self.assertRaises(RunSuperseded):
            data.settle(ref, self.ctx)
        resume.assert_not_called()

    async def test_classification_cancel_during_native_read_never_sends_resume(self):
        from unittest.mock import Mock
        from workbench_platform.application_services.operations import classification
        action = Mock(side_effect=AssertionError('Stopped classification must not resume'))
        self.ctx.services.job_service = SimpleNamespace(action=action)
        def read_job(context, job):
            self.stop()
            return {'status': 'paused', 'capabilities': ['resume']}
        with patch.object(classification, 'read_job', side_effect=read_job):
            with self.assertRaises(RunSuperseded):
                classification.settle(self.reference('classification'), self.ctx)
        action.assert_not_called()

    async def test_ocr_cancel_during_native_read_never_posts_queue_resume(self):
        from unittest.mock import Mock
        from workbench_platform.application_services.operations import ocr
        request = Mock(side_effect=AssertionError('Stopped OCR must not resume'))
        adapter = SimpleNamespace(context=SimpleNamespace(module_project_id='nativeProject'),
            batch=lambda _: SimpleNamespace(tasks=[SimpleNamespace(native_task_id='taskFence', native_status='paused')]),
            _request=request)
        self.ctx.services.ocr_handoff = SimpleNamespace(_context=lambda *args: object(), _adapter=lambda _: adapter)
        def read_job(context, job):
            self.stop()
            return {'status': 'paused', 'native_job_ids': ['taskFence']}
        with patch.object(ocr, 'read_job', side_effect=read_job), \
             patch.object(ocr, 'scoped_handoff', return_value={'link_id': 'linkFence'}):
            with self.assertRaises(RunSuperseded):
                ocr.settle(self.reference('ocr'), self.ctx)
        request.assert_not_called()
