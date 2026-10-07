"""Request consistency and cold replay on synthetic F-drive inputs only."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_data_tools as data_tests
from test_skills import skill_at
from workbench_platform.agent.environment import environment
from workbench_platform.agent.providers import ProviderFault, SYSTEM_PROMPTS
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.skills import SkillService
from workbench_platform.agent.store import Conflict, canonical


class RequestSnapshotTests(unittest.IsolatedAsyncioTestCase):
    setUp = data_tests.DataToolTests.setUp
    tearDown = data_tests.DataToolTests.tearDown

    def make_runtime(self, name='snapshot'):
        root = Path(self.temporary.name) / name
        runtime = AgentRuntime(root, self.services)
        config = {'protocol': 'openai_chat_completions', 'model': 'local-snapshot',
                  'revision': 1, 'context_cap': 16384, 'max_output_tokens': 2048,
                  'streaming': False}
        runtime.connection = SimpleNamespace(view=lambda: {'available': True, 'revision': 1},
            resolve=lambda revision: (config, 'synthetic-secret'), assert_current=lambda revision: None)
        self.services.agent = runtime
        return runtime

    def start(self, runtime):
        session = runtime.store.create_session('wtest', 'Snapshot', [], 'snapshot-session')
        runtime.tasks.record_goal(session['id'], '读取已注册工具')
        run, _ = runtime.store.start_run(session['id'], '读取已注册工具', 'snapshot-run', 1)
        return session, run, runtime.initial_state(run, '读取已注册工具')

    async def send(self, config, secret, payload, identity, **kwargs):
        self.wires.append(deepcopy(payload))
        return {'id': 'snapshot-reply', 'role': 'assistant', 'content': '',
                'calls': [{'call_id': 'snapshot-answer', 'tool': 'conversation_answer',
                           'arguments': {'content': '固定回复'}}],
                'usage': {'input_tokens': 12, 'output_tokens': 4}}

    def request_event(self, runtime):
        row = runtime.store.rows("SELECT * FROM events WHERE kind='model_request' ORDER BY seq DESC LIMIT 1")[0]
        return row, json.loads(row['payload'])

    async def test_one_source_scan_and_one_task_projection_survive_mid_scan_mutation(self):
        runtime = self.make_runtime()
        source = Path(self.temporary.name) / 'sources'
        directory = skill_at(source, allowed='data_read_table', body='原固定正文')
        runtime.skills = SkillService(runtime.store, [source], registry=runtime.registry)
        session, run, state = self.start(runtime)
        runtime.skills.activate('wtest', session['id'], 'sample-skill', 'activate', context=runtime.context(state))
        state['visible'] += ['data_read_table', 'jobs_get']
        original_scan = runtime.skills._scan
        def scan(workspace):
            value = original_scan(workspace)
            path = directory / 'SKILL.md'
            path.write_text(path.read_text(encoding='utf-8').replace('本地业务指导', '读取中发生的新描述') +
                            '\n未经升级的新正文。', encoding='utf-8')
            return value
        self.wires = []
        with patch.object(runtime.skills, '_scan', side_effect=scan) as scanned, \
                patch.object(runtime.tasks, 'get', wraps=runtime.tasks.get) as task_reads, \
                patch('workbench_platform.agent.runtime.environment', wraps=environment) as environment_reads, \
                patch('workbench_platform.agent.runtime.request', self.send):
            first = await runtime.model(state)
        self.assertEqual(scanned.call_count, 1)
        self.assertEqual(task_reads.call_count, 1)
        self.assertEqual(environment_reads.call_count, 1)
        system = self.wires[0]['messages'][0]['content']
        self.assertIn('原固定正文', system)
        self.assertIn('本地业务指导', system)
        self.assertNotIn('读取中发生的新描述', system)
        self.assertNotIn('未经升级的新正文', system)
        self.assertIn('data_read_table', first['selected_tools'])
        self.assertNotIn('jobs_get', first['selected_tools'])
        _, event = self.request_event(runtime)
        self.assertFalse(event['injection']['skills'][0]['source_changed'])
        self.assertNotIn('synthetic-secret', canonical(event))
        with patch('workbench_platform.agent.runtime.request', self.send):
            await runtime.model({**deepcopy(state), 'step': 1})
        self.assertIn('读取中发生的新描述', self.wires[1]['messages'][0]['content'])
        self.assertIn('原固定正文', self.wires[1]['messages'][0]['content'])
        self.assertNotIn('未经升级的新正文', self.wires[1]['messages'][0]['content'])
        self.assertTrue(self.request_event(runtime)[1]['injection']['skills'][0]['source_changed'])

    async def test_activation_upgrade_and_deactivation_are_visible_on_next_round(self):
        runtime = self.make_runtime()
        source = Path(self.temporary.name) / 'round-sources'
        directory = skill_at(source, allowed='data_read_table', body='版本一正文')
        runtime.skills = SkillService(runtime.store, [source], registry=runtime.registry)
        session, run, state = self.start(runtime)
        state['visible'] += ['data_read_table', 'jobs_get']
        self.wires = []
        with patch('workbench_platform.agent.runtime.request', self.send):
            first = await runtime.model(state)
            runtime.skills.activate('wtest', session['id'], 'sample-skill', 'activate', context=runtime.context(state))
            active = await runtime.model({**deepcopy(state), 'step': 1})
            path = directory / 'SKILL.md'
            path.write_text(path.read_text(encoding='utf-8').replace('data_read_table', 'jobs_get').replace('版本一正文', '版本二正文'), encoding='utf-8')
            new_version = runtime.skills.catalog('wtest')['items'][0]['version']
            runtime.skills.activate('wtest', session['id'], 'sample-skill', 'upgrade', version=new_version, context=runtime.context(state))
            upgraded = await runtime.model({**deepcopy(state), 'step': 2})
            runtime.skills.deactivate('wtest', session['id'], 'sample-skill', 'deactivate', context=runtime.context(state))
            inactive = await runtime.model({**deepcopy(state), 'step': 3})
        self.assertIn('jobs_get', first['selected_tools'])
        self.assertNotIn('jobs_get', active['selected_tools'])
        self.assertIn('版本一正文', self.wires[1]['messages'][0]['content'])
        self.assertIn('jobs_get', upgraded['selected_tools'])
        self.assertNotIn('data_read_table', upgraded['selected_tools'])
        self.assertIn('版本二正文', self.wires[2]['messages'][0]['content'])
        self.assertIn('data_read_table', inactive['selected_tools'])
        self.assertNotIn('<registered_skill', self.wires[3]['messages'][0]['content'])

    async def test_received_cold_replay_never_scans_live_sources_and_rejects_contract_change(self):
        runtime = self.make_runtime()
        session, run, state = self.start(runtime)
        self.wires = []
        with patch('workbench_platform.agent.runtime.request', self.send):
            first = await runtime.model(state)
            reopened = self.make_runtime()
            with patch.object(reopened.skills, '_scan', side_effect=AssertionError('recovery must not scan live source')):
                self.assertEqual(await reopened.model(deepcopy(state)), first)
                original = reopened.registry.tools['conversation_answer']
                reopened.registry.tools['conversation_answer'] = replace(original, version='new-contract')
                with self.assertRaises(Conflict):
                    await reopened.model(state)
                reopened.registry.tools['conversation_answer'] = original
                with self.assertRaises(Conflict):
                    await reopened.model({**deepcopy(state), 'messages': [{'role': 'user', 'content': '目标已变化'}]})
                reopened.tasks.save_profile('wtest', {'request_id': 'new-profile', 'expected_revision': 0,
                    'audit_object': '', 'period_start': '', 'period_end': '', 'objective': '', 'rules': '新口径'})
                with self.assertRaises(Conflict):
                    await reopened.model(state)
        self.assertEqual(len(self.wires), 1)
        self.assertEqual(len(reopened.store.rows('SELECT * FROM model_requests')), 1)

    async def test_unknown_replay_never_scans_or_resends_and_missing_input_stays_conflict(self):
        runtime = self.make_runtime()
        session, run, state = self.start(runtime)
        requests = []
        async def lost(*args, **kwargs):
            requests.append(args[3])
            raise ProviderFault('response_unknown', '本地合成回包丢失')
        with patch('workbench_platform.agent.runtime.request', lost):
            with self.assertRaises(ProviderFault):
                await runtime.model(state)
            reopened = self.make_runtime()
            with patch.object(reopened.skills, '_scan', side_effect=AssertionError('unknown must not rescan')):
                with self.assertRaises(ProviderFault) as caught:
                    await reopened.model(state)
                self.assertEqual(caught.exception.code, 'response_unknown')
                row, event = self.request_event(reopened)
                event.pop('input_state_version')
                with reopened.store.transaction() as db:
                    db.execute('UPDATE events SET payload=? WHERE seq=?', (canonical(event), row['seq']))
                with self.assertRaises(Conflict):
                    await reopened.model(state)
        self.assertEqual(len(requests), 1)
        self.assertEqual(reopened.store.rows('SELECT * FROM model_requests')[0]['state'], 'sent')

    async def test_legacy_received_prompt_has_exact_compatibility_without_weakening_inputs(self):
        runtime = self.make_runtime()
        session, run, state = self.start(runtime)
        self.wires = []
        old_system = SYSTEM_PROMPTS['clear-agent-system-v1']
        with patch('workbench_platform.agent.runtime.request', self.send):
            with patch('workbench_platform.agent.runtime.SYSTEM', old_system), \
                    patch('workbench_platform.agent.runtime.SYSTEM_VERSION', 'clear-agent-system-v1'):
                first = await runtime.model(state)
            reopened = self.make_runtime()
            self.assertEqual(await reopened.model(state), first)
            current_system = SYSTEM_PROMPTS['clear-agent-system-v2']
            with patch('workbench_platform.agent.runtime.SYSTEM', current_system + '未经登记的新执行契约'):
                with self.assertRaises(Conflict):
                    await reopened.model(state)
            row, event = self.request_event(reopened)
            event.pop('request_snapshot')  # Exact pre-SC05 legacy event format.
            with reopened.store.transaction() as db:
                db.execute('UPDATE events SET payload=? WHERE seq=?', (canonical(event), row['seq']))
            self.assertEqual(await reopened.model(state), first)
            with self.assertRaises(Conflict):
                await reopened.model({**deepcopy(state), 'visible': [*state['visible'], 'jobs_get']})
            event['request_snapshot'] = {'format': 'clear-model-input-v1', 'system_version': 'unknown-future-version'}
            with reopened.store.transaction() as db:
                db.execute('UPDATE events SET payload=? WHERE seq=?', (canonical(event), row['seq']))
            with self.assertRaises(Conflict):
                await reopened.model(state)
        self.assertEqual(len(self.wires), 1)


if __name__ == '__main__':
    unittest.main()
