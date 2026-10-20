"""Real loopback HTTP provider → official Harness runtime → session SSE.

The model controller is deliberately synthetic. Production request parsing,
configuration/DPAPI, dispatch, native data workers, persistence and SSE routes
are real. No model_provider, HTTPX mock transport, or user workspace is used.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
from pathlib import Path
import socket
import sys
import unittest

import httpx
from fastapi import FastAPI
import uvicorn

import test_data_tools as data_tests
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.routes import router

HELPER = Path('F:/OCR/output/agent-streaming-ux-dev-20261005')
EVIDENCE = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence')
sys.path.insert(0, str(HELPER))
from local_stream_controller import ANSWER, LocalController


class ProductionStreamingTests(unittest.IsolatedAsyncioTestCase):
    setUp = data_tests.DataToolTests.setUp
    tearDown = data_tests.DataToolTests.tearDown
    asset = data_tests.DataToolTests.asset

    async def asyncSetUp(self):
        self.sse_task = None
        self.second_sse_task = None
        self.client = None
        self.server_task = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.origin = 'http://127.0.0.1:' + str(self.sock.getsockname()[1])
        self.headers = {'Origin': self.origin}
        self.controller = LocalController(delay=0.004, pause=True)
        self.received, self.queue = [], asyncio.Queue()

        @asynccontextmanager
        async def lifespan(app):
            for name in ('workspace_store', 'asset_library', 'data_workbench', 'artifact_center'):
                setattr(app.state, name, getattr(self.services, name))
            self.runtime = AgentRuntime(Path(self.temporary.name) / 'production-agent', app.state)
            self.assertIsNone(self.runtime.model_provider)
            app.state.agent = self.runtime
            self.services.agent = self.runtime
            await self.runtime.start()
            try:
                yield
            finally:
                await self.runtime.close()

        app = FastAPI(lifespan=lifespan)
        app.include_router(router)
        self.controller.install(app)
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0,
            log_level='warning', access_log=False, loop='asyncio', lifespan='on'))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        async with asyncio.timeout(60):
            while not self.server.started:
                if self.server_task.done():
                    await self.server_task
                    self.fail('Loopback server failed to start')
                await asyncio.sleep(0.02)
        self.client = httpx.AsyncClient(base_url=self.origin, trust_env=False, timeout=120)

    async def asyncTearDown(self):
        self.controller.release.set()
        if self.sse_task:
            self.sse_task.cancel()
            await asyncio.gather(self.sse_task, return_exceptions=True)
        if self.second_sse_task:
            self.second_sse_task.cancel()
            await asyncio.gather(self.second_sse_task, return_exceptions=True)
        if self.client:
            await self.client.aclose()
        if self.server_task:
            self.server.should_exit = True
            await asyncio.wait_for(self.server_task, 15)
        self.sock.close()

    async def connect(self, protocol):
        result = await self.client.put('/api/platform/v1/agent/connection', headers=self.headers,
            json={'protocol': protocol, 'base_url': self.origin + '/v1', 'model': 'local-stream-demo',
                  'api_key': 'local', 'context_cap': 65536, 'max_output_tokens': 2048, 'streaming': True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['available'])
        capabilities = result.json()['capabilities']
        self.assertEqual(capabilities['total_rounds'], 3)
        self.assertEqual(capabilities['streaming']['status'], 'supported')
        self.assertTrue(capabilities['production_preflight']['passed'])
        self.assertEqual(len(self.controller.requests), 3)

    async def create_session(self, selected=()):
        response = await self.client.post('/api/platform/v1/workspaces/wtest/agent/sessions',
            headers=self.headers, json={'name': 'Synthetic production SSE', 'request_id': 'production_session',
                                       'selected_assets': list(selected)})
        self.assertEqual(response.status_code, 201, response.text)
        self.session_id = response.json()['id']
        connected = asyncio.Event()

        async def consume():
            async with self.client.stream('GET', f'/api/platform/v1/agent/sessions/{self.session_id}/stream') as response:
                self.assertEqual(response.status_code, 200)
                connected.set()
                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        event = json.loads(line[6:])
                        event['observed_monotonic'] = asyncio.get_running_loop().time()
                        self.received.append(event)
                        await self.queue.put(event)
        self.sse_task = asyncio.create_task(consume())
        await asyncio.wait_for(connected.wait(), 10)

    async def submit(self, content):
        response = await self.client.post(f'/api/platform/v1/agent/sessions/{self.session_id}/messages',
            headers=self.headers, json={'content': content, 'request_id': 'production_message'})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    async def until(self, predicate):
        async with asyncio.timeout(90):
            while True:
                event = await self.queue.get()
                if predicate(event):
                    return event
                if event['kind'] == 'agent_activity' and event['payload']['phase'] in {'failed', 'needs_verification'}:
                    self.fail('Unexpected terminal activity: ' + json.dumps(event, ensure_ascii=False))

    async def chat(self, protocol):
        await self.connect(protocol)
        await self.create_session()
        second_connected, second_preview, second_completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        second_events = []

        async def second_subscriber():
            async with self.client.stream('GET', f'/api/platform/v1/agent/sessions/{self.session_id}/stream') as response:
                self.assertEqual(response.status_code, 200)
                second_connected.set()
                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        event = json.loads(line[6:])
                        second_events.append(event)
                        if event['kind'] == 'response_preview' and event['payload']['source'] == 'answer':
                            second_preview.set()
                        if event['kind'] == 'agent_activity' and event['payload']['phase'] == 'completed':
                            second_completed.set()
        self.second_sse_task = asyncio.create_task(second_subscriber())
        await asyncio.wait_for(second_connected.wait(), 10)
        run = await self.submit('你好，请介绍平台能力。')
        preview = await self.until(lambda event: event['kind'] == 'response_preview' and event['payload']['source'] == 'answer')
        await asyncio.wait_for(second_preview.wait(), 5)
        self.assertTrue(preview['payload']['content'])
        self.assertTrue(self.controller.first_chunk.is_set())
        self.assertFalse(self.controller.release.is_set())
        self.assertEqual(self.runtime.store.run(run['id'])['status'], 'running')
        request = self.runtime.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))
        self.assertEqual(len(request), 1)
        self.assertEqual(request[0]['state'], 'sent')
        self.assertEqual(self.runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],)), [])
        self.assertFalse(any(event['kind'] == 'response_state' and event['payload']['status'] == 'confirmed'
                             for event in self.received))
        self.controller.release.set()
        await self.until(lambda event: event['kind'] == 'agent_activity' and event['payload']['phase'] == 'completed')
        await asyncio.wait_for(second_completed.wait(), 5)
        self.assertEqual([event['seq'] for event in self.received], [event['seq'] for event in second_events])
        self.assertEqual(self.runtime.store.run(run['id'])['status'], 'answered')
        events = self.runtime.store.events(self.session_id, limit=50)['items']
        confirmed = [event for event in events if event['kind'] == 'response_state' and event['payload']['status'] == 'confirmed']
        formal = [event for event in events if event['kind'] == 'message' and event['payload']['role'] == 'assistant']
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(len(formal), 1)
        self.assertEqual(formal[0]['payload']['content'], ANSWER)
        self.assertEqual(formal[0]['payload']['display_id'], preview['payload']['display_id'])
        self.assertEqual(confirmed[0]['payload']['display_id'], preview['payload']['display_id'])
        self.assertLess(preview['seq'], confirmed[0]['seq'])
        self.assertLess(confirmed[0]['seq'], formal[0]['seq'])
        response = json.loads(self.runtime.store.rows('SELECT response FROM model_requests WHERE run_id=?', (run['id'],))[0]['response'])
        self.assertEqual(response['calls'][0]['arguments'], {'content': ANSWER})
        self.assertNotIn('public_preview', response)
        self.assertEqual(len([item for item in self.controller.requests if item['purpose'] == 'business']), 1)
        operations = self.runtime.store.rows('SELECT tool FROM operations WHERE run_id=?', (run['id'],))
        self.assertEqual(operations, [{'tool': 'conversation_answer'}])
        archived = self.runtime.store.rows('SELECT p.* FROM harness_model_payloads p JOIN model_requests m ON m.id=p.request_id WHERE m.run_id=? ORDER BY m.step', (run['id'],))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]['sha256'], hashlib.sha256(archived[0]['body'].encode('utf-8')).hexdigest())
        self.assertTrue(json.loads(archived[0]['body'])['stream'])
        self.assertTrue(self.runtime.store.rows('SELECT * FROM harness_events WHERE session_id=?', (self.session_id,)))
        confirmed_time = next(event['observed_monotonic'] for event in self.received if event['seq'] == confirmed[0]['seq'])
        receipt = {'controller': 'synthetic-local-no-remote-model', 'production_http': True,
            'protocol': protocol, 'probe_requests': 3, 'business_model_posts': 1,
            'preview_before_model_received': True, 'confirmed_events': 1, 'formal_messages': 1,
            'concurrent_sse_subscribers': 2, 'both_received_identical_sequences': True,
            'same_display_id': True, 'tool_dispatches': operations,
            'preview_to_confirmed_seconds': round(confirmed_time - preview['observed_monotonic'], 4),
            'events': [{'seq': event['seq'], 'kind': event['kind'], 'payload': event['payload']} for event in events]}
        (EVIDENCE / ('harness-production-sse-' + protocol + '.json')).write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')

    async def test_openai_tool_only_answer_before_complete_response_confirmed_once(self):
        await self.chat('openai_chat_completions')

    async def test_anthropic_tool_only_answer_before_complete_response_confirmed_once(self):
        await self.chat('anthropic_messages')

    async def test_native_import_real_worker_progress_and_single_submission(self):
        asset = self.asset('suppliers-left.csv')
        self.controller.asset = {'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash}
        self.controller.release.set()
        await self.connect('openai_chat_completions')
        await self.create_session([asset.asset_id])
        run = await self.submit('[native-import] 导入并读回合成供应商台账。')
        await self.until(lambda event: event['kind'] == 'agent_activity' and event['payload']['phase'] == 'completed')
        self.assertEqual(self.runtime.store.run(run['id'])['status'], 'succeeded')
        native = self.native.list('wtest')
        self.assertEqual(len(native), 1)
        record = self.native.get(native[0]['id'])
        self.assertEqual(len(record['uploads']), 1)
        self.assertEqual(len(record['jobs']), 1)
        self.assertEqual(record['jobs'][0]['action'], 'import')
        self.assertEqual(record['jobs'][0]['status'], 'succeeded')
        events = self.runtime.store.events(self.session_id, limit=100)['items']
        operations = self.runtime.store.rows('SELECT tool,result FROM operations WHERE run_id=? ORDER BY created', (run['id'],))
        self.assertEqual([row['tool'] for row in operations], ['tools_describe', 'data_create_session',
            'data_import', 'data_read_table', 'delivery_publish'])
        imported = json.loads(next(row['result'] for row in operations if row['tool'] == 'data_import'))
        readback = json.loads(next(row['result'] for row in operations if row['tool'] == 'data_read_table'))
        self.assertEqual(len(imported['job_refs']), 1)
        self.assertEqual(record['jobs'][0]['id'], imported['job_refs'][0]['object_id'])
        self.assertEqual(readback['data']['rows'][0]['cells'][0]['value'], '000001')
        activity = [event['payload'] for event in events if event['kind'] == 'tool_activity' and event['payload']['tool'] == 'data_import']
        self.assertEqual([item['phase'] for item in activity], ['started', 'pending', 'finished'])
        self.assertEqual(len({item['operation_id'] for item in activity}), 1)
        progress = [event['payload'] for event in events if event['kind'] == 'job_progress']
        self.assertTrue(progress, 'Expected real worker observer samples')
        for item in progress:
            self.assertEqual(item['job_ref']['object_id'], imported['job_refs'][0]['object_id'])
            self.assertEqual(item['observation'], 'native')
        self.assertEqual(len([item for item in self.controller.requests if item['purpose'] == 'business']), 5)
        delivery = [event for event in events if event['kind'] == 'delivery']
        self.assertEqual(len(delivery), 1)
        self.assertTrue(delivery[0]['payload']['delivery']['data']['verified'])
        receipt = {'controller': 'synthetic-local-no-remote-model', 'production_http': True,
            'real_native_worker': True, 'business_submit_posts': 1, 'business_model_posts': 5,
            'native_uploads': 1, 'native_jobs': record['jobs'], 'native_job_refs': imported['job_refs'],
            'readback_first_id': '000001', 'tool_phases': activity, 'job_progress': progress,
            'delivery_verified': True, 'final_status': 'succeeded'}
        (EVIDENCE / 'harness-production-native-import.json').write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    unittest.main(verbosity=2)
