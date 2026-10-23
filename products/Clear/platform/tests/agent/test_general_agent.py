"""New general turns use real Harness and the existing operation services."""
from pathlib import Path
import json
import os
import time
import unittest

import test_data_tools as fixtures
import test_graph as graph_tests
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.general import CONTRACT

response = graph_tests.response


class GeneralAgentTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DataToolTests.setUp
    wait_status = graph_tests.GraphTests.wait_status

    def tearDown(self):
        if self._testMethodName.startswith('test_official_file'):
            self.native.close()
            self.temporary._finalizer.detach()
            evidence = Path(os.environ['TEMP']).parent / 'evidence'
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / ('native-file-fixture-' + str(time.time_ns()) + '.json')).write_text(
                json.dumps({'directory': self.temporary.name, 'test': self._testMethodName}), encoding='utf-8')
        else:
            fixtures.DataToolTests.tearDown(self)

    async def runtime(self, provider):
        runtime = AgentRuntime(Path(self.temporary.name) / 'general-agent', self.services, model_provider=provider)
        self.services.agent = runtime
        await runtime.start()
        self.assertTrue(runtime.agent_enabled, runtime.agent_unavailable_reason)
        session = runtime.store.create_session('wtest', 'General task', [], 'general-session')
        return runtime, session

    async def test_plain_text_finishes_once_without_answer_or_delivery_tool(self):
        observed = []
        async def provider(state, schemas):
            observed.append(state['step'])
            return response(state, content='2 + 3 = 5。')
        runtime, session = await self.runtime(provider)
        try:
            run = await runtime.submit(session['id'], '计算2+3', 'general-plain')
            await self.wait_status(runtime, run['id'], {'answered', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'answered')
            self.assertEqual(observed, [0])
            self.assertEqual(runtime.graph.load(run['id'])['execution_contract'], CONTRACT)
            self.assertEqual(runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],)), [])
            rows = runtime.store.rows("SELECT * FROM public_responses WHERE run_id=? AND status='confirmed'", (run['id'],))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['content'], '2 + 3 = 5。')
        finally:
            await runtime.close()

    async def test_runtime_context_snapshots_are_current_on_wire_and_complete_in_history(self):
        header = 'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.'
        user_text = header + '\n\nUser text must remain, even when it resembles a runtime snapshot.'
        seen = []
        async def provider(state, schemas):
            contents = [message.get('content', '') for message in state['messages'] if message['role'] == 'user']
            seen.append(sum(content.startswith(header) and content != user_text for content in contents))
            self.assertIn(user_text, contents)
            if state['step'] == 0:
                return response(state, 'read', {'file_path': 'source.txt'})
            if state['step'] == 1:
                return response(state, 'write', {'file_path': 'changed.md', 'content': '# Verified\n42\n'})
            if state['step'] == 2:
                return response(state, 'read', {'file_path': 'changed.md'})
            return response(state, content='已读取源文件并创建、读回结果42。')
        runtime, session = await self.runtime(provider)
        directory = runtime.store.root / 'workspaces' / session['id']
        directory.mkdir(parents=True)
        (directory / 'source.txt').write_text('42', encoding='utf-8')
        try:
            run = await runtime.submit(session['id'], user_text, 'runtime-snapshot-projection')
            await self.wait_status(runtime, run['id'], {'succeeded', 'answered', 'failed'})
            self.assertIn(runtime.store.run(run['id'])['status'], {'succeeded', 'answered'})
            self.assertEqual(seen, [1, 1, 1, 1])
            events = [json.loads(row['payload']) for row in runtime.store.rows(
                'SELECT payload FROM harness_events WHERE session_id=? ORDER BY seq', (session['id'],))]
            contexts = [event for event in events if event['type'] == 'user/message'
                        and event['data'].get('source', {}).get('kind') == 'runtime-context']
            self.assertGreaterEqual(len(contexts), 3)
            self.assertEqual((directory / 'changed.md').read_text('utf-8'), '# Verified\n42\n')
        finally:
            await runtime.close()

    def test_ocr_evidence_view_preserves_fixed_text_without_repeating_raw_engine_payload(self):
        from copy import deepcopy
        from workbench_platform.application_services.operations.audit import evidence_data
        value = {'reference': {'application': 'ocr', 'object_id': 'fixed-result', 'revision': 3},
            'snapshot_hash': 'a' * 64, 'location_url': '/fixed-evidence', 'source_assets': [{'asset_id': 'source'}],
            'observation': {'locator': {'page_number': 2}, 'snapshot': {'id': 'snapshot', 'revision': 3,
                'available': True, 'original_sha256': 'b' * 64,
                'edited': {'text': '编号000731，金额100.20'},
                'original': {'character_boxes': [[1, 2, 3, 4]] * 2000, 'engine': 'ppocr'}}}}
        before = deepcopy(value)
        view = evidence_data(value)
        self.assertEqual(value, before)
        self.assertEqual(view['snapshot_hash'], before['snapshot_hash'])
        self.assertEqual(view['observation']['snapshot']['edited']['text'], '编号000731，金额100.20')
        self.assertTrue(view['observation']['snapshot']['edited']['text_complete'])
        self.assertEqual(view['text_readback']['reference'], before['reference'])
        self.assertTrue(view['observation_preview_only'])
        self.assertLess(len(json.dumps(view)), 2000)

    async def test_product_read_context_checks_scope_without_requiring_a_running_agent(self):
        from workbench_platform.application_services.context import SessionContext
        from workbench_platform.agent.store import RunSuperseded
        runtime, session = await self.runtime(lambda *args: None)
        try:
            ctx = SessionContext('wtest', session['id'], '', 0, 0, '', (), self.services, runtime.store,
                grants=frozenset({'read'}))
            ctx.current()
            with self.assertRaises(ValueError):
                ctx.require_write()
            from dataclasses import replace
            with self.assertRaises(RunSuperseded):
                replace(ctx, workspace_id='wother').current()
            with self.assertRaises(ValueError):
                replace(ctx, run_id='nonexistent').current()
        finally:
            await runtime.close()

    async def test_one_search_loads_tool_and_native_action_can_end_with_plain_text(self):
        observed = []
        async def provider(state, schemas):
            observed.append(set(schemas))
            if state['step'] == 0:
                self.assertIn('tools_search', schemas)
                self.assertNotIn('data_create_session', schemas)
                return response(state, 'tools_search', {'query': 'data_create_session', 'limit': 1})
            if state['step'] == 1:
                self.assertIn('data_create_session', schemas)
                return response(state, 'data_create_session', {'name': '原生数据工作'})
            return response(state, content='数据作业已创建。')
        runtime, session = await self.runtime(provider)
        try:
            run = await runtime.submit(session['id'], '为我建立一个数据作业', 'general-search')
            await self.wait_status(runtime, run['id'], {'succeeded', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'succeeded')
            self.assertEqual(len(observed), 3)
            self.assertEqual(len(self.native.list('wtest')), 1)
            operations = runtime.store.rows('SELECT tool FROM operations WHERE run_id=? ORDER BY created', (run['id'],))
            self.assertEqual([row['tool'] for row in operations], ['tools_search', 'data_create_session'])
        finally:
            await runtime.close()

    async def test_natural_completion_surfaces_required_review_and_preserves_rejection(self):
        from dataclasses import replace
        from workbench_platform.application_services.decisions import ask_user, AskUser
        from workbench_platform.agent.registry import error_result
        async def provider(state, schemas):
            if state['step'] == 0:
                return response(state, 'write', {'file_path': 'review-draft.txt', 'content': '待真实用户复核的草稿'})
            if state['step'] == 1:
                return response(state, content='草稿已生成，等待固定版本复核。')
            self.assertIn('不确认', json.dumps(state['messages'], ensure_ascii=False))
            return response(state, content='用户未确认该版本，文件保留草稿，正式交付仍待复核。')
        runtime, session = await self.runtime(provider)
        def review_service(args, ctx):
            replies = ctx.store.rows('SELECT reply FROM decisions WHERE run_id=? AND reply IS NOT NULL', (ctx.run_id,))
            if replies:
                self.assertEqual(replies[0]['reply'], '不确认')
                return error_result('review_required', '用户未确认此版本；草稿保留，不能作为核准成果')
            result = ask_user(AskUser(question='请确认固定版本fixed-v1', options=['确认此版本', '不确认']), ctx)
            result['data']['action'] = {'action': 'review', 'snapshot_id': 'fixed-v1'}
            return result
        runtime.registry.tools['delivery_publish'] = replace(runtime.registry.tools['delivery_publish'], execute=review_service)
        runtime.tasks.update(session['id'], {'request_id': 'review-policy',
            'expected_revision': runtime.tasks.get(session['id'])['revision'], 'delivery_policy': 'reviewed'})
        try:
            run = await runtime.submit(session['id'], '生成待人工复核的正式成果', 'natural-review')
            await self.wait_status(runtime, run['id'], {'waiting_user', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'waiting_user')
            decision = runtime.store.rows('SELECT * FROM decisions WHERE run_id=?', (run['id'],))[0]
            question = runtime.store.rows("SELECT payload FROM events WHERE run_id=? AND kind='question'", (run['id'],))
            self.assertTrue(question)
            await runtime.reply(decision['id'], '不确认', 1, 'actual-rejection')
            await self.wait_status(runtime, run['id'], {'paused', 'failed', 'succeeded'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'paused', runtime.store.run(run['id']))
            final = runtime.store.rows("SELECT content FROM public_responses WHERE run_id=? AND status='confirmed' ORDER BY rowid", (run['id'],))
            self.assertIn('未确认', final[-1]['content'])
            self.assertFalse(runtime.graph.load(run['id']).get('delivery', {}).get('data', {}).get('verified'))
        finally:
            await runtime.close()

    async def test_native_read_preview_converges_without_recursive_receipt_wrapping(self):
        from unittest.mock import patch
        from workbench_platform.agent.context import TokenEstimator
        original_count = TokenEstimator.count
        def pressure_for_native_body(estimator, text):
            serialized = json.dumps(text)
            return 100000 if serialized.count('NATIVE_BODY') > 100 and 'context_preview_origin' not in serialized else original_count(estimator, text)
        async def provider(state, schemas):
            purpose = runtime.store.rows('SELECT purpose FROM harness_model_calls WHERE request_id=?', (state['pending_model_request'],))[0]['purpose']
            if purpose == 'compaction':
                return response(state, content=json.dumps({'goal': '读取原文件', 'constraints': ['保留原文来源'],
                    'plan': [], 'completed': ['已读取文件预览'], 'pending_jobs': [], 'artifacts': [],
                    'next_step': '说明来源', 'references': []}))
            if state['step'] == 0:
                return response(state, 'read', {'file_path': 'large-native.txt'})
            result = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            self.assertEqual(result['status'], 'success')
            self.assertIn('context_preview_origin', result['data'])
            self.assertNotIn('value', result['data'])
            self.assertLess(len(json.dumps(result)), 4000)
            return response(state, content='已读取原文预览，完整回执可按来源读取。')
        runtime, session = await self.runtime(provider)
        observations = []
        prepare = runtime.harness_prepare
        async def observed_prepare(state, params):
            result = await prepare(state, params)
            observations.append({'messages_body_count': json.dumps(params['messages']).count('NATIVE_BODY'),
                'incoming_body_count': json.dumps(params.get('incoming')).count('NATIVE_BODY'),
                'keys': sorted(result), 'pressure': result.get('metrics', {}).get('pressure_ratio'),
                'facts_body_count': (result.get('facts') or '').count('NATIVE_BODY')})
            return result
        runtime.harness_prepare = observed_prepare
        directory = runtime.store.root / 'workspaces' / session['id']
        directory.mkdir(parents=True)
        (directory / 'large-native.txt').write_text('NATIVE_BODY ' * 1000, encoding='utf-8')
        try:
            with patch.object(TokenEstimator, 'count', pressure_for_native_body):
                run = await runtime.submit(session['id'], '读取large-native.txt并说明来源', 'native-preview')
                await self.wait_status(runtime, run['id'], {'answered', 'failed', 'paused', 'needs_verification'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'answered', [runtime.store.run(run['id']), observations])
            self.assertEqual(runtime.store.run(run['id'])['rounds'], 2)
            history = runtime.store.rows("SELECT payload FROM harness_events WHERE session_id=? ORDER BY seq", (session['id'],))
            replacements = [json.loads(row['payload']) for row in history if isinstance(json.loads(row['payload']).get('surfaceOp'), dict)]
            self.assertGreaterEqual(len(replacements), 1)
            self.assertLessEqual(len(replacements), 4)
            record = runtime.store.rows("SELECT result FROM operations WHERE run_id=? AND tool='read'", (run['id'],))[0]
            self.assertGreater(json.dumps(json.loads(record['result'])).count('NATIVE_BODY'), 60)
        finally:
            await runtime.close()

    async def test_partial_outputs_continue_in_same_harness_run_without_delivery_tool(self):
        from dataclasses import replace
        from unittest.mock import Mock
        seen = []
        async def provider(state, schemas):
            seen.append(state['run_id'])
            if state['step'] == 0:
                return response(state, 'write', {'file_path': 'completed.txt', 'content': '已成功部分42'})
            if state['step'] in {1, 3}:
                return response(state, 'workspace_get_context', {})
            if state['step'] == 2:
                return response(state, content='已保存可用部分，继续补齐失败项。')
            self.assertIn('只补齐', json.dumps(state['messages'], ensure_ascii=False))
            return response(state, content='失败项已补齐。')
        runtime, session = await self.runtime(provider)
        calls = []
        def execute(args, ctx):
            calls.append(ctx.run_id)
            return {'status': 'success', 'summary': 'receipt', 'job_refs': [], 'result_refs': [], 'artifact_refs': [],
                    'data': {'failures': [{'asset_id': 'missing', 'message': 'temporary'}]} if len(calls) == 1 else {'completed_asset_ids': ['missing']}}
        runtime.registry.tools['workspace_get_context'] = replace(runtime.registry.tools['workspace_get_context'], execute=execute)
        try:
            run = await runtime.submit(session['id'], '处理资料并补齐可恢复失败', 'general-partial-repair')
            await self.wait_status(runtime, run['id'], {'answered', 'succeeded', 'paused', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'succeeded', runtime.store.run(run['id']))
            self.assertEqual(seen, [run['id']] * 5)
            published = runtime.store.rows("SELECT result FROM operations WHERE tool='workspace_publish_outputs'")
            self.assertEqual(len(published), 1)
            self.assertTrue(json.loads(published[0]['result'])['artifact_refs'])
            self.assertEqual(calls, [run['id']] * 2)
            self.assertEqual(runtime.store.rows("SELECT id FROM operations WHERE tool='delivery_publish'"), [])
        finally:
            await runtime.close()

    async def test_legacy_state_keeps_explicit_answer_contract(self):
        async def provider(*_):
            self.fail('Pure legacy completion probe must not call a model')
        runtime, session = await self.runtime(provider)
        try:
            run, _ = runtime.store.start_run(session['id'], 'legacy turn', 'legacy-general-probe', 0)
            state = runtime.initial_state(run, 'legacy turn')
            self.assertIsNone(state['execution_contract'])
            state['final_response'] = {'request_id': 'legacy-text', 'content': 'raw text'}
            self.assertFalse(await runtime.harness_finalize(state))
            self.assertEqual(runtime.store.run(run['id'])['status'], 'running')
        finally:
            await runtime.close()

    async def test_official_file_and_powershell_tools_execute_python_and_confine_writes(self):
        async def provider(state, schemas):
            for name in ('read', 'write', 'edit', 'pwsh', 'glob', 'grep'):
                self.assertIn(name, schemas)
            step = state['step']
            if step == 0:
                code = (
                    'from pathlib import Path\n'
                    'outside = Path.cwd().parent / "outside-denied.txt"\n'
                    'try:\n    outside.write_text("must not be written", encoding="utf-8")\n'
                    'except PermissionError:\n    print("outside denied")\n'
                    'else:\n    raise RuntimeError("sandbox did not confine writes")\n'
                    'Path("computed.csv").write_text("编号,金额\\n000731,100.20\\n", encoding="utf-8")\n'
                    'print(6 * 7)\n'
                )
                return response(state, 'write', {'file_path': 'calc.py', 'content': code})
            previous = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            self.assertEqual(previous['status'], 'success', previous)
            if step == 1:
                return response(state, 'pwsh', {'description': '执行本地计算并验证目录写入边界',
                    'command': '& "' + state['python_executable'] + '" -B calc.py'})
            if step == 2:
                self.assertIn('outside denied', previous['data']['text'])
                self.assertIn('42', previous['data']['text'])
                return response(state, 'read', {'file_path': 'computed.csv'})
            self.assertIn('000731,100.20', previous['data']['text'])
            return response(state, content='计算结果42，CSV已生成并读回核对。[下载CSV](sandbox:/computed.csv)')
        runtime, session = await self.runtime(provider)
        try:
            run = await runtime.submit(session['id'], '创建脚本并计算，保留编号金额原文，验证不能写到工作目录之外', 'general-python')
            await self.wait_status(runtime, run['id'], {'succeeded', 'failed', 'paused'})
            final = runtime.store.run(run['id'])
            self.assertEqual(final['status'], 'succeeded', final)
            state = runtime.graph.load(run['id'])
            directory = Path(state['working_directory'])
            self.assertEqual((directory / 'computed.csv').read_text(encoding='utf-8'), '编号,金额\n000731,100.20\n')
            self.assertFalse((directory.parent / 'outside-denied.txt').exists())
            operations = runtime.store.rows('SELECT tool,state,result FROM operations WHERE run_id=? ORDER BY created', (run['id'],))
            self.assertEqual([row['tool'] for row in operations],
                             ['write', 'pwsh', 'read', 'workspace_publish_outputs'])
            published = json.loads(next(row['result'] for row in operations if row['tool'] == 'workspace_publish_outputs'))
            self.assertEqual({item['path'] for item in published['data']['files']}, {'calc.py', 'computed.csv'})
            bundle_id = published['artifact_refs'][0]['object_id']
            final_text = runtime.store.rows("SELECT content FROM public_responses WHERE run_id=? AND status='confirmed'", (run['id'],))[0]['content']
            self.assertIn('/api/platform/v1/workspaces/wtest/artifacts/' + bundle_id + '/files/computed.csv', final_text)
            self.assertNotIn('sandbox:/', final_text)
            self.assertEqual({item['name'] for item in self.center.get('wtest', bundle_id)['files']},
                             {'calc.py', 'computed.csv', 'clear-output-files.json'})
            artifact = self.center.file('wtest', bundle_id, 'computed.csv')
            with self.center.store.stage_download('wtest', artifact.artifact_id, expected_revision=artifact.revision,
                                                  expected_hash=artifact.blob_hash) as downloaded:
                self.assertEqual(downloaded.read_bytes(), (directory / 'computed.csv').read_bytes())
            observed = []
            async def followup(state, schemas):
                observed.append(state['step'])
                return response(state, content='文件已保留，普通说明不需要重新发布。')
            runtime.model_provider = followup
            next_run = await runtime.submit(session['id'], '解释一下刚才的结果', 'general-followup')
            await self.wait_status(runtime, next_run['id'], {'answered', 'failed'})
            self.assertEqual(runtime.store.run(next_run['id'])['status'], 'answered')
            self.assertEqual(observed, [0])
            self.assertEqual(runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (next_run['id'],)), [])
        finally:
            await runtime.close()

    async def test_registered_native_skill_can_read_and_execute_its_script(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                self.assertIn('skill', schemas)
                return response(state, 'skill', {'name': 'double'})
            previous = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            self.assertEqual(previous['status'], 'success', previous)
            if state['step'] == 1:
                self.assertIn('multiply.py', json.dumps(previous, ensure_ascii=False))
                return response(state, 'pwsh', {'description': '运行登记技能脚本', 'command': '& "' +
                    state['python_executable'] + '" -B "' + str(script) + '" 21'})
            self.assertIn('42', previous['data']['text'])
            return response(state, content='已运行技能脚本，结果42。')
        runtime, session = await self.runtime(provider)
        directory = runtime.store.root / 'workspaces' / session['id'] / '.clear' / 'registered-skills'
        skill = directory / 'double'
        (skill / 'scripts').mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: double\ndescription: 使用随附脚本将整数乘二\n---\n'
            '运行 scripts/multiply.py，传入用户要求的整数。\n', encoding='utf-8')
        script = skill / 'scripts/multiply.py'
        script.write_text('import sys\nprint(int(sys.argv[1]) * 2)\n', encoding='utf-8')
        runtime.skills.roots = (directory,)
        try:
            run = await runtime.submit(session['id'], '用double技能把21乘二', 'general-skill')
            await self.wait_status(runtime, run['id'], {'succeeded', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'succeeded', runtime.store.run(run['id']))
            self.assertEqual(runtime.store.run(run['id'])['rounds'], 3)
        finally:
            await runtime.close()
