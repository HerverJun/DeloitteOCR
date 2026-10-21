"""Fault windows in the Clear journal: never replay uncertain model/writes."""
from dataclasses import replace
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

import test_data_tools as fixtures
import test_graph as graph_tests
from workbench_platform.agent.harness import HarnessEngine, HarnessProcess
from workbench_platform.agent.providers import ProviderFault, request as provider_request
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import Conflict, canonical, digest

response = graph_tests.response


class HarnessJournalTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DataToolTests.setUp
    tearDown = fixtures.DataToolTests.tearDown

    def runtime(self):
        async def forbidden(*args):
            self.fail('Recovery must not contact the model')
        runtime = AgentRuntime(Path(self.temporary.name) / 'runtime-agent', self.services, model_provider=forbidden)
        self.services.agent = runtime
        runtime.graph = HarnessEngine(runtime)
        session = runtime.store.create_session('wtest', 'Fault recovery', [], 'fault-session')
        run, _ = runtime.store.start_run(session['id'], 'Finish the original task', 'fault-run', 0)
        state = runtime.initial_state(run, 'Finish the original task')
        runtime.graph.save(state)
        return runtime, state

    async def test_received_response_restores_exact_calls_once_without_dispatch(self):
        runtime, state = self.runtime()
        try:
            record, _ = runtime.store.begin_model(state['run_id'], 1, 0, digest('original serialized request'))
            reply = {**response(state, 'data_create_session', {'name': 'original'}), 'request_id': record['id']}
            runtime.store.finish_model(record['id'], reply)
            with runtime.store.transaction() as db:
                db.execute('INSERT INTO harness_model_calls VALUES(?,?,?,?)',
                           (record['id'], 'conversation', record['fingerprint'], '{}'))
            state['pending_model_request'] = record['id']
            runtime.graph.save(state)
            result = await runtime.harness_recovery(state, {'call_ids': [], 'committed_model_requests': []})
            self.assertEqual(result['response'], reply)
            self.assertEqual(state['step'], 1)
            self.assertEqual(result['results']['call_0']['error']['code'], 'not_started')
            self.assertEqual(runtime.store.rows('SELECT * FROM operations'), [])
            self.assertEqual(self.native.list('wtest'), [])
            again = await runtime.harness_recovery(state, {'call_ids': [], 'committed_model_requests': [record['id']]})
            self.assertNotIn('response', again)
            self.assertNotIn('pending_model_request', state)
            self.assertEqual(state['step'], 1)
            self.assertEqual(len(runtime.store.rows('SELECT * FROM model_requests')), 1)
        finally:
            await runtime.close()

    async def test_sent_model_receipt_unknown_blocks_before_child_or_tool(self):
        runtime, state = self.runtime()
        try:
            runtime.store.begin_model(state['run_id'], 1, 0, digest('unknown request'))
            with self.assertRaises(ProviderFault) as caught:
                await runtime.graph.ainvoke(None, runtime.options(state['run_id']))
            self.assertEqual(caught.exception.code, 'response_unknown')
            self.assertEqual(runtime.graph.processes, {})
            self.assertEqual(runtime.store.rows('SELECT * FROM operations'), [])
        finally:
            await runtime.close()

    async def test_unknown_nonidempotent_write_stops_before_model(self):
        runtime, state = self.runtime()
        try:
            # Model a native endpoint that lacks identity lookup/replay support.
            name = 'data_create_session'
            runtime.registry.tools[name] = replace(runtime.registry.tools[name], idempotent=False, lookup_operation=None)
            call = {'call_id': 'submitted-unknown', 'tool': name, 'arguments': {'name': 'must not duplicate'}}
            runtime.store.begin_operation(state['run_id'], 1, call)
            with self.assertRaises(ProviderFault) as caught:
                await runtime.harness_wait(state)
            self.assertEqual(caught.exception.code, 'response_unknown')
            self.assertEqual(self.native.list('wtest'), [])
            value = json.loads(runtime.store.rows('SELECT result FROM operations')[0]['result'])
            self.assertEqual(value['status'], 'needs_verification')
        finally:
            await runtime.close()

    async def test_history_projection_rejects_changed_event_identity(self):
        runtime, state = self.runtime()
        try:
            event = {'seq': 0, 'time': 1, 'type': 'turn/start', 'data': {'turn': 1}}
            runtime.graph.record_event(state, event)
            runtime.graph.record_event(state, event)
            with self.assertRaises(Conflict):
                runtime.graph.record_event(state, {**event, 'data': {'turn': 2}})
            saved = runtime.store.rows('SELECT payload,sha256 FROM harness_events')
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]['payload'], canonical(event))
            self.assertEqual(saved[0]['sha256'], digest(event))
        finally:
            await runtime.close()

    async def test_new_run_preserves_unknown_history_and_never_borrows_reused_call_id(self):
        runtime, old_state = self.runtime()
        try:
            tool = 'data_create_session'
            old_call = {'call_id': 'call_0', 'tool': tool, 'arguments': {'name': 'old native session'}}
            old_result = await runtime.registry.dispatch(old_call, runtime.context(old_state))
            self.assertEqual(old_result['status'], 'success')
            runtime.registry.tools[tool] = replace(runtime.registry.tools[tool], idempotent=False, lookup_operation=None)
            unknown = {'call_id': 'historical-write-unknown', 'tool': tool,
                       'arguments': {'name': 'uncertain historical write'}}
            runtime.store.begin_operation(old_state['run_id'], old_state['generation'], unknown)
            before = runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY id', (old_state['run_id'],))
            with runtime.store.transaction() as db:
                runtime.store.cancel_in_transaction(db, run_id=old_state['run_id'])
            new, _ = runtime.store.start_run(old_state['session_id'], 'Continue with a new task', 'new-recovery-run', 0)
            state = runtime.initial_state(new, 'Continue with a new task')
            state['visible'].append(tool)
            runtime.graph.save(state)
            history = await runtime.harness_recovery(state,
                {'call_ids': [unknown['call_id']], 'committed_model_requests': []})
            self.assertEqual(history['results'][unknown['call_id']]['status'], 'needs_verification')
            self.assertEqual(history['results'][unknown['call_id']]['error']['code'], 'response_unknown')
            record, _ = runtime.store.begin_model(new['id'], new['generation'], 0, digest('new serialized request'))
            reply = {**response(state, tool, {'name': 'new native session'}), 'request_id': record['id']}
            self.assertEqual(reply['calls'][0]['call_id'], old_call['call_id'])
            runtime.store.finish_model(record['id'], reply)
            with runtime.store.transaction() as db:
                db.execute('INSERT INTO harness_model_calls VALUES(?,?,?,?)',
                           (record['id'], 'conversation', record['fingerprint'], '{}'))
            state['pending_model_request'] = record['id']
            runtime.graph.save(state)
            recovered = await runtime.harness_recovery(state,
                {'call_ids': [unknown['call_id']], 'committed_model_requests': []})
            self.assertEqual(recovered['response'], reply)
            self.assertEqual(recovered['results'][old_call['call_id']]['error']['code'], 'not_started')
            self.assertNotEqual(recovered['results'][old_call['call_id']], old_result)
            self.assertEqual(recovered['results'][unknown['call_id']]['status'], 'needs_verification')
            self.assertEqual(runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY id',
                                               (old_state['run_id'],)), before)
            self.assertEqual(runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (new['id'],)), [])
            result = await runtime.harness_tool(state, reply['calls'][0])
            self.assertEqual(result['status'], 'success')
            self.assertNotEqual(result['data']['data_session_id'], old_result['data']['data_session_id'])
            self.assertEqual(len(self.native.list('wtest')), 2)
            old_operation = next(item for item in before if item['call_id'] == old_call['call_id'])
            new_operation = runtime.store.rows('SELECT * FROM operations WHERE run_id=? AND call_id=?',
                                              (new['id'], old_call['call_id']))[0]
            self.assertNotEqual(new_operation['id'], old_operation['id'])
            self.assertEqual(runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY id',
                                               (old_state['run_id'],)), before)
        finally:
            await runtime.close()


class HarnessControlTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DataToolTests.setUp
    tearDown = fixtures.DataToolTests.tearDown
    make_runtime = graph_tests.GraphTests.make_runtime
    wait_status = graph_tests.GraphTests.wait_status

    async def test_idle_compaction_keeps_terminal_status_and_uses_no_business_tools(self):
        purposes = []
        async def provider(state, schemas):
            purpose = runtime.store.rows('SELECT purpose FROM harness_model_calls WHERE request_id=?',
                                         (state['pending_model_request'],))[0]['purpose']
            purposes.append(purpose)
            if purpose == 'compaction':
                value = {'goal': '回复原目标', 'constraints': [], 'plan': [], 'completed': ['答案已确认'],
                         'pending_jobs': [], 'artifacts': [], 'next_step': '等待新目标', 'references': []}
                return response(state, content=canonical(value))
            return response(state, 'conversation_answer', {'content': '已读取目标，可以继续讨论。'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '请说明如何继续', 'idle-compact')
            await self.wait_status(runtime, run['id'], {'answered', 'failed'})
            task = runtime.running.get(run['id'])
            if task:
                await task
            await asyncio.sleep(0)
            result = await runtime.compact(session['id'])
            self.assertFalse(result['queued'])
            self.assertTrue(result['compacted'])
            final = runtime.store.run(run['id'])
            self.assertEqual((final['status'], final['rounds'], final['tool_count']), ('answered', 1, 1))
            self.assertEqual(purposes, ['conversation', 'compaction'])
            operations = runtime.store.rows('SELECT tool FROM operations WHERE run_id=?', (run['id'],))
            self.assertEqual(operations, [{'tool': 'conversation_answer'}])
            self.assertEqual(self.native.list('wtest'), [])
        finally:
            await runtime.close()

    async def test_idle_http_compaction_journals_transport_and_shrinks_without_changing_terminal_run(self):
        requests = []
        summary = {'goal': '保留原目标并等待新目标', 'constraints': ['保留来源编号'], 'plan': [],
                   'completed': ['已回复'], 'pending_jobs': [], 'artifacts': [],
                   'next_step': '等待新目标', 'references': []}
        def transport(request):
            self.assertEqual(request.url.path, '/v1/chat/completions')
            body = json.loads(request.content)
            requests.append(body)
            if body.get('tools'):
                message = {'role': 'assistant', 'content': '已经读取原目标，可以继续讨论。'}
            else:
                message = {'role': 'assistant', 'content': canonical(summary)}
            return httpx.Response(200, json={'id': 'http-response-' + str(len(requests)), 'model': body['model'],
                'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 5000 if body.get('tools') else 3000, 'completion_tokens': 100}})
        mock_transport = httpx.MockTransport(transport)
        async def http_request(config, secret, payload, identifier, **callbacks):
            return await provider_request(config, secret, payload, identifier, transport=mock_transport, **callbacks)
        async def probe(*args, **kwargs):
            return {'native_tools': True, 'business_data_sent': False}
        runtime = AgentRuntime(Path(self.temporary.name) / 'runtime-http-agent', self.services)
        self.services.agent = runtime
        async def history(state):
            idle = runtime.graph.idle_processes.get(state['session_id'])
            if idle:
                return await idle.call('history', {})
            process = HarnessProcess(runtime.graph, dict(state))
            try:
                await process.start()
                return await process.call('history', {})
            finally:
                await process.close()
        try:
            with patch('workbench_platform.agent.connection.probe', probe), \
                 patch('workbench_platform.agent.harness_service.request', http_request):
                await runtime.connection.save({'protocol': 'openai_chat_completions',
                    'base_url': 'http://127.0.0.1:54321/v1', 'model': 'mock-http-model', 'api_key': 'synthetic-secret',
                    'context_cap': 65536, 'max_output_tokens': 2048, 'streaming': False})
                await runtime.start()
                session = runtime.store.create_session('wtest', 'HTTP compaction', [], 'http-compact-session')
                content = '请读取原目标并保留来源编号。\n' + ('记录A0001：合成资料正文，供上下文压缩验收。\n' * 450)
                run = await runtime.submit(session['id'], content, 'http-idle-compact')
                final = await self.wait_status(runtime, run['id'], {'answered', 'failed'})
                self.assertEqual(final['status'], 'answered')
                task = runtime.running.get(run['id'])
                if task:
                    await task
                before_run = runtime.store.run(run['id'])
                before_history = await history(runtime.graph.load(run['id']))
                before_events = runtime.store.rows('SELECT * FROM harness_events WHERE session_id=? ORDER BY seq', (session['id'],))
                before_operation = runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],))
                result = await runtime.compact(session['id'])
                self.assertFalse(result['queued'])
                self.assertTrue(result['compacted'])
                after_history = await history(runtime.graph.load(run['id']))
            after_run = runtime.store.run(run['id'])
            self.assertEqual(after_run, before_run)
            self.assertLess(len(canonical(after_history)), len(canonical(before_history)))
            self.assertIn(summary['goal'], canonical(after_history))
            self.assertIn(summary['constraints'][0], canonical(after_history))
            after_events = runtime.store.rows('SELECT * FROM harness_events WHERE session_id=? ORDER BY seq', (session['id'],))
            self.assertEqual(after_events[:len(before_events)], before_events)
            self.assertEqual(runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],)), before_operation)
            self.assertEqual(before_operation, [])
            calls = runtime.store.rows('SELECT m.id,m.state,h.purpose FROM model_requests m JOIN harness_model_calls h ON h.request_id=m.id ORDER BY m.step')
            self.assertEqual([item['purpose'] for item in calls], ['conversation', 'compaction'])
            self.assertTrue(all(item['state'] == 'received' for item in calls))
            journal = runtime.store.rows("SELECT payload FROM events WHERE run_id=? AND kind='model_transport' ORDER BY seq", (run['id'],))
            compaction_transports = [json.loads(item['payload']) for item in journal if json.loads(item['payload']).get('purpose') == 'compaction']
            self.assertEqual(len(compaction_transports), 1)
            self.assertEqual(compaction_transports[0]['request_id'], calls[1]['id'])
            self.assertEqual(compaction_transports[0]['mode'], 'disabled')
            self.assertEqual(len(requests), 2)
            self.assertEqual(self.native.list('wtest'), [])
        finally:
            await runtime.close()

    async def test_stop_then_new_goal_waits_for_old_session_writer_to_close(self):
        first_request, first_release, old_closing, close_release, new_saved = [asyncio.Event() for _ in range(5)]
        requests = []
        async def provider(state, schemas):
            requests.append(state['run_id'])
            if len(requests) == 1:
                first_request.set()
                await first_release.wait()
                return response(state, 'data_create_session', {'name': 'late cancelled write'})
            return response(state, 'conversation_answer', {'content': '新目标已回复'})
        runtime, session = await self.make_runtime(provider)
        try:
            old = await runtime.submit(session['id'], '旧目标', 'stop-old')
            await first_request.wait()
            child = runtime.graph.processes[old['id']]
            original_close = child.close
            async def gated_close():
                old_closing.set()
                await close_release.wait()
                await original_close()
            child.close = gated_close
            runtime.cancel(old['id'])
            await old_closing.wait()
            original_save = runtime.graph.save
            def save(state):
                original_save(state)
                if state['run_id'] != old['id']:
                    new_saved.set()
            runtime.graph.save = save
            new = await runtime.submit(session['id'], '新的讨论目标', 'after-stop')
            await new_saved.wait()
            self.assertNotIn(new['id'], runtime.graph.processes)
            self.assertEqual(requests, [old['id']])
            close_release.set()
            first_release.set()
            final = await self.wait_status(runtime, new['id'], {'answered', 'failed'})
            self.assertEqual(final['status'], 'answered')
            self.assertEqual(runtime.store.run(old['id'])['status'], 'cancelled')
            self.assertEqual(self.native.list('wtest'), [])
            self.assertEqual(requests, [old['id'], new['id']])
        finally:
            close_release.set()
            first_release.set()
            await runtime.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
