"""Real Harness and HTTP serialization retain candidates through preparation."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import httpx
import test_harness_delivery_checks as fixtures
from workbench_platform.agent.context import TokenEstimator
from workbench_platform.agent.providers import request as provider_request
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.tools.context import Empty


EVIDENCE = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence')
BUSINESS = 'retention_inspect'


class HarnessToolRetentionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DeliveryChecksTests.setUp
    tearDown = fixtures.DeliveryChecksTests.tearDown

    async def scenario(self, *, newly_described):
        runtime = AgentRuntime(self.root / 'retention-agent', self.services)
        self.services.agent = runtime
        config = {'protocol': 'openai_chat_completions', 'model': 'engineering-retention', 'revision': 1,
                  'base_url': 'http://127.0.0.1:19999/v1', 'context_cap': 65536,
                  'max_output_tokens': 2048, 'streaming': False}
        runtime.connection = SimpleNamespace(view=lambda: {'available': True, 'revision': 1},
            resolve=lambda revision: (config, 'local-retention-secret'), assert_current=lambda revision: None)
        executions, conversations, summaries = [], [], []
        raw_wires, preparations = [], []
        def inspect(args, ctx):
            executions.append(ctx.operation_id)
            return {'status': 'success', 'summary': 'Read the registered business source',
                    'data': {'opaque_source': '证' * 20000 if not newly_described and len(executions) == 1 else 'small verified source'}}
        runtime.registry.register(ToolSpec(BUSINESS, 'Inspect registered evidence ' * 80, Empty, inspect,
                                           effect='read', idempotent=True))
        initial_state = runtime.initial_state
        def seeded(run, content):
            state = initial_state(run, content)
            state['messages'].insert(1, {'id': run['id'] + ':original-source', 'role': 'user',
                'content': '[Historical source data; keep the task and source identity]\n' + '史' * 12000})
            return state
        runtime.initial_state = seeded
        prepare = runtime.harness_prepare
        async def record_prepare(state, params):
            before = list(state.get('protected_tools', []))
            result = await prepare(state, params)
            preparations.append({'step': state['step'], 'before': before,
                'after': list(state.get('protected_tools', [])), 'compact': result.get('compact', False),
                'selected': [tool['name'] for tool in result.get('tools', [])]})
            return result
        runtime.harness_prepare = record_prepare
        dispatch = runtime.harness_tool
        async def compact_new_activation(state, call):
            result = await dispatch(state, call)
            if newly_described and call['tool'] == 'tools_describe':
                # This is the same durable flag as the public manual-compaction
                # endpoint. No budget or preparation logic is mocked.
                state['compact_requested'] = True
                runtime.graph.save(state)
            return result
        runtime.harness_tool = compact_new_activation
        checkpoint = {'goal': 'Read the registered evidence without rediscovering its schema',
            'constraints': ['Keep task and source identities'], 'plan': ['Read evidence and answer'],
            'completed': [], 'pending_jobs': [], 'artifacts': [], 'next_step': 'Use retention_inspect', 'references': []}
        def transport(request):
            wire = json.loads(request.content)
            raw_wires.append(request.content)
            names = [tool['function']['name'] for tool in wire.get('tools', [])]
            if not names:
                summaries.append(wire)
                message = {'role': 'assistant', 'content': json.dumps(checkpoint)}
                reason = 'stop'
            else:
                index = len(conversations)
                conversations.append(wire)
                tool = 'tools_describe' if index == 0 else BUSINESS if index < (2 if newly_described else 3) else 'conversation_answer'
                if index:
                    self.assertIn(BUSINESS, names, 'A compacted history must not permanently erase a still-fitting tool candidate')
                arguments = {'names': [BUSINESS]} if tool == 'tools_describe' else {'content': 'Evidence read and source retained'} if tool == 'conversation_answer' else {}
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'retention_call_' + str(index),
                    'type': 'function', 'function': {'name': tool, 'arguments': json.dumps(arguments)}}]}
                reason = 'tool_calls'
            return httpx.Response(200, json={'choices': [{'finish_reason': reason, 'message': message}],
                                           'usage': {'prompt_tokens': 10, 'completion_tokens': 10}})
        async def send(config, secret, payload, identity, **kwargs):
            return await provider_request(config, secret, payload, identity,
                transport=httpx.MockTransport(transport), **kwargs)
        await runtime.start()
        session = runtime.store.create_session('wtest', 'Retention wire regression', [], 'retention-session')
        try:
            with patch('workbench_platform.agent.harness_service.request', send):
                run = await runtime.submit(session['id'], 'Read the existing registered business source, then answer.', 'retention-message')
                async with asyncio.timeout(120):
                    while runtime.store.run(run['id'])['status'] in {'running', 'waiting_job', 'waiting_user'}:
                        await asyncio.sleep(.1)
                self.assertEqual(runtime.store.run(run['id'])['status'], 'answered', runtime.store.run(run['id']))
            self.assertEqual(len(conversations), 3 if newly_described else 4)
            self.assertGreaterEqual(len(summaries), 1)
            operations = runtime.store.rows('SELECT tool FROM operations WHERE run_id=? ORDER BY created', (run['id'],))
            self.assertEqual([row['tool'] for row in operations].count('tools_describe'), 1)
            self.assertEqual(len(executions), 1 if newly_described else 2)
            if newly_described:
                passes = [item for item in preparations if item['step'] == 1]
                self.assertGreaterEqual(len(passes), 2)
                self.assertTrue(any(item['compact'] for item in passes))
                self.assertTrue(all(item['before'] == [BUSINESS] and item['after'] == [BUSINESS] for item in passes), passes)
            archived = runtime.store.rows('SELECT p.body,p.sha256 FROM harness_model_payloads p JOIN model_requests m '
                'ON m.id=p.request_id WHERE m.run_id=? ORDER BY m.step', (run['id'],))
            self.assertEqual(len(archived), len(raw_wires))
            for row, raw in zip(archived, raw_wires, strict=True):
                self.assertEqual(row['body'].encode('utf-8'), raw)
                self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())
            report = {'transport': 'real provider serializer with httpx.MockTransport', 'external_model': False,
                'new_activation': newly_described, 'compactions': len(summaries), 'preparations': preparations,
                'conversation_tools': [[item['function']['name'] for item in wire['tools']] for wire in conversations],
                'serialized_estimates': [TokenEstimator(config['model']).count(raw.decode('utf-8')) for raw in raw_wires]}
            (EVIDENCE / ('tool-retention-activation.json' if newly_described else 'tool-retention-after-result.json')).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            await runtime.close()

    async def test_compaction_after_business_result_restores_fitting_candidates_without_a_second_describe(self):
        await self.scenario(newly_described=False)

    async def test_new_activation_protection_survives_all_compaction_prepares_until_real_conversation_response(self):
        await self.scenario(newly_described=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
