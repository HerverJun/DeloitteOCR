"""Actual FastAPI routes, origin/scope validation and durable event projection."""
import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from workbench_platform.app import app

LOCAL = {'host': '127.0.0.1:8866', 'origin': 'http://127.0.0.1:8866'}
ROOT = Path('F:/OCR/output/platform-agent-dev-20261004/evidence/http')


class AgentRouteTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.previous = {key: os.environ.get(key) for key in ('WORKBENCH_DATA_DIR', 'WORKBENCH_AGENT_ENABLED')}
        os.environ['WORKBENCH_DATA_DIR'] = self.temporary.name
        os.environ['WORKBENCH_AGENT_ENABLED'] = '1'
        self.client = TestClient(app, base_url='http://127.0.0.1:8866')
        self.client.__enter__()
        created = self.client.post('/api/platform/v1/workspaces', headers=LOCAL,
            json={'schema_version': '1.0', 'name': 'Agent HTTP test'})
        self.assertEqual(created.status_code, 201, created.text)
        self.workspace = created.json()['workspace_id']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def create(self):
        path = f'/api/platform/v1/workspaces/{self.workspace}/agent/sessions'
        result = self.client.post(path, headers=LOCAL, json={'name': '新任务', 'request_id': 'session_request'})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(self.client.post(path, headers=LOCAL, json={'name': '新任务', 'request_id': 'session_request'}).json()['id'], result.json()['id'])
        return result.json()['id']

    def test_session_scope_idempotency_and_unconfigured_controller(self):
        session = self.create()
        missing = self.client.post(f'/api/platform/v1/agent/sessions/{session}/messages', headers=LOCAL,
                                  json={'content': '比对两个台账', 'request_id': 'message_request'})
        self.assertEqual(missing.status_code, 400)
        self.assertIn('主控', missing.json()['detail'])
        events = self.client.get(f'/api/platform/v1/agent/sessions/{session}/events', headers=LOCAL)
        self.assertEqual(events.json()['items'], [])
        connection = self.client.get('/api/platform/v1/agent/connection', headers=LOCAL)
        self.assertFalse(connection.json()['configured'])
        capabilities = self.client.get(f'/api/platform/v1/workspaces/{self.workspace}/agent/capabilities', headers=LOCAL)
        self.assertEqual(capabilities.status_code, 200)
        self.assertTrue({'data_compare', 'ocr_run', 'classification_infer', 'forensics_inspect', 'jobs_get'} <=
                        {tool['name'] for tool in capabilities.json()['tools']})

    def test_remote_origin_extra_fields_and_checkpoint_selection_are_rejected(self):
        session = self.create()
        path = f'/api/platform/v1/agent/sessions/{session}/messages'
        foreign = {**LOCAL, 'origin': 'https://foreign.example'}
        self.assertEqual(self.client.post(path, headers=foreign, json={'content': '目标', 'request_id': 'm1'}).status_code, 403)
        rejected = self.client.post(path, headers=LOCAL, json={'content': '目标', 'request_id': 'm1', 'thread_id': 'arbitrary'})
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn('arbitrary', rejected.text)
        self.assertEqual(self.client.get('/api/platform/v1/agent/sessions/missing', headers=LOCAL).status_code, 404)
        self.assertEqual(self.client.post('/api/platform/v1/workspaces/missing/agent/sessions', headers=LOCAL,
            json={'name': '新任务', 'request_id': 'other_request'}).status_code, 404)

    def test_disabling_agent_leaves_workspace_assets_and_data_routes_available(self):
        self.client.__exit__(None, None, None)
        os.environ['WORKBENCH_AGENT_ENABLED'] = '0'
        self.client = TestClient(app, base_url='http://127.0.0.1:8866')
        self.client.__enter__()
        self.assertFalse(self.client.get('/api/platform/v1/agent/health', headers=LOCAL).json()['enabled'])
        self.assertEqual(self.client.get('/api/platform/v1/workspaces?limit=50', headers=LOCAL).status_code, 200)
        self.assertEqual(self.client.get(f'/api/platform/v1/workspaces/{self.workspace}/assets', headers=LOCAL).status_code, 200)
        self.assertEqual(self.client.get('/api/platform/v1/data/health', headers=LOCAL).status_code, 200)

    def test_platform_skill_browsing_without_workspace_has_no_business_writes(self):
        path='/api/platform/v1/workspaces?limit=50'
        before=self.client.get(path,headers=LOCAL).json()
        records=app.state.agent.store.rows('SELECT COUNT(*) total FROM sessions')[0]['total']
        catalog=self.client.get('/api/platform/v1/agent/skills',headers=LOCAL)
        self.assertEqual(catalog.status_code,200,catalog.text)
        self.assertTrue(catalog.json()['items'])
        self.assertEqual(catalog.json()['active'],[])
        self.assertEqual(self.client.get(path,headers=LOCAL).json(),before)
        self.assertEqual(app.state.agent.store.rows('SELECT COUNT(*) total FROM sessions')[0]['total'],records)


if __name__ == '__main__':
    unittest.main(verbosity=2)
