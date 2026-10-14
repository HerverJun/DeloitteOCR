"""Meaningful protocol, DPAPI, identity and persisted graph compatibility checks."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import httpx
from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.dependencies import load_dependencies
from workbench_platform.agent.connection import ControllerConnection, probe
from workbench_platform.agent.credentials import CredentialVault
from workbench_platform.agent.providers import payload_for, normalize_response, ProviderFault
from workbench_platform.agent.registry import ToolRegistry, ToolSpec, ToolContext
from workbench_platform.agent.store import AgentStore, Conflict

ROOT = Path('F:/OCR/output/platform-agent-dev-20261004/evidence/foundation')


def config(protocol='openai_chat_completions'):
    return {'protocol': protocol, 'base_url': 'http://127.0.0.1:54321/v1', 'model': 'contract-model',
            'token_parameter': 'max_tokens', 'max_output_tokens': 1024}


def transport_for(protocol, *, failing=False):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if failing:
            return httpx.Response(401, json={'error': {'message': 'private-key-must-not-leak'}})
        if len(calls) == 1:
            challenge = body['messages'][-1]['content'].split('value ')[-1]
            if protocol == 'openai_chat_completions':
                response = {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
                    'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {
                        'name': 'probe_echo', 'arguments': json.dumps({'value': challenge})}}]}}]}
            else:
                response = {'role': 'assistant', 'type': 'message', 'stop_reason': 'tool_use',
                    'content': [{'type': 'tool_use', 'id': 'call_1', 'name': 'probe_echo', 'input': {'value': challenge}}]}
        elif len(calls) == 2:
            if protocol == 'openai_chat_completions':
                receipt = json.loads(body['messages'][-1]['content'])['receipt']
                response = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': receipt}}]}
            else:
                receipt = json.loads(body['messages'][-1]['content'][0]['content'])['receipt']
                response = {'role': 'assistant', 'type': 'message', 'stop_reason': 'end_turn',
                            'content': [{'type': 'text', 'text': receipt}]}
        else:
            # This endpoint supports native tools but answers stream=True with
            # valid JSON. The optional data-free SSE probe must record fallback
            # without invalidating the original two native-tool rounds.
            value = body['messages'][-1]['content'].rsplit('Value: ', 1)[1]
            if protocol == 'openai_chat_completions':
                response = {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
                    'content': value, 'tool_calls': [{'id': 'stream_probe', 'type': 'function', 'function': {
                        'name': 'conversation_answer', 'arguments': json.dumps({'content': value})}}]}}]}
            else:
                response = {'role': 'assistant', 'type': 'message', 'stop_reason': 'tool_use',
                    'content': [{'type': 'text', 'text': value}, {'type': 'tool_use', 'id': 'stream_probe',
                        'name': 'conversation_answer', 'input': {'content': value}}]}
        return httpx.Response(200, json=response)

    return httpx.MockTransport(handler), calls


class FoundationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    async def test_protocols_consume_real_tool_result_in_second_round(self):
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            transport, calls = transport_for(protocol)
            result = await probe(config(protocol), 'test-secret', transport=transport)
            self.assertEqual(result['rounds'], 2)
            self.assertEqual(result['total_rounds'], 3)
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[2]['stream'])
            self.assertEqual(result['streaming']['status'], 'fallback')
            self.assertTrue(result['native_tools'])
            self.assertFalse(result['business_data_sent'])
            disabled, disabled_calls = transport_for(protocol)
            no_stream = await probe({**config(protocol), 'streaming': False}, 'test-secret', transport=disabled)
            self.assertEqual(len(disabled_calls), 2)
            self.assertEqual(no_stream['total_rounds'], 2)
            self.assertEqual(no_stream['streaming']['status'], 'disabled')

    async def test_probe_failure_preserves_valid_connection_and_dpapi_secret(self):
        store = AgentStore(self.root / 'agent')
        connection = ControllerConnection(store)
        transport, _ = transport_for('openai_chat_completions')
        saved = await connection.save({**config(), 'api_key': 'private-key-must-not-leak'}, transport=transport)
        self.assertEqual(saved['revision'], 1)
        self.assertNotIn('credential_ref', saved)
        self.assertNotIn('private-key-must-not-leak', store.path.read_bytes().decode('latin1'))
        self.assertEqual(connection.resolve(1)[1], 'private-key-must-not-leak')
        failed, _ = transport_for('openai_chat_completions', failing=True)
        with self.assertRaises(ProviderFault) as caught:
            await connection.save({**config(), 'api_key': 'different-key'}, transport=failed)
        self.assertNotIn('private-key', str(caught.exception))
        self.assertEqual(ControllerConnection(store).view()['revision'], 1)

    async def test_truncated_duplicate_json_and_unpaired_result_are_rejected(self):
        for reason, arguments in [('length', '{}'), ('tool_calls', '{"x":1,"x":2}')]:
            raw = {'choices': [{'finish_reason': reason, 'message': {'role': 'assistant',
                'tool_calls': [{'id': 'x', 'type': 'function', 'function': {'name': 'echo', 'arguments': arguments}}]}}]}
            with self.assertRaises(ProviderFault):
                normalize_response('openai_chat_completions', raw, 'message')
        with self.assertRaises(ProviderFault):
            payload_for(config(), [{'role': 'tool', 'call_id': 'missing', 'result': {'status': 'success'}}])

    async def test_sqlite_checkpoint_closes_reopens_and_resumes_without_repeating_effect(self):
        StateGraph, Saver = load_dependencies()
        from langgraph.graph import START, END
        from langgraph.types import interrupt, Command
        from typing import TypedDict

        class State(TypedDict):
            count: int
            accepted: bool

        effects = []

        def effect(state):
            effects.append('executed')
            return {'count': state['count'] + 1}

        def wait(state):
            return {'accepted': interrupt({'question': '继续？'})}

        def graph(saver):
            builder = StateGraph(State)
            builder.add_node('effect', effect)
            builder.add_node('wait', wait)
            builder.add_edge(START, 'effect')
            builder.add_edge('effect', 'wait')
            builder.add_edge('wait', END)
            return builder.compile(checkpointer=saver)

        options = {'configurable': {'thread_id': 'test-platform-thread'}}
        async with Saver.from_conn_string(str(self.root / 'checkpoints.sqlite3')) as saver:
            first = await graph(saver).ainvoke({'count': 0, 'accepted': False}, options, durability='sync')
            self.assertTrue(first['__interrupt__'])
        async with Saver.from_conn_string(str(self.root / 'checkpoints.sqlite3')) as saver:
            result = await graph(saver).ainvoke(Command(resume=True), options, durability='sync')
            self.assertEqual(result['count'], 1)
            self.assertTrue(result['accepted'])
        self.assertEqual(effects, ['executed'])

    async def test_registry_strict_batch_server_context_and_replay_identity(self):
        class Echo(AgentModel):
            value: str

        effects = []
        def execute(arguments, context):
            effects.append((arguments.value, context.workspace_id, context.operation_id))
            return AgentToolResult(status='success', summary=arguments.value).model_dump()

        registry = ToolRegistry()
        registry.register(ToolSpec('test_echo', '真实注册路径测试', Echo, execute, effect='derive', idempotent=True))
        store = AgentStore(self.root / 'agent')
        session = store.create_session('wtest', 'Test', [], 'session_request')
        run, _ = store.start_run(session['id'], 'echo', 'run_request', 1)
        services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
        context = ToolContext('wtest', session['id'], run['id'], 1, 0, '', (), services, store)
        call = {'call_id': 'c1', 'tool': 'test_echo', 'arguments': {'value': 'hello'}}
        invalid = {**call, 'call_id': 'c2', 'arguments': {'value': 'hello', 'workspace_id': 'other'}}
        with self.assertRaises(ValueError):
            registry.validate_batch([call, invalid], {'test_echo'})
        self.assertEqual(effects, [])
        for _ in range(2):
            self.assertEqual((await registry.dispatch(call, context))['status'], 'success')
        self.assertEqual(len(effects), 1)
        with self.assertRaises(Conflict):
            await registry.dispatch({**call, 'arguments': {'value': 'changed'}}, context)
        with self.assertRaises(ValueError):
            registry.validate_batch([{**call, 'tool': 'shell'}], {'test_echo'})
        load_dependencies()
        tool = registry.as_langchain_tool('test_echo', context)
        self.assertEqual(tool.name, 'test_echo')
        self.assertEqual((await tool.ainvoke({'value': 'tool-interface'}))['status'], 'success')


if __name__ == '__main__':
    unittest.main(verbosity=2)
