"""Task-center continuation resolves real archived credentials without changing defaults."""
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx

from workbench_platform.agent.connection import ControllerConnection
from workbench_platform.agent.store import AgentStore
from workbench_platform.tasks.store import LedgerError
from workbench_platform.tasks.worker import ExecutionHost


ROOT = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence/task-connection-tests')


async def local_probe(*args, **kwargs):
    return {'native_tools': True, 'business_data_sent': False}


class TaskConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.store = AgentStore(self.root / 'agent')
        self.connection = ControllerConnection(self.store, production_preflight=lambda config: {'passed': True})
        with patch('workbench_platform.agent.connection.probe', local_probe):
            first = await self.connection.save(self.draft('first-model', 'first-secret'))
            self.revision = first['revision']
            await self.connection.save(self.draft('second-model', 'second-secret'))
        self.host = ExecutionHost.__new__(ExecutionHost)
        self.host.root = self.root
        self.host.legacy = SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(connection=self.connection)))
        self.run = {'id': 'original-run', 'connection_revision': self.revision, 'status': 'paused'}
        self.binding = ['agent.runs', self.run['id'], '/api/platform/v1/agent/runs/' + self.run['id']]
        # Only task-ledger receipt lookup is stubbed; the configuration store,
        # archived revision and encrypted credential reads below are real.
        result = SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: {'receipt': {'binding': self.binding}}))
        self.host.engine = SimpleNamespace(connect=lambda: nullcontext(SimpleNamespace(execute=lambda query: result)))
        self.command = {'tool_id': 'agent', 'path': '/api/platform/v1/agent/sessions/sessionOwned/messages',
                        'method': 'POST', 'retry_of': 'prior-ledger-run', 'resolved_ai_service': {'id': 'old-profile'}}
        async def call(method, path, payload=None, step=None):
            return httpx.Response(200, json={'runs': [self.run]} if '/sessions/' in path else self.run)
        self.host.call = call

    async def asyncTearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def draft(model, secret):
        return {'protocol': 'openai_chat_completions', 'base_url': 'http://127.0.0.1:54321/v1',
                'model': model, 'api_key': secret, 'context_cap': 65536,
                'max_output_tokens': 2048, 'streaming': False}

    async def test_retry_and_execute_profile_use_archive_and_leave_current_default(self):
        before = self.connection._read()
        with patch('workbench_platform.tasks.ai_services.activate', side_effect=AssertionError('Old profile activated')):
            profile = await self.host.agent_request_profile(self.command, {'ai_service_profile_id': 'old-profile'})
            method, path, payload = await self.host.retry_command({'run_id': 'retry'}, self.command)
        self.assertEqual(profile['model'], 'first-model')
        self.assertEqual((method, path, payload), ('POST', self.binding[2] + '/resume', {}))
        self.assertEqual(self.connection._read(), before)
        self.assertEqual(self.connection.resolve(self.revision)[1], 'first-secret')
        self.assertNotIn('secret', json.dumps(profile))

    async def test_active_session_continuation_uses_original_connection(self):
        command = {key: value for key, value in self.command.items() if key != 'retry_of'}
        with patch('workbench_platform.tasks.ai_services.activate', side_effect=AssertionError('Old profile activated')):
            profile = await self.host.agent_request_profile(command, {'ai_service_profile_id': 'old-profile'})
        self.assertEqual(profile['model'], 'first-model')
        self.assertEqual(self.connection._read()['model'], 'second-model')

    async def test_deletion_revokes_archive_even_after_new_default_is_saved(self):
        self.connection.clear()
        with self.assertRaises(LedgerError):
            await self.host.agent_request_profile(self.command, {})
        with patch('workbench_platform.agent.connection.probe', local_probe):
            await self.connection.save(self.draft('third-model', 'third-secret'))
        with self.assertRaises(LedgerError):
            await self.host.retry_command({'run_id': 'retry'}, self.command)

    async def test_missing_archived_credential_blocks_continuation(self):
        archived = json.loads(self.store.rows('SELECT value FROM settings WHERE key=?',
            ('agent-controller-revision:' + str(self.revision),))[0]['value'])
        self.connection.vault.delete(archived['credential_ref'])
        with self.assertRaises(LedgerError):
            await self.host.agent_request_profile(self.command, {})

    async def test_new_run_still_checks_selected_current_profile(self):
        async def call(*args, **kwargs):
            return httpx.Response(200, json={'runs': []})
        self.host.call = call
        command = {key: value for key, value in self.command.items() if key != 'retry_of'}
        with patch('workbench_platform.tasks.ai_services.activate', return_value={'id': 'current-profile'}) as activate:
            profile = await self.host.agent_request_profile(command, {'ai_service_profile_id': 'current-profile'})
        self.assertEqual(profile['id'], 'current-profile')
        activate.assert_called_once_with(self.host.engine, self.host.legacy, self.root, 'current-profile')

    async def test_legacy_runtime_without_resolver_is_rejected_explicitly(self):
        self.host.legacy.state.agent.connection = SimpleNamespace(_read=lambda: {'revision': self.revision})
        with self.assertRaisesRegex(LedgerError, '原连接版本不能核实'):
            await self.host.agent_request_profile(self.command, {})

    async def test_unknown_native_run_response_does_not_fall_back_to_default(self):
        async def call(*args, **kwargs):
            return httpx.Response(404, json={'detail': 'missing'})
        self.host.call = call
        with patch('workbench_platform.tasks.ai_services.activate', side_effect=AssertionError('Default fallback')):
            with self.assertRaisesRegex(LedgerError, '无法核实'):
                await self.host.agent_request_profile(self.command, {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
