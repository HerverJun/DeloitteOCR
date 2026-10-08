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
from workbench_platform.agent.runtime import AgentRuntime, CURRENT_FACTS_PREFIX
from workbench_platform.agent.skills import SkillService
from workbench_platform.agent.store import Conflict, canonical, digest


LEGACY_FACTS_PREFIX = '[服务器工作事实；用户目标/规则是任务约束，操作状态为历史回执；继续前查询当前状态]\n'


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

    def start(self, runtime, goal='读取已注册工具'):
        session = runtime.store.create_session('wtest', 'Snapshot', [], 'snapshot-session')
        runtime.tasks.record_goal(session['id'], goal)
        run, _ = runtime.store.start_run(session['id'], goal, 'snapshot-run', 1)
        return session, run, runtime.initial_state(run, goal)

    async def send(self, config, secret, payload, identity, **kwargs):
        self.wires.append(deepcopy(payload))
        return {'id': 'snapshot-reply', 'role': 'assistant', 'content': '',
                'calls': [{'call_id': 'snapshot-answer', 'tool': 'conversation_answer',
                           'arguments': {'content': '固定回复'}}],
                'usage': {'input_tokens': 12, 'output_tokens': 4}}

    def request_event(self, runtime):
        row = runtime.store.rows("SELECT * FROM events WHERE kind='model_request' ORDER BY seq DESC LIMIT 1")[0]
        return row, json.loads(row['payload'])

    def legacy_facts_digest(self):
        """Serialize the pre-prefix host contract when the original request is sent."""
        recorded = {'contracts': [], 'inputs': []}
        def old_digest(value):
            value = deepcopy(value)
            if isinstance(value, dict) and 'host_contract' in value:
                value['host_contract'].pop('facts_message_prefix', None)
                recorded['inputs'].append(deepcopy(value))
            elif isinstance(value, dict) and {
                    'system', 'environment', 'tool_schemas', 'tool_versions'} <= value.keys():
                value.pop('facts_message_prefix', None)
                recorded['contracts'].append(deepcopy(value))
            return digest(value)
        return old_digest, recorded

    async def assert_legacy_input_changes_conflict(self, runtime, state, directory, skill_name='sample-skill'):
        with patch.object(runtime.skills, '_scan', side_effect=AssertionError('recovery must not scan sources')):
            for field, value in [
                    ('messages', [*state['messages'], {'role': 'user', 'content': '原目标已改变'}]),
                    ('visible', [*state['visible'], 'jobs_get']),
                    ('protected_tools', ['jobs_get'])]:
                with self.subTest(field=field), self.assertRaises(Conflict):
                    await runtime.model({**deepcopy(state), field: value})
            original = runtime.registry.tools['conversation_answer']
            try:
                runtime.registry.tools['conversation_answer'] = replace(original, version='changed-legacy-contract')
                with self.assertRaises(Conflict):
                    await runtime.model(deepcopy(state))
            finally:
                runtime.registry.tools['conversation_answer'] = original
            profile = runtime.tasks.profile('wtest')
            with patch.object(runtime.tasks, 'profile', return_value={**profile, 'rules': '已改变的审计规则'}):
                with self.assertRaises(Conflict):
                    await runtime.model(deepcopy(state))

        fixed = runtime.skills.request_snapshot('wtest', state['session_id'], scan_sources=False).active[0]
        path = directory / 'SKILL.md'
        path.write_text(path.read_text(encoding='utf-8').replace('固定技能版本一', '固定技能版本二'),
                        encoding='utf-8')
        upgraded = runtime.skills.catalog('wtest', state['session_id'])['items'][0]['version']
        self.assertNotEqual(upgraded, fixed['active_version'])
        runtime.skills.activate('wtest', state['session_id'], skill_name, 'legacy-upgrade',
                                version=upgraded, context=runtime.context(state))
        with patch.object(runtime.skills, '_scan', side_effect=AssertionError('recovery must not scan sources')):
            with self.assertRaises(Conflict):
                await runtime.model(deepcopy(state))

    async def assert_legacy_facts_contract_recovery(self, *, unknown):
        name = 'legacy-facts-unknown' if unknown else 'legacy-facts-received'
        source = Path(self.temporary.name) / (name + '-skills')
        directory = skill_at(source, allowed='data_read_table', body='固定技能版本一')
        runtime = self.make_runtime(name)
        runtime.skills = SkillService(runtime.store, [source], registry=runtime.registry)
        session, run, state = self.start(runtime)
        runtime.skills.activate('wtest', session['id'], 'sample-skill', 'legacy-activate',
                                context=runtime.context(state))
        state['visible'] += ['data_read_table']
        self.wires, sent = [], []
        async def transport(config, secret, payload, identity, **kwargs):
            sent.append(identity)
            if unknown:
                self.wires.append(deepcopy(payload))
                raise ProviderFault('response_unknown', '旧契约的合成回包丢失')
            return await self.send(config, secret, payload, identity, **kwargs)

        old_digest, recorded = self.legacy_facts_digest()
        original_event_handler = runtime.event
        def old_event(checkpoint, identity, kind, payload):
            payload = deepcopy(payload)
            if kind == 'model_request':
                payload['request_snapshot'].pop('task_matches', None)
            return original_event_handler(checkpoint, identity, kind, payload)
        with patch('workbench_platform.agent.runtime.request', transport):
            # Produce the historical payload and hashes before transmission;
            # the persisted request event is never rewritten to emulate age.
            with patch('workbench_platform.agent.runtime.CURRENT_FACTS_PREFIX', LEGACY_FACTS_PREFIX), \
                    patch('workbench_platform.agent.runtime.digest', old_digest), \
                    patch.object(runtime, 'event', side_effect=old_event):
                if unknown:
                    with self.assertRaises(ProviderFault) as caught:
                        await runtime.model(state)
                    self.assertEqual(caught.exception.code, 'response_unknown')
                    first = None
                else:
                    first = await runtime.model(state)
            original_row, original_event = self.request_event(runtime)
            original_models = runtime.store.rows('SELECT * FROM model_requests')
            old_contract = recorded['contracts'][-1]
            self.assertNotIn('facts_message_prefix', old_contract)
            self.assertNotIn('task_matches', original_event['request_snapshot'])
            self.assertEqual(original_event['request_snapshot']['host_contract_version'], digest(old_contract))
            self.assertEqual(original_event['input_state_version'], digest(recorded['inputs'][-1]))
            self.assertNotEqual(LEGACY_FACTS_PREFIX, CURRENT_FACTS_PREFIX)
            self.assertIn({'role': 'user', 'content': LEGACY_FACTS_PREFIX + canonical(recorded['inputs'][-1]['facts'])},
                          self.wires[0]['messages'])

            reopened = self.make_runtime(name)
            reopened.skills = SkillService(reopened.store, [source], registry=reopened.registry)
            with patch.object(reopened.skills, '_scan', side_effect=AssertionError('cold recovery must not scan')):
                for attempt in range(2):
                    with self.subTest(attempt=attempt):
                        if unknown:
                            with self.assertRaises(ProviderFault) as caught:
                                await reopened.model(deepcopy(state))
                            self.assertEqual(caught.exception.code, 'response_unknown')
                        else:
                            self.assertEqual(await reopened.model(deepcopy(state)), first)
            await self.assert_legacy_input_changes_conflict(reopened, state, directory)

        self.assertEqual(len(sent), 1)
        self.assertEqual(len(self.wires), 1)
        self.assertEqual(reopened.store.rows('SELECT * FROM model_requests'), original_models)
        self.assertEqual(original_models[0]['state'], 'sent' if unknown else 'received')
        self.assertEqual(self.request_event(reopened), (original_row, original_event))
        self.assertEqual(len(reopened.store.rows("SELECT * FROM events WHERE kind='model_request'")), 1)

    async def test_pre_prefix_received_request_recovers_original_reply_without_weakening_inputs(self):
        await self.assert_legacy_facts_contract_recovery(unknown=False)

    async def test_pre_prefix_unknown_request_never_resends_or_accepts_changed_inputs(self):
        await self.assert_legacy_facts_contract_recovery(unknown=True)

    async def matched_source_recovery(self, *, unknown):
        name = 'matched-unknown' if unknown else 'matched-received'
        source = Path(self.temporary.name) / (name + '-skills')
        directory = skill_at(source, name='payment-reconciliation', allowed='', body='固定技能版本一')
        runtime = self.make_runtime(name)
        runtime.skills = SkillService(runtime.store, [source], registry=runtime.registry)
        session, run, state = self.start(runtime, '请核对两份数据并导出差异')
        self.wires = []
        async def transport(config, secret, payload, identity, **kwargs):
            if unknown:
                self.wires.append(deepcopy(payload))
                raise ProviderFault('response_unknown', '已匹配任务的合成回包丢失')
            return await self.send(config, secret, payload, identity, **kwargs)
        with patch('workbench_platform.agent.runtime.request', transport):
            if unknown:
                with self.assertRaises(ProviderFault) as caught:
                    await runtime.model(state)
                self.assertEqual(caught.exception.code, 'response_unknown')
                first = None
            else:
                first = await runtime.model(state)
            original_event = self.request_event(runtime)
            original_models = runtime.store.rows('SELECT * FROM model_requests')
            projection = original_event[1]['request_snapshot']['task_matches']
            self.assertEqual(projection['match'], 'single')
            self.assertEqual(projection['workflows'][0]['skill_state'], 'compatible')
            self.assertEqual(projection['workflows'][0]['suggested_skill'], 'payment-reconciliation')
            skill = directory / 'SKILL.md'
            skill.write_text(skill.read_text('utf8').replace('本地业务指导', '未升级的新描述'), encoding='utf8')
            reopened = self.make_runtime(name)
            reopened.skills = SkillService(reopened.store, [source], registry=reopened.registry)
            with patch.object(reopened.skills, '_scan', side_effect=AssertionError('matched recovery must not scan')):
                if unknown:
                    with self.assertRaises(ProviderFault) as caught:
                        await reopened.model(deepcopy(state))
                    self.assertEqual(caught.exception.code, 'response_unknown')
                else:
                    self.assertEqual(await reopened.model(deepcopy(state)), first)
            await self.assert_legacy_input_changes_conflict(reopened, state, directory,
                                                           skill_name='payment-reconciliation')
        self.assertEqual(len(self.wires), 1)
        self.assertEqual(reopened.store.rows('SELECT * FROM model_requests'), original_models)
        self.assertEqual(self.request_event(reopened), original_event)

    async def test_matched_received_task_uses_bound_source_projection_after_source_changes(self):
        await self.matched_source_recovery(unknown=False)

    async def test_matched_unknown_task_uses_bound_source_projection_without_resending(self):
        await self.matched_source_recovery(unknown=True)

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
