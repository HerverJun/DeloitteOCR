import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from ocr_workbench.agent.graph import GraphServices, build_graph
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.external_review import CredentialVault
from ocr_workbench.service import create_app
from test_agent_connection import echo_probe


class InboxAPITests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def app_client(self, model):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bundle'
            (bundle / 'config').mkdir(parents=True)
            (bundle / 'config/engines.json').write_text('{}', encoding='utf-8')
            app = create_app(bundle, root / 'workspace', 'synthetic-token', start_queue=False, agent_enabled=True)
            project = app.state.store.project('Inbox API')['id']
            app.state.agent_connection.vault = CredentialVault(root / 'credentials')
            await app.state.agent_connection.save({'protocol': 'openai_chat_completions', 'base_url': 'https://synthetic.invalid/v1',
                'model': 'synthetic', 'api_key': 'synthetic-key'}, transport=httpx.MockTransport(echo_probe))
            async with app.router.lifespan_context(app):
                manager = app.state.agent_runtime
                manager.graph = build_graph(GraphServices(manager.agent, manager.registry, model, jobs=manager.jobs, inbox=manager.inbox), manager.checkpoints.saver)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    self.assertEqual((await client.get('/api/agent/status')).status_code, 401)
                    client.headers['Authorization'] = 'Bearer synthetic-token'
                    await client.put(f'/api/projects/{project}/agent/controller-authorization', json={'revision': 1, 'allow': True})
                    session = (await client.post(f'/api/projects/{project}/agent/sessions', json={'client_request_id': 'session', 'title': 'Inbox'})).json()['id']
                    yield app, client, session

    @staticmethod
    def response(message, step):
        return normalize_response('openai_chat_completions', {'choices': [{'message': message, 'finish_reason': 'tool_calls' if 'tool_calls' in message else 'stop'}]}, f'synthetic-{step}')

    async def wait_status(self, client, run_id, expected):
        for _ in range(200):
            run = (await client.get(f'/api/agent/runs/{run_id}')).json()
            if run['status'] == expected:
                return run
            if run['status'] == 'failed':
                self.fail(str(run))
            await asyncio.sleep(.02)
        self.fail(f'Expected {expected}: {run}')

    async def test_authenticated_duplicate_inbox_automatically_supersedes_question(self):
        messages = []
        async def model(state, run):
            messages.append(state['messages'])
            message = {'role': 'assistant', 'content': '按追加说明完成'}
            if state['step'] == 0:
                message = {'role': 'assistant', 'tool_calls': [{'id': 'ask', 'type': 'function', 'function': {
                    'name': 'ask_user', 'arguments': json.dumps({'question': '哪类？', 'options': ['文字', '表格']})}}]}
            return self.response(message, state['step'])
        async with self.app_client(model) as (app, client, session):
            route = f'/api/agent/sessions/{session}/messages'
            sent = await client.post(route, json={'client_request_id': 'start', 'content': '查询'})
            self.assertEqual(sent.status_code, 200, sent.text)
            run = sent.json()['run']
            await self.wait_status(client, run['id'], 'waiting_user')
            old = app.state.store.rows('SELECT * FROM agent_decisions')[0]
            body = {'client_request_id': 'append', 'content': '不用分类，只说明当前状态'}
            replies = await asyncio.gather(*(client.post(route, json=body) for _ in range(5)))
            self.assertTrue(all(r.status_code == 200 for r in replies), [r.text for r in replies])
            self.assertEqual(len({r.json()['inbox_id'] for r in replies}), 1)
            await self.wait_status(client, run['id'], 'completed')
            self.assertEqual(len(messages), 2)
            self.assertEqual(sum(m['content'] == body['content'] for m in messages[-1] if m['role'] == 'user'), 1)
            snapshot = (await client.get(f'/api/agent/sessions/{session}')).json()
            self.assertEqual(len(snapshot['runs']), 1)
            self.assertEqual(snapshot['inbox'][0]['state'], 'applied')
            self.assertEqual((await client.post(route, json=body)).json()['inbox_id'], replies[0].json()['inbox_id'])
            self.assertEqual((await client.post(route, json={**body, 'content': 'changed'})).status_code, 409)
            stale = await client.post(f"/api/agent/decisions/{old['id']}/reply", json={'client_request_id': 'old', 'payload_hash': old['payload_hash'], 'option_id': 'option-0'})
            self.assertEqual(stale.status_code, 409)

    async def test_stop_marks_pending_inbox_discarded_and_is_idempotent(self):
        entered = asyncio.Event()
        async def model(state, run):
            entered.set()
            await asyncio.Event().wait()
        async with self.app_client(model) as (app, client, session):
            route = f'/api/agent/sessions/{session}/messages'
            sent = await client.post(route, json={'client_request_id': 'start', 'content': '查询'})
            run = sent.json()['run']
            await asyncio.wait_for(entered.wait(), 2)
            await client.post(route, json={'client_request_id': 'append', 'content': 'pending'})
            body = {'client_request_id': 'cancel', 'generation': run['generation'], 'mode': 'stop_agent'}
            first = await client.post(f"/api/agent/runs/{run['id']}/cancel", json=body)
            duplicate = await client.post(f"/api/agent/runs/{run['id']}/cancel", json=body)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json(), duplicate.json())
            changed = await client.post(f"/api/agent/runs/{run['id']}/cancel", json={**body, 'mode': 'cancel_owned_jobs'})
            self.assertEqual(changed.status_code, 409)
            snapshot = (await client.get(f'/api/agent/sessions/{session}')).json()
            self.assertEqual(snapshot['inbox'][0]['state'], 'discarded')

    async def test_persisted_reply_is_recovered_when_notification_is_lost(self):
        async def model(state, run):
            message = {'role': 'assistant', 'content': '已收到选择'}
            if state['step'] == 0:
                message = {'role': 'assistant', 'tool_calls': [{'id': 'ask', 'type': 'function', 'function': {
                    'name': 'ask_user', 'arguments': json.dumps({'question': '选择？', 'options': ['一', '二']})}}]}
            return self.response(message, state['step'])
        async with self.app_client(model) as (app, client, session):
            sent = await client.post(f'/api/agent/sessions/{session}/messages', json={'client_request_id': 'start', 'content': '查询'})
            run = sent.json()['run']
            await self.wait_status(client, run['id'], 'waiting_user')
            decision = app.state.store.rows('SELECT * FROM agent_decisions')[0]
            # Simulate reply commit followed by crash before schedule/HTTP ack.
            manager = app.state.agent_runtime
            manager.agent.reply_decision(run['project_id'], run['id'], decision['id'], {
                'client_request_id': 'reply', 'payload_hash': decision['payload_hash'], 'option_id': 'option-0'})
            await self.wait_status(client, run['id'], 'completed')
            self.assertEqual(len(app.state.store.rows('SELECT * FROM agent_decisions')), 1)
