"""Execution boundaries with real stores/workers; never controller scores."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest
from contextlib import closing

import test_data_tools as data_tests
import test_graph as graph_tests
response = graph_tests.response
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.tools import data, results
from workbench_platform.application_services.references import verify, asset_allowed
from packages.contracts.agent import AgentModel


class ReferenceTests(data_tests.DataToolTests):
    # Reuse only setup helpers, not the parent suite's tests.
    test_real_clean_compare_export_independent_values_and_native_provenance = None
    test_multisheet_is_discovered_and_not_implicitly_imported = None
    test_native_upload_replay_version_and_cross_project_boundaries = None

    def setUp(self):
        super().setUp()
        uploads = Path(self.temporary.name) / 'uploads'
        uploads.mkdir()
        self.assets.roots['uploads'] = uploads

    async def test_selected_scope_covers_preexisting_data_inputs(self):
        selected = self.asset('suppliers-left.csv')
        outside = self.asset('suppliers-right.csv')
        session = await self.create()
        await self.import_asset(session, 'left', outside)
        restricted = replace(self.ctx, selected_assets=(selected.asset_id,))
        with self.assertRaises(ValueError):
            data.data_session(data.DataTable(data_session_id=session, side='left'), restricted)

    async def test_revision_hash_rejects_changed_cached_rows(self):
        session = await self.create()
        await self.import_asset(session, 'left', self.asset('suppliers-left.csv'))
        value = await self.call('data_read_table', {'data_session_id': session, 'side': 'left'})
        ref = value['result_refs'][0]
        verify(ref, self.ctx)
        with closing(sqlite3.connect(self.native.session_dir(session) / 'views.sqlite3')) as db, db:
            row = db.execute('SELECT payload FROM table_rows WHERE revision=? AND ordinal=0', (ref['object_id'],)).fetchone()
            payload = json.loads(row[0]); payload['cells'][0]['value'] = 'corrupted'
            db.execute('UPDATE table_rows SET payload=? WHERE revision=? AND ordinal=0', (json.dumps(payload), ref['object_id']))
        with self.assertRaises(ValueError):
            verify(ref, self.ctx)

    async def test_derived_sources_flatten_and_corruption_blocks_scope(self):
        original = self.asset('suppliers-left.csv')
        restricted = replace(self.ctx, selected_assets=(original.asset_id,), operation_id='bridge_one')
        first = results.install(b'a,b\n000001,100.00\n', 'one.csv', 'text/csv', [], [original.asset_id], {'known': True}, restricted)
        derived = first['data']['asset_id']
        self.assertTrue(asset_allowed(restricted, derived))
        second = results.install(b'a,b\n000003,50.00\n', 'two.csv', 'text/csv', [], [derived], {}, replace(restricted, operation_id='bridge_two'))
        second_id = second['data']['asset_id']
        row = self.store.rows('SELECT * FROM derived_inputs WHERE asset_id=?', (second_id,))[0]
        self.assertEqual(json.loads(row['leaf_assets']), [original.asset_id])
        self.assertTrue(asset_allowed(restricted, second_id))
        (self.store.root / row['provenance_path']).write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            asset_allowed(restricted, second_id)

    async def test_native_receipt_loss_recovers_same_import_job(self):
        session = await self.create()
        asset = self.asset('suppliers-left.csv')
        arguments = {'data_session_id': session, 'expected_version': 1, 'side': 'left',
                     'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash}
        call = {'call_id': 'lost_receipt', 'tool': 'data_import', 'arguments': arguments}
        operation, _ = self.store.begin_operation(self.ctx.run_id, 1, call)
        native = data.import_data(data.DataImport.model_validate(arguments), replace(self.ctx, operation_id=operation['id']))
        recovered = await self.registry.dispatch(call, self.ctx)
        self.assertEqual(recovered['job_refs'], native['job_refs'])
        self.assertEqual(len(self.native.get(session)['jobs']), 1)
        self.assertEqual(len(self.native.get(session)['uploads']), 1)

    async def test_transport_fault_after_native_commit_queries_same_import(self):
        session = await self.create()
        asset = self.asset('suppliers-left.csv')
        original = self.registry.tools['data_import']
        executed = []
        def lose_response(args, ctx):
            value = data.import_data(args, ctx)
            executed.append(value)
            raise TimeoutError('lost response')
        self.registry.tools['data_import'] = replace(original, execute=lose_response)
        result = await self.call('data_import', {'data_session_id': session, 'expected_version': 1, 'side': 'left',
            'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(len(executed), 1)
        self.assertEqual(len(self.native.get(session)['jobs']), 1)
        self.assertEqual(len(self.native.get(session)['uploads']), 1)


class BoundaryGraphTests(graph_tests.GraphTests):
    test_native_wait_does_not_call_model_until_result_and_delivery_is_readable = None
    test_bound_question_survives_close_reopen_and_changes_next_tool = None
    test_stop_fences_late_controller_response = None
    test_no_delivery_cannot_be_marked_success = None

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_append_during_native_wait_withholds_rest_of_batch(self):
        ready = asyncio.Event(); release = asyncio.Event()
        async def provider(state, schemas):
            if state['step'] == 0:
                value = response(state, 'data_create_session', {'name': 'first'})
                value['calls'].append({'call_id': 'stale_write', 'tool': 'data_create_session', 'arguments': {'name': 'stale'}})
                return value
            self.assertTrue(any(message.get('content') == '只保留第一个作业' for message in state['messages']))
            return response(state, content='已收窄目标')
        runtime, session = await self.make_runtime(provider)
        original = runtime.registry.tools['data_create_session']
        async def gated(args, ctx):
            ready.set(); await release.wait()
            return data.create(args, ctx)
        runtime.registry.tools['data_create_session'] = replace(original, execute=gated)
        try:
            run = await runtime.submit(session['id'], '创建两项作业', 'start_batch')
            await ready.wait()
            await runtime.submit(session['id'], '只保留第一个作业', 'narrow_goal')
            release.set()
            final = await self.wait_status(runtime, run['id'], {'paused', 'failed'})
            self.assertEqual(final['status'], 'paused')
            self.assertEqual([item['name'] for item in self.native.list('wtest')], ['first'])
            self.assertEqual(len(runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],))), 1)
        finally:
            await runtime.close()

    async def test_initial_checkpoint_window_and_duplicate_inbox(self):
        async def provider(state, schemas):
            return response(state, content='没有成果，不宣称成功')
        runtime, session = await self.make_runtime(provider)
        await runtime.close()
        run, _ = runtime.store.start_run(session['id'], '启动回执已保存', 'initial_request', 0)
        from workbench_platform.agent.runtime import AgentRuntime
        reopened = AgentRuntime(runtime.store.root, self.services, model_provider=provider)
        self.services.agent = reopened
        await reopened.start()
        try:
            final = await self.wait_status(reopened, run['id'], {'paused', 'failed'})
            self.assertEqual(final['status'], 'paused')
            duplicate = await reopened.submit(session['id'], '启动回执已保存', 'initial_request')
            self.assertEqual(duplicate['id'], run['id'])
            self.assertEqual(len(reopened.store.rows('SELECT * FROM runs')), 1)
        finally:
            await reopened.close()

    async def test_budget_extension_is_explicit_and_reply_idempotent(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                return response(state, 'data_create_session', {'name': 'budget'})
            return response(state, content='停止')
        runtime, session = await self.make_runtime(provider)
        runtime.round_limit = 1
        try:
            run = await runtime.submit(session['id'], '预算检查', 'budget_request')
            await self.wait_status(runtime, run['id'], {'waiting_user'})
            question = runtime.store.rows('SELECT * FROM decisions WHERE run_id=?', (run['id'],))[0]
            await runtime.reply(question['id'], '继续（增加20轮、60次工具）', 1, 'budget_reply')
            await self.wait_status(runtime, run['id'], {'paused', 'failed'})
            await runtime.reply(question['id'], '继续（增加20轮、60次工具）', 1, 'budget_reply')
            self.assertEqual(runtime.store.run(run['id'])['round_limit'], 21)
            self.assertIsNone(runtime.store.run(run['id'])['tool_limit'])
        finally:
            await runtime.close()

    async def test_pause_then_immediate_resume_while_old_child_closes_fences_late_reply(self):
        pending, old_reply, late_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closing, allow_close = asyncio.Event(), asyncio.Event()
        calls = []
        async def provider(state, schemas):
            calls.append(state['generation'])
            if len(calls) == 1:
                pending.set()
                try:
                    await old_reply.wait()
                except asyncio.CancelledError:
                    pass
                late_returned.set()
                return response(state, 'data_create_session', {'name': 'old generation must not write'})
            return response(state, 'conversation_answer', {'content': '已按原目标恢复并完成讨论。'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '讨论后暂停再继续', 'pause_immediate_resume')
            await asyncio.wait_for(pending.wait(), 60)
            process = runtime.graph.processes[run['id']]
            original_close = process.close
            async def gated_close():
                closing.set()
                await allow_close.wait()
                await original_close()
            process.close = gated_close
            paused = await runtime.pause(run['id'])
            self.assertEqual(paused['status'], 'paused')
            self.assertEqual(paused['generation'], run['generation'] + 1)
            await asyncio.wait_for(closing.wait(), 60)
            resumed = await runtime.reconcile(run['id'])
            self.assertEqual(resumed['status'], 'running')
            self.assertIn(run['id'], runtime.running)
            self.assertIn(run['id'], runtime.reschedules)
            old_reply.set()
            await asyncio.wait_for(late_returned.wait(), 60)
            self.assertEqual(self.native.list('wtest'), [])
            allow_close.set()
            final = await self.wait_status(runtime, run['id'], {'answered', 'failed', 'needs_verification'})
            self.assertEqual(final['status'], 'answered', runtime.store.events(session['id']))
            self.assertEqual(calls, [run['generation'], paused['generation']])
            self.assertEqual(self.native.list('wtest'), [])
            self.assertEqual([row['tool'] for row in runtime.store.rows('SELECT tool FROM operations')], ['conversation_answer'])
            self.assertEqual(len(runtime.store.rows("SELECT id FROM model_requests WHERE state='cancelled'")), 1)
        finally:
            old_reply.set()
            allow_close.set()
            await runtime.close()
