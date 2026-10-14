"""Exercise the production HTTP serialization, graph and native data workers."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import httpx
import test_data_tools as data_tests
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.providers import request as provider_request
from workbench_platform.agent.context import core_tools
from workbench_platform.agent.providers import ProviderFault
from workbench_platform.agent.skills import SkillService
from workbench_platform.agent.store import Conflict, RunSuperseded


class WireContextTests(unittest.IsolatedAsyncioTestCase):
    setUp = data_tests.DataToolTests.setUp
    tearDown = data_tests.DataToolTests.tearDown
    asset = data_tests.DataToolTests.asset

    async def execute(self, choose, goal):
        runtime = AgentRuntime(Path(self.temporary.name) / 'wire-agent', self.services)
        self.services.agent = runtime
        config = {'model': 'gpt-6.1-sol', 'protocol': 'openai_chat_completions',
                  'base_url': 'http://127.0.0.1:19999/v1', 'revision': 1,
                  'context_cap': 16384, 'max_output_tokens': 2048, 'streaming': False}
        runtime.connection = SimpleNamespace(view=lambda: {'available': True, 'revision': 1},
            resolve=lambda revision: (config, 'local-test-secret'), assert_current=lambda revision: None)
        wires = []
        def transport(req):
            wire = json.loads(req.content)
            wires.append(wire)
            tool, arguments = choose(len(wires) - 1, wire)
            message = {'role': 'assistant', 'content': '完成' if tool is None else None}
            if tool:
                batch = tool if isinstance(tool, list) else [(tool, arguments)]
                message['tool_calls'] = [{'id': 'call_' + str(len(wires)) + '_' + str(index), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
                    for index, (name, args) in enumerate(batch)]
            return httpx.Response(200, json={'model': 'returned-local-test-model',
                'choices': [{'finish_reason': 'tool_calls' if tool else 'stop', 'message': message}],
                'usage': {'prompt_tokens': 42, 'completion_tokens': 10}})
        async def send(config, secret, payload, identity, **kwargs):
            return await provider_request(config, secret, payload, identity,
                transport=httpx.MockTransport(transport), **kwargs)
        await runtime.start()
        session = runtime.store.create_session('wtest', 'Wire test', [], 'wire-session')
        try:
            with patch('workbench_platform.agent.runtime.request', send):
                run = await runtime.submit(session['id'], goal, 'wire-message')
                for _ in range(200):
                    final = runtime.store.run(run['id'])
                    if final['status'] not in {'running', 'waiting_job'}:
                        break
                    await asyncio.sleep(.1)
                self.assertIn(final['status'], {'answered', 'succeeded'}, runtime.store.events(session['id']))
            # Streaming and activity add events; read the complete fixture
            # rather than assuming this multi-step run fits one 50-row page.
            events = runtime.store.events(session['id'], limit=10000)['items']
            requests = [e['payload'] for e in events if e['kind'] == 'model_request']
            self.assertEqual(len(requests), len(wires))
            for wire, metadata in zip(wires, requests, strict=True):
                metrics = metadata['context_metrics']
                self.assertEqual(metrics['payload_bytes'], len(json.dumps(wire, ensure_ascii=False,
                    separators=(',', ':')).encode('utf-8')))
                self.assertLessEqual(metrics['input_tokens_estimate'] + metrics['output_reserve'], 16384)
                self.assertGreater(metrics['component_tokens']['environment'], 0)
                self.assertGreater(metrics['component_tokens']['skills'], 0)
                self.assertGreater(metrics['component_tokens']['facts'], 0)
                self.assertNotIn('local-test-secret', json.dumps(metadata))
                system = wire['messages'][0]['content']
                self.assertIn('gpt-6.1-sol', system)
                self.assertIn('Windows', system)
                self.assertIn(session['id'], system)
                self.assertIn('skill', system)
                self.assertEqual(metadata['injection']['tools'], metrics['selected_tools'])
            usages = [e['payload'] for e in events if e['kind'] == 'usage']
            self.assertEqual(usages[-1]['returned_model'], 'returned-local-test-model')
            self.assertEqual(usages[-1]['configured_model'], 'gpt-6.1-sol')
            self.assertEqual(usages[-1]['input_tokens'], 42)
            evidence = data_tests.RUN / 'evidence' / ('wire-' + self._testMethodName + '.json')
            evidence.write_text(json.dumps({'real_external_model': False,
                'transport': 'httpx.MockTransport', 'requests': requests, 'wires': wires,
                'final_status': final['status']}, ensure_ascii=False, indent=2), encoding='utf-8')
            return wires, requests
        finally:
            await runtime.close()

    async def test_first_short_chat_uses_small_core_and_trusted_environment(self):
        wires, requests = await self.execute(lambda step, wire:
            ('conversation_answer', {'content': '你好，我可以调用平台登记的工具帮助完成审计任务。'}),
            '你好，你是什么模型？有什么工具？')
        self.assertEqual(len(wires), 1)
        names = [t['function']['name'] for t in wires[0]['tools']]
        self.assertEqual(set(names), set(core_tools(self.services.agent.registry)))
        self.assertNotIn('data_import', names)

    async def test_paginated_discovery_explicit_load_real_import_read_and_delivery(self):
        asset = self.asset('suppliers-left.csv')
        def choose(step, wire):
            names = [t['function']['name'] for t in wire['tools']]
            result = json.loads(wire['messages'][-1]['content']) if step else {}
            if step == 0:
                return 'tools_list', {'family': 'data', 'limit': 2}
            if step == 1:
                self.assertNotIn('data_import', names)
                self.assertEqual(len(result['data']['tools']), 2)
                return 'tools_describe', {'names': ['data_create_session', 'data_import', 'data_read_table']}
            if step == 2:
                self.assertIn('data_import', names)
                self.assertNotIn('input_schema', json.dumps(result))
                return 'data_create_session', {'name': '真实工具接续'}
            if step == 3:
                return 'data_import', {'data_session_id': result['data']['data_session_id'],
                    'expected_version': result['data']['version'], 'side': 'left', 'asset_id': asset.asset_id,
                    'asset_revision': asset.revision, 'blob_hash': asset.blob_hash}
            if step == 4:
                self.assertEqual(result['status'], 'success')
                return 'data_read_table', {'data_session_id': result['data']['data_session_id'], 'side': 'left'}
            if step == 5:
                self.assertEqual(result['data']['rows'][0]['cells'][0]['value'], '000001')
                return 'delivery_publish', {'summary': '台账导入后已读取实际内容', 'result_refs': result['result_refs']}
            return None, {}
        wires, requests = await self.execute(choose, '调用已有工具导入台账并交付读取结果')
        self.assertEqual(len(wires), 7)
        self.assertEqual(len(self.native.list('wtest')), 1)
        self.assertEqual(len(self.native.get(self.native.list('wtest')[0]['id'])['uploads']), 1)

    async def test_agent_activates_skill_and_same_batch_cannot_escape_allowed_tools(self):
        def choose(step, wire):
            if step == 0:
                self.assertNotIn('<registered_skill', wire['messages'][0]['content'])
                return 'tools_describe', {'names': ['jobs_get']}
            if step == 1:
                return [('skills_activate', {'name': 'payment-reconciliation'}),
                        ('jobs_get', {'job_id': 'not-authorized-by-skill'})], {}
            self.assertIn('<registered_skill name="payment-reconciliation"', wire['messages'][0]['content'])
            self.assertNotIn('jobs_get', [t['function']['name'] for t in wire['tools']])
            result = json.loads(wire['messages'][-1]['content'])
            self.assertEqual(result['error']['code'], 'skill_tool_denied')
            return 'conversation_answer', {'content': '已启用付款核对技能，可继续调用允许的数据工具。'}
        wires, requests = await self.execute(choose, '使用付款核对技能')
        self.assertEqual(len(wires), 3)
        self.assertEqual(requests[-1]['injection']['skills'][0]['name'], 'payment-reconciliation')
        self.assertGreater(requests[-1]['context_metrics']['component_tokens']['skills'],
                           requests[0]['context_metrics']['component_tokens']['skills'])

    async def test_full_current_rules_goal_and_plan_survive_and_injected_provider_is_budgeted(self):
        calls = []
        async def provider(state, schemas):
            calls.append(state)
        runtime = AgentRuntime(Path(self.temporary.name) / 'rules-agent', self.services, model_provider=provider)
        self.services.agent = runtime
        session = runtime.store.create_session('wtest', 'Constraints', [], 'constraints-session')
        rules = '保持完整的用户规则；' * 400
        goal = '完整的当前工作目标；' * 300
        runtime.tasks.save_profile('wtest', {'request_id': 'profile', 'expected_revision': 0,
            'audit_object': '合成项目', 'period_start': '', 'period_end': '', 'objective': goal, 'rules': rules})
        runtime.tasks.record_goal(session['id'], goal)
        run, _ = runtime.store.start_run(session['id'], goal, 'constraints-run', 0)
        facts = runtime.session_facts(run)
        self.assertEqual(facts['audit_context']['rules'], rules)
        self.assertEqual(facts['audit_context']['objective'], goal)
        self.assertEqual(facts['task']['goal'], goal)
        self.assertIn('plan', facts['task'])
        state = runtime.initial_state(run, '超大输入' * 10000)
        with self.assertRaises(ProviderFault) as caught:
            await runtime.model(state)
        self.assertEqual(caught.exception.code, 'context_limit')
        self.assertIsNotNone(caught.exception.context_metrics)
        self.assertEqual(calls, [])
        self.assertEqual(runtime.store.rows('SELECT * FROM model_requests'), [])
        self.assertEqual(runtime.tasks.profile('wtest')['rules'], rules)

    async def test_history_reads_complete_original_after_restart_and_rejects_other_session(self):
        root = Path(self.temporary.name) / 'history-agent'
        runtime = AgentRuntime(root, self.services)
        self.services.agent = runtime
        session = runtime.store.create_session('wtest', 'History', [], 'history-session')
        other = runtime.store.create_session('wother', 'Other', [], 'other-history-session')
        content = '必须保留每一项原始用户条件。' * 700
        runtime.store.event(session['id'], None, 'history-original', 'message', {'role': 'user', 'content': content})
        runtime.store.event(other['id'], None, 'other-history-original', 'message', {'role': 'user', 'content': '其他项目机密'})
        seq = runtime.store.rows('SELECT seq FROM events WHERE identity=?', ('history-original',))[0]['seq']
        foreign = runtime.store.rows('SELECT seq FROM events WHERE identity=?', ('other-history-original',))[0]['seq']
        run, _ = runtime.store.start_run(session['id'], '读取旧规则', 'history-run', 0)
        reopened = AgentRuntime(root, self.services)
        self.services.agent = reopened
        ctx = reopened.context(reopened.initial_state(run, '读取旧规则'))
        async def read(call_id, args):
            return await reopened.registry.dispatch({'call_id': call_id, 'tool': 'session_read_history', 'arguments': args}, ctx)
        catalog = await read('catalog', {'limit': 1})
        self.assertEqual(catalog['data']['items'][0]['event_seq'], seq)
        self.assertTrue(catalog['data']['items'][0]['preview_only'])
        self.assertEqual(catalog['data']['next_after'], seq)
        pages, offset, hashes = [], 0, []
        while True:
            page = await read('page' + str(offset), {'event_seq': seq, 'offset': offset, 'chars': 1700})
            self.assertEqual(page['status'], 'success')
            pages.append(page['data']['content'])
            hashes.append(page['data']['content_sha256'])
            offset = page['data']['next_offset']
            if offset is None:
                break
        self.assertEqual(''.join(pages), content)
        self.assertEqual(len(set(hashes)), 1)
        denied = await read('foreign', {'event_seq': foreign})
        self.assertEqual(denied['status'], 'error')
        self.assertNotIn('其他项目机密', json.dumps(denied, ensure_ascii=False))

    async def test_received_reply_recovers_fixed_input_after_skill_source_changes(self):
        root = Path(self.temporary.name) / 'reply-recovery'
        registered = root / 'skills'
        source = Path(__file__).resolve().parents[2] / 'config/skills'
        shutil.copytree(source, registered)
        calls = []
        async def provider(state, schemas):
            calls.append(state['step'])
            return {'id': 'fixed-reply', 'role': 'assistant', 'content': '',
                    'calls': [{'call_id': 'fixed-call', 'tool': 'conversation_answer',
                               'arguments': {'content': '固定的原回复'}}],
                    'usage': {'input_tokens': 12, 'output_tokens': 4}}
        def reopen():
            runtime = AgentRuntime(root / 'agent', self.services, model_provider=provider)
            runtime.skills = SkillService(runtime.store, roots=[registered], registry=runtime.registry)
            self.services.agent = runtime
            return runtime
        runtime = reopen()
        session = runtime.store.create_session('wtest', 'Recovery', [], 'fixed-recovery-session')
        fixed = runtime.skills.activate('wtest', session['id'], 'payment-reconciliation', 'activate-fixed')
        run, _ = runtime.store.start_run(session['id'], '保持固定技能', 'fixed-recovery-run', 0)
        state = runtime.initial_state(run, '保持固定技能')
        first = await runtime.model(state)
        skill = registered / 'payment-reconciliation/SKILL.md'
        skill.write_text(skill.read_text(encoding='utf-8').replace('description:', 'description: 修改源目录描述，') +
                         '\n这段新正文未经升级，不应替换固定正文。\n', encoding='utf-8')
        recovered = reopen()
        self.assertTrue(recovered.skills.active_manifest(session['id'])[0]['source_changed'])
        self.assertEqual(await recovered.model(deepcopy(state)), first)
        self.assertEqual(calls, [0])
        self.assertEqual(len(recovered.store.rows('SELECT * FROM model_requests')), 1)
        self.assertEqual(len(recovered.store.rows("SELECT * FROM events WHERE kind='model_request'")), 1)
        self.assertEqual(first['selected_tools'], (await recovered.model(state))['selected_tools'])
        for field, value in [('messages', [*state['messages'], {'role': 'user', 'content': '新的目标'}]),
                             ('visible', [*state['visible'], 'jobs_get']),
                             ('protected_tools', ['jobs_get'])]:
            with self.subTest(field=field), self.assertRaises(Conflict):
                await recovered.model({**deepcopy(state), field: value})
        original = recovered.registry.tools['conversation_answer']
        recovered.registry.tools['conversation_answer'] = replace(original, version='changed-contract')
        with self.assertRaises(Conflict):
            await recovered.model(state)
        recovered.registry.tools['conversation_answer'] = original
        catalog = recovered.skills.catalog('wtest', session['id'])
        current = next(item for item in catalog['items'] if item['name'] == 'payment-reconciliation')
        self.assertNotEqual(current['version'], fixed['skill']['active_version'])
        recovered.skills.activate('wtest', session['id'], 'payment-reconciliation', 'upgrade-fixed',
                                 version=current['version'], context=recovered.context(state))
        with self.assertRaises(Conflict):
            await recovered.model(state)
        recovered.cancel(run['id'])
        with self.assertRaises(RunSuperseded):
            await recovered.model(state)
        self.assertEqual(calls, [0])

    async def test_unknown_reply_recovery_does_not_resend_or_accept_changed_input(self):
        root = Path(self.temporary.name) / 'unknown-recovery'
        calls = []
        async def provider(state, schemas):
            calls.append(state['step'])
            raise ProviderFault('response_unknown', '合成回包丢失')
        runtime = AgentRuntime(root, self.services, model_provider=provider)
        self.services.agent = runtime
        session = runtime.store.create_session('wtest', 'Unknown', [], 'unknown-session')
        run, _ = runtime.store.start_run(session['id'], '原目标', 'unknown-run', 0)
        state = runtime.initial_state(run, '原目标')
        with self.assertRaises(ProviderFault):
            await runtime.model(state)
        reopened = AgentRuntime(root, self.services, model_provider=provider)
        self.services.agent = reopened
        with self.assertRaises(ProviderFault) as caught:
            await reopened.model(state)
        self.assertEqual(caught.exception.code, 'response_unknown')
        with self.assertRaises(Conflict):
            await reopened.model({**state, 'messages': [{'role': 'user', 'content': '另一目标'}]})
        self.assertEqual(calls, [0])
        self.assertEqual(len(reopened.store.rows('SELECT * FROM model_requests')), 1)


if __name__ == '__main__':
    unittest.main()
