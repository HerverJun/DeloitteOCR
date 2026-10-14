"""Focused product behavior checks with synthetic inputs and real data workers."""
import asyncio
from dataclasses import replace
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import test_data_tools as data_tests
import test_graph as graph_tests
import test_routes as route_tests
from packages.contracts.audit import DirectEvidenceRequest, AuditDraftWrite, AuditDraftExport
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.providers import request, ProviderFault
from workbench_platform.audit.service import AuditService
from workbench_platform.application_services.manual import candidates
from workbench_platform.application_services.search import search
from workbench_platform.agent.tools.audit import Search


class ProductGraphTests(unittest.IsolatedAsyncioTestCase):
    setUp = data_tests.DataToolTests.setUp
    tearDown = data_tests.DataToolTests.tearDown
    asset = data_tests.DataToolTests.asset
    call = data_tests.DataToolTests.call
    create = data_tests.DataToolTests.create
    import_asset = data_tests.DataToolTests.import_asset
    wait_status = graph_tests.GraphTests.wait_status
    make_runtime = graph_tests.GraphTests.make_runtime

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60

    def audit_service(self):
        self.services.batch_service = SimpleNamespace(workspace=lambda identifier: self.workspaces.get_workspace(identifier))
        self.services.audit_service = AuditService(SimpleNamespace(state=self.services), Path(self.temporary.name) / 'audit')
        return self.services.audit_service

    async def test_normal_discussion_and_saved_answer_survive_next_turn(self):
        async def provider(state, schemas):
            return graph_tests.response(state, 'conversation_answer', {'content': '先确定供应商编号及金额口径，再读取台账。'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '如何核对台账？', 'discussion1')
            final = await self.wait_status(runtime, run['id'], {'answered', 'failed'})
            self.assertEqual(final['status'], 'answered')
            self.assertIn('供应商编号', json.dumps(runtime.session_facts({'id': 'next', 'session_id': session['id'], 'workspace_id': 'wtest'}), ensure_ascii=False))
            events = runtime.store.events(session['id'])['items']
            self.assertFalse(any(event['kind'] == 'delivery' for event in events))
            task = runtime.tasks.get(session['id'])
            self.assertEqual(task['status'], 'answered')
        finally:
            await runtime.close()

    async def test_scope_change_withholds_inflight_call_and_applies_at_next_boundary(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        asset = self.asset('suppliers-left.csv')
        async def provider(state, schemas):
            if state['step'] == 0:
                waiting.set(); await release.wait()
                return graph_tests.response(state, 'data_create_session', {'name': '旧范围调用不可执行'})
            self.assertEqual(runtime.store.session(session['id'])['selected_assets'], [asset.asset_id])
            return graph_tests.response(state, 'conversation_answer', {'content': '已读取更新后的资料范围。'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '讨论资料', 'scope1'); await waiting.wait()
            task = runtime.tasks.get(session['id'])
            command = {'request_id': 'scopechange', 'expected_revision': task['revision'], 'selected_assets': [asset.asset_id]}
            first = runtime.tasks.update(session['id'], command)
            self.assertTrue(first['queued']); self.assertEqual(runtime.tasks.update(session['id'], command), first)
            release.set(); final = await self.wait_status(runtime, run['id'], {'answered', 'failed'})
            self.assertEqual(final['status'], 'answered'); self.assertEqual(self.native.list('wtest'), [])
            self.assertEqual(runtime.tasks.get(session['id'])['scope_revision'], 2)
        finally:
            await runtime.close()

    async def test_manual_takeover_fences_late_model_and_return_preserves_current_results(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        async def provider(state, schemas):
            waiting.set(); await release.wait()
            return graph_tests.response(state, 'data_create_session', {'name': '迟到调用'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '任务', 'manual1'); await waiting.wait()
            task = runtime.tasks.get(session['id'])
            command = {'request_id': 'takeover', 'expected_revision': task['revision'], 'action': 'take_over'}
            receipt = runtime.tasks.control(session['id'], command)
            self.assertEqual(runtime.tasks.control(session['id'], command), receipt)
            release.set(); await asyncio.sleep(.2)
            self.assertEqual(runtime.store.run(run['id'])['status'], 'cancelled'); self.assertEqual(self.native.list('wtest'), [])
            with self.assertRaises(ValueError): await runtime.submit(session['id'], '新动作', 'manualblocked')
            task = runtime.tasks.get(session['id'])
            receipt = runtime.tasks.return_results(session['id'], {'request_id': 'return1', 'expected_revision': task['revision'], 'summary': '已人工保存并修改金额口径', 'references': []})
            self.assertFalse(receipt['manual'])
            self.assertIn('人工保存', json.dumps(runtime.session_facts({'id': 'next', 'session_id': session['id'], 'workspace_id': 'wtest'}), ensure_ascii=False))
        finally:
            await runtime.close()

    async def test_decimal_summary_direct_evidence_editable_workpaper_and_revision_warning(self):
        (data_tests.RUN / 'fixtures/product-grouping.csv').write_text('供应商编号,金额\n000001,1.005\n000001,2.995\n123456789012345678901234,1000000000000000000000000000000.01\n123456789012345678901234,0.09\n', encoding='utf-8-sig')
        session_id = await self.create(); asset = self.asset('product-grouping.csv')
        await self.import_asset(session_id, 'left', asset)
        columns = self.native.get(session_id)['state']['tables']['left']['columns']
        amount = next(column['column_id'] for column in columns if column['header'] in {'金额', 'amount'})
        key = columns[0]['column_id']
        await self.call('data_apply_step', {'data_session_id': session_id, 'expected_version': self.native.get(session_id)['version'], 'side': 'left', 'step': {'operator': 'cast', 'column': amount, 'target': 'decimal'}})
        original = await self.call('data_read_table', {'data_session_id': session_id, 'side': 'left'})
        from decimal import Decimal, localcontext
        expected = {}
        amount_index = next(i for i, column in enumerate(columns) if column['column_id'] == amount)
        for row in original['data']['rows']:
            expected.setdefault(row['cells'][0]['value'], []).append(Decimal(row['cells'][amount_index]['value']))
        await self.call('data_apply_step', {'data_session_id': session_id, 'expected_version': self.native.get(session_id)['version'], 'side': 'left', 'step': {'operator': 'group_summary', 'columns': [key], 'amount_columns': [amount]}})
        grouped = await self.call('data_read_table', {'data_session_id': session_id, 'side': 'left'})
        for row in grouped['data']['rows']:
            with localcontext() as context:
                context.prec = 4096
                self.assertEqual(Decimal(row['cells'][-1]['value']), sum(expected[row['cells'][0]['value']], Decimal(0)))
            self.assertEqual(len(row['source_rows']), int(row['cells'][1]['value']))
        self.audit_service()
        async def provider(state, schemas): return graph_tests.response(state, 'conversation_answer', {'content': '已准备好审计任务。'})
        runtime, conversation = await self.make_runtime(provider)
        try:
            ctx = replace(self.ctx, session_id=conversation['id'], run_id='', store=runtime.store, services=self.services)
            bound = self.services.audit_service.drafts.bind(DirectEvidenceRequest(reference=grouped['result_refs'][0]), ctx)
            self.assertTrue(bound['sources']); self.assertTrue(any(source['row_number'] >= 2 for source in bound['sources']))
            command = AuditDraftWrite(request_id='finding1', expected_revision=0, title='分组金额核对', facts='已按供应商编号汇总台账。', interpretation='需进一步与支付台账核对。', evidence_ids=[bound['evidence_id']])
            finding = self.services.audit_service.drafts.write(command, ctx, {'kind': 'human', 'formal_signature': False})
            self.assertEqual(self.services.audit_service.drafts.write(command, ctx, {'kind': 'human'})['revision'], 1)
            bundle = self.services.audit_service.drafts.export(AuditDraftExport(request_id='workpaper1', draft_ids=[finding['draft_id']]), ctx, {'kind': 'human'})
            self.assertEqual(bundle['kind'], 'audit_draft'); self.assertEqual({file['name'] for file in bundle['files']}, {'workpaper.md', 'workpaper.html', 'findings.json', 'evidence.json', 'sources.csv'})
            for file in bundle['files']:
                artifact = self.center.file('wtest', bundle['bundle_id'], file['name'])
                with self.center.store.stage_download('wtest', artifact.artifact_id, expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                    self.assertGreater(path.stat().st_size, 0)
            restored = candidates(ctx)
            self.assertTrue(any(item['reference']['object_id'] == grouped['result_refs'][0]['object_id'] for item in restored['items']))
            await self.call('data_apply_step', {'data_session_id': session_id, 'expected_version': self.native.get(session_id)['version'], 'side': 'left', 'step': {'operator': 'rename', 'column': key, 'header': '供应商编号'}})
            self.assertTrue(self.services.audit_service.drafts.get(finding['draft_id'], ctx)['outdated'])
            self.assertTrue(self.services.audit_service.drafts.evidence_status(bound['evidence_id'], ctx)['stale'])
            self.assertEqual(self.center.get('wtest', bundle['bundle_id'])['status'], 'outdated')
            runtime.tasks.save_profile('wtest', {'request_id': 'newprofile', 'expected_revision': 0, 'rules': '新增金额口径'})
            self.assertIn('项目背景或审计口径已变更', self.services.audit_service.drafts.get(finding['draft_id'], ctx)['outdated_reasons'])
            matches = search(Search(query='000001'), ctx)
            self.assertTrue(matches['items']); self.assertIn('reference', matches['items'][0])
        finally:
            await runtime.close()

    async def test_interrupted_workpaper_reuses_reserved_snapshot_after_finding_edit(self):
        self.audit_service()
        async def provider(state, schemas): return graph_tests.response(state, 'conversation_answer', {'content': '等待补充资料。'})
        runtime, session = await self.make_runtime(provider)
        try:
            ctx = replace(self.ctx, session_id=session['id'], run_id='', store=runtime.store, services=self.services)
            drafts = self.services.audit_service.drafts
            finding = drafts.write(AuditDraftWrite(request_id='recoverfinding1', expected_revision=0, title='待补材料', missing_information='尚未取得支付明细'), ctx, {'kind': 'human'})
            command = AuditDraftExport(request_id='recoverexport', draft_ids=[finding['draft_id']])
            actual_publish, count = self.center.store.publish_managed, 0
            def interrupted(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 2: raise OSError('synthetic publication interruption')
                return actual_publish(*args, **kwargs)
            with patch.object(self.center.store, 'publish_managed', interrupted):
                with self.assertRaises(OSError): drafts.export(command, ctx, {'kind': 'human'})
            drafts.write(AuditDraftWrite(request_id='recoverfinding2', draft_id=finding['draft_id'], expected_revision=1, title='新的待补材料', missing_information='另需补充凭证'), ctx, {'kind': 'human'})
            result = drafts.export(command, ctx, {'kind': 'human'})
            self.assertEqual(len(result['files']), 5); self.assertEqual(result['status'], 'outdated')
            file = self.center.file('wtest', result['bundle_id'], 'findings.json')
            with self.center.store.stage_download('wtest', file.artifact_id, expected_revision=file.revision, expected_hash=file.blob_hash) as saved:
                contents = json.loads(saved.read_text(encoding='utf-8'))
            self.assertEqual(contents[0]['revision'], 1); self.assertEqual(contents[0]['title'], '待补材料')
            self.assertEqual(drafts.export(command, ctx, {'kind': 'human'})['snapshot_hash'], result['snapshot_hash'])
        finally:
            await runtime.close()

    async def test_agent_plan_evidence_finding_and_workpaper_use_registry_end_to_end(self):
        native_session = await self.create(); asset = self.asset('suppliers-left.csv')
        await self.import_asset(native_session, 'left', asset)
        table = await self.call('data_read_table', {'data_session_id': native_session, 'side': 'left'})
        reference = table['result_refs'][0]
        self.audit_service()
        async def provider(state, schemas):
            step = state['step']
            if step == 0:
                return graph_tests.response(state, 'audit_update_plan', {'items': [{'key': 'observe', 'title': '固定台账证据', 'status': 'running'}, {'key': 'draft', 'title': '形成发现及底稿', 'depends_on': ['observe']}]})
            previous = state['messages'][-1]['result']
            self.assertEqual(previous['status'], 'success', previous)
            if step == 1: return graph_tests.response(state, 'audit_bind_evidence', {'reference': reference})
            if step == 2: return graph_tests.response(state, 'audit_write_finding', {'expected_revision': 0, 'title': '台账初步核对', 'facts': '台账中的原始供应商编号得到保留。', 'missing_information': '待取得支付台账继续核对。', 'evidence_ids': [previous['data']['evidence_id']]})
            if step == 3: return graph_tests.response(state, 'audit_generate_workpaper', {'draft_ids': [previous['data']['draft_id']]})
            if step == 4: return graph_tests.response(state, 'delivery_publish', {'summary': '已固定台账证据并形成底稿草稿，支付台账待补充。', 'artifact_refs': previous['artifact_refs']})
            return graph_tests.response(state, content='已有草稿可下载。')
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '整理台账证据并形成底稿草稿', 'fullproduct1')
            final = await self.wait_status(runtime, run['id'], {'succeeded', 'partial', 'failed', 'incomplete'})
            self.assertEqual(final['status'], 'succeeded', runtime.store.events(session['id']))
            self.assertEqual(runtime.tasks.get(session['id'])['plan'][0]['title'], '固定台账证据')
            ctx = replace(self.ctx, session_id=session['id'], run_id='', store=runtime.store, services=self.services)
            finding = self.services.audit_service.drafts.list(ctx)['items'][0]
            self.assertEqual(finding['author']['kind'], 'agent'); self.assertEqual(finding['author']['run_id'], run['id'])
            self.assertFalse('prepared' in finding); self.assertEqual(finding['evidence_ids'].__len__(), 1)
            delivery = next(event for event in runtime.store.events(session['id'], limit=10000)['items'] if event['kind'] == 'delivery')
            self.assertEqual(len(delivery['payload']['delivery']['data']['delivery'][0]['files']), 5)
        finally:
            await runtime.close()

    async def test_typed_filter_summary_unknown_amount_and_group_comparison(self):
        from modules.data_workbench.recipes.engine import _operate, _parameters, Step, RecipeError
        from modules.data_workbench.comparison.exact import _snapshot
        columns = [{'column_id': 'c1', 'header': '供应商'}, {'column_id': 'c2', 'header': '金额'}]
        rows = [{'row_id': 'r' + str(i).zfill(32), 'source_row': i + 2, 'cells': [{'kind': 'text', 'raw': '000001', 'value': '000001'}, {'kind': kind, 'raw': value, 'value': value}]} for i, (kind, value) in enumerate([('decimal', '1.005'), ('decimal', '2.995'), ('text', '未知')])]
        filtered, event = _operate({'columns': columns, 'rows': rows}, 'filter_compare', _parameters(Step('filter_compare', {'column': 'c2', 'predicate': 'between', 'value_kind': 'decimal', 'value': '1.005', 'upper': '2.995'}), columns))
        self.assertEqual(len(filtered['rows']), 2); self.assertEqual(event['errors_total'], 1)
        parameters = _parameters(Step('group_summary', {'columns': ['c1'], 'amount_columns': ['c2']}), columns)
        grouped, _ = _operate(filtered, 'group_summary', parameters)
        self.assertEqual(grouped['rows'][0]['cells'][-1]['value'], '4.000'); self.assertEqual(grouped['rows'][0]['cells'][0]['value'], '000001')
        _snapshot(grouped, 'v' + 'a' * 64)
        unknown, event = _operate({'columns': columns, 'rows': rows}, 'group_summary', parameters)
        self.assertEqual(unknown['rows'][0]['cells'][-1]['kind'], 'error'); self.assertIsNone(unknown['rows'][0]['cells'][-1]['value'])
        self.assertEqual(event['errors_total'], 1)
        with self.assertRaises(RecipeError): _parameters(Step('filter_compare', {'column': 'c2', 'predicate': 'gt', 'value_kind': 'decimal', 'value': 'NaN'}), columns)


class ProductRouteTests(unittest.TestCase):
    setUp = route_tests.AgentRouteTests.setUp
    tearDown = route_tests.AgentRouteTests.tearDown
    create = route_tests.AgentRouteTests.create

    def test_unconfigured_draft_profile_revisions_and_project_overview(self):
        identifier = self.create(); path = f'/api/platform/v1/agent/sessions/{identifier}/task'
        task = self.client.get(path, headers=route_tests.LOCAL).json()
        command = {'request_id': 'draft1', 'expected_revision': task['revision'], 'name': '审计讨论', 'goal': '讨论审计口径'}
        receipt = self.client.put(path, headers=route_tests.LOCAL, json=command)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(self.client.put(path, headers=route_tests.LOCAL, json=command).json(), receipt.json())
        conflict = self.client.put(path, headers=route_tests.LOCAL, json={**command, 'request_id': 'conflict1'})
        self.assertEqual(conflict.status_code, 409)
        profile = f'/api/platform/v1/workspaces/{self.workspace}/audit-profile'
        result = self.client.put(profile, headers=route_tests.LOCAL, json={'request_id': 'profile1', 'expected_revision': 0, 'rules': '金额超过1元需核对'})
        self.assertEqual(result.status_code, 200, result.text)
        invalid = self.client.put(profile, headers=route_tests.LOCAL, json={'request_id': 'profile2', 'expected_revision': 1, 'period_start': '2026-02-31'})
        self.assertEqual(invalid.status_code, 400)
        overview = self.client.get(f'/api/platform/v1/workspaces/{self.workspace}/audit-overview', headers=route_tests.LOCAL)
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(overview.json()['tasks']['items'][0]['goal'], '讨论审计口径')
        manual = self.client.get(f'/api/platform/v1/agent/sessions/{identifier}/manual-results', headers=route_tests.LOCAL)
        self.assertEqual(manual.status_code, 200, manual.text)
        self.assertIn('items', manual.json())


class NativeCandidateTests(unittest.TestCase):
    def test_pdf_unselected_page_does_not_hide_saved_page_and_classification_results(self):
        def connect():
            db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
            db.executescript("CREATE TABLE ocr_handoff(handoff_id TEXT,workspace_id TEXT,updated_at TEXT); INSERT INTO ocr_handoff VALUES('hpdf','wtest','2026'); CREATE TABLE classification_handoff(handoff_id TEXT,workspace_id TEXT,updated_at TEXT);")
            return db
        refs = [
            {'kind': 'job', 'application': 'classification', 'parent_id': 'hclass', 'object_id': 'jobclass', 'workspace_id': 'wtest', 'revision': 1},
        ]
        pdf_ref = {'kind': 'result', 'application': 'ocr', 'parent_id': 'hpdf', 'object_id': 'pdfresult', 'workspace_id': 'wtest', 'revision': 2}
        class_ref = {'kind': 'result', 'application': 'classification', 'parent_id': 'hclass', 'object_id': 'classresult', 'workspace_id': 'wtest', 'revision': 'manualrevision'}
        services = SimpleNamespace(data_workbench=SimpleNamespace(list_page=lambda *args: {'items': [], 'total': 0}),
            artifact_center=SimpleNamespace(list=lambda *args: {'items': []}),
            ocr_handoff=SimpleNamespace(_connect=connect),
            classification_handoff=SimpleNamespace(_connect=connect),
            agent=SimpleNamespace(tasks=SimpleNamespace(get=lambda identifier: {'result_refs': [], 'initial_refs': [], 'job_refs': refs})))
        ctx = SimpleNamespace(services=services, workspace_id='wtest', session_id='task')
        adapter = SimpleNamespace(document_pages=lambda *args, **kwargs: {'pages': [{'page_number': 1}, {'page_number': 2}]})
        def page(args, context):
            if args.page_number == 1: raise ValueError('page outside explicit selection')
            return {'result_refs': [pdf_ref]}
        with patch('workbench_platform.application_services.manual.verify', lambda *args: {}), \
             patch('workbench_platform.application_services.manual.reference_assets', return_value=[]), \
             patch('workbench_platform.agent.tools.ocr.read', side_effect=ValueError('PDF handoff')), \
             patch('workbench_platform.agent.tools.pdf.bound', return_value=(None, None, adapter, {'document': {}})), \
             patch('workbench_platform.agent.tools.pdf.read', side_effect=page), \
             patch('workbench_platform.agent.tools.classification.snapshot', return_value={}), \
             patch('workbench_platform.agent.tools.classification.reference', return_value=class_ref):
            value = candidates(ctx)
        self.assertEqual({item['reference']['object_id'] for item in value['items']}, {'pdfresult', 'classresult'})
        self.assertEqual(value['unavailable'], [])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_stream_keeps_private_blocks_and_requires_block_end(self):
        events = [
            {'type': 'message_start', 'message': {'id': 'native1', 'role': 'assistant', 'type': 'message', 'content': [], 'stop_reason': None, 'usage': {'input_tokens': 5, 'output_tokens': 0}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'private trace'}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'signed'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': '公开文字 sk-sec'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'ret 后续'}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'call1', 'name': 'conversation_answer', 'input': {}}},
            {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"content":'}},
            {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '"你好"}'}},
            {'type': 'content_block_stop', 'index': 2},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}},
            {'type': 'message_stop'},
        ]
        config = {'protocol': 'anthropic_messages', 'base_url': 'https://controller.example/v1', 'streaming': True}
        partial = []
        async def delta(text): partial.append(text)
        def transport(complete):
            async def endpoint(req):
                wire = ''.join('event: ' + item['type'] + '\ndata: ' + json.dumps(item, ensure_ascii=False) + '\n\n' for item in complete)
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=wire)
            return httpx.MockTransport(endpoint)
        result = await request(config, 'sk-secret', {}, 'm1', transport=transport(events), on_delta=delta)
        self.assertEqual(result['calls'][0]['arguments']['content'], '你好')
        self.assertEqual(result['blocks'][0]['thinking'], 'private trace')
        self.assertEqual(result['usage'], {'input_tokens': 5, 'output_tokens': 10})
        self.assertTrue(partial); self.assertNotIn('private trace', ''.join(partial)); self.assertNotIn('sk-secret', ''.join(partial))
        self.assertIn('[redacted]', partial[-1])
        without_stop = [event for event in events if not (event['type'] == 'content_block_stop' and event['index'] == 2)]
        with self.assertRaises(ProviderFault): await request(config, 'sk-secret', {}, 'm2', transport=transport(without_stop), on_delta=delta)

    async def test_split_tool_json_is_assembled_and_incomplete_stream_never_dispatches(self):
        events = []
        for delta in [{'content': '正在读取资料。'}, {'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': 'conversation_answer', 'arguments': '{"content":"'}}]}, {'tool_calls': [{'index': 0, 'function': {'arguments': '你好"}'}}]}]:
            events.append({'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})
        events.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})
        wire = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events)
        config = {'protocol': 'openai_chat_completions', 'base_url': 'https://controller.example/v1', 'streaming': True}
        partial = []
        async def delta(text): partial.append(text)
        async def endpoint(req): return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=wire + 'data: [DONE]\n\n')
        result = await request(config, 'secret', {}, 'm1', transport=httpx.MockTransport(endpoint), on_delta=delta)
        self.assertEqual(result['calls'][0]['arguments']['content'], '你好'); self.assertEqual(partial[-1], '正在读取资料。')
        async def truncated(req): return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=wire)
        with self.assertRaises(ProviderFault): await request(config, 'secret', {}, 'm2', transport=httpx.MockTransport(truncated), on_delta=delta)
