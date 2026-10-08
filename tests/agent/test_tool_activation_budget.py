"""CPU admission contracts; a checkpoint fixture is not a real controller run."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from workbench_platform.agent.context import core_tools, prepare_request, skill_control_tools
from workbench_platform.agent.environment import compose, environment
from workbench_platform.agent.providers import SYSTEM
from workbench_platform.agent.providers import ProviderFault
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import canonical, digest
from workbench_platform.agent.tools.context import ToolDescribe, describe_tools
from workbench_platform.data_api.service import DataWorkbenchService
from workbench_platform.store import WorkspaceStore

ROOT = Path('F:/OCR/output/platform-specialized-agent-20261007/temp/tool-activation-contracts')
DATA_PYTHON = Path('F:/OCR/output/clear-automation-dev-20261002/program/runtimes/data/python.exe')


class ToolActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self.temporary.name)
        workspaces = WorkspaceStore(root / 'workspaces.sqlite3')
        workspaces.create_workspace('CPU activation contract', workspace_id='wtest')
        self.data = DataWorkbenchService(root / 'data', DATA_PYTHON)
        self.services = SimpleNamespace(workspace_store=workspaces, data_workbench=self.data)
        self.runtime = AgentRuntime(root / 'agent', self.services)
        self.services.agent = self.runtime
        self.config = {'protocol': 'openai_chat_completions', 'model': 'unknown-test-controller',
            'revision': 1, 'context_cap': 12000, 'max_output_tokens': 2048, 'streaming': False}
        self.runtime.connection = SimpleNamespace(resolve=lambda revision: (self.config, 'unused-local-fixture'))
        self.session = self.runtime.store.create_session('wtest', 'CPU admission', [], 'activation-session')
        self.run, _ = self.runtime.store.start_run(self.session['id'], 'Load registered tools for the explicit task',
                                                 'activation-run', 1)
        self.state = self.runtime.initial_state(self.run, 'Load registered tools for the explicit task')

    async def asyncTearDown(self):
        await self.runtime.close()
        self.data.close()
        self.temporary.cleanup()

    async def call(self, names, *, extra_call=False):
        if self.state.get('calls') and hasattr(self, 'result'):
            previous_id = self.state['calls'][0]['call_id']
            self.state['messages'].append({'role': 'tool', 'call_id': previous_id, 'result': self.result})
        call_id = 'load-contracts' if not hasattr(self, 'result') else 'load-contracts-next'
        call = {'call_id': call_id, 'tool': 'tools_describe', 'arguments': {'names': names}}
        self.state['calls'] = [call]
        self.state['visible'] = core_tools(self.runtime.registry)
        self.state['messages'].append({'role': 'assistant', 'protocol': 'openai_chat_completions',
                                      'calls': [call], 'content': '', 'private': {}})
        if extra_call:
            extra = {'call_id': 'other-pending', 'tool': 'audit_get_task', 'arguments': {}}
            self.state['calls'].append(extra)
            self.state['messages'][-1]['calls'].append(extra)
        async def snapshot(options):
            saved = deepcopy(self.state)
            if hasattr(self, 'forced_checkpoint_scope'):
                saved['workspace_id'] = self.forced_checkpoint_scope
            return SimpleNamespace(values=saved)
        self.runtime.graph = SimpleNamespace(aget_state=snapshot)
        self.result = await self.runtime.registry.dispatch(call, self.runtime.context(self.state))
        self.assertEqual(self.runtime.store.rows('SELECT id FROM model_requests'), [])
        return self.result

    def next_request(self, result):
        scope = self.runtime.skills.request_snapshot('wtest', self.session['id'])
        task = self.runtime.tasks.get(self.session['id'])
        values = environment(self.config, workspace_id='wtest', session_id=self.session['id'],
            scope_revision=task['scope_revision'], manual=task['manual'],
            families=(name.split('_', 1)[0] for name in self.runtime.registry.tools))
        system, _, sections = compose(SYSTEM, self.config, environment_values=values,
            skill_prompt='[登记技能精简名录；正文按需启用]\n' + canonical(scope.catalog) + '\n' + scope.instructions)
        messages = [message for message in self.state['messages']
                    if not message.get('id', '').endswith((':session-facts', ':current-facts'))]
        messages.insert(0, {'id': self.run['id'] + ':current-facts', 'role': 'user',
            'content': '[服务器工作事实；用户目标/规则是任务约束，操作状态为历史回执；继续前查询当前状态]\n' +
                       canonical(self.runtime.session_facts(self.run, task=task))})
        messages.append({'role': 'tool', 'call_id': self.state['calls'][0]['call_id'], 'result': result})
        activated = result['data']['activated_tools']
        allowed = scope.allowed_tools(self.state['visible'], core=skill_control_tools(self.runtime.registry))
        visible = [name for name in self.state['visible'] if name in allowed]
        return prepare_request(self.config, messages, self.runtime.registry, [*visible, *activated],
                               protected=activated, system=system, system_components=sections)

    async def test_activation_budget_matches_next_native_schemas_and_preserves_all_requested_names(self):
        names = ['data_inspect_input', 'data_create_session', 'data_import', 'data_read_table',
                 'data_compare', 'data_read_result', 'data_export', 'audit_bind_evidence', 'audit_read_evidence']
        result = await self.call(names)
        self.assertEqual(result['data']['requested_tools'], names)
        self.assertEqual([item['name'] for item in result['data']['tools']], names)
        loaded = result['data']['activated_tools']
        self.assertTrue(loaded)
        self.assertLess(len(loaded), len(names))
        payload, metrics, selected = self.next_request(result)
        self.assertLessEqual(metrics['input_tokens_estimate'], metrics['available_input_tokens'])
        self.assertTrue(set(loaded) <= set(selected))
        native = {entry['function']['name']: entry['function']['parameters'] for entry in payload['tools']}
        for item in result['data']['tools']:
            self.assertEqual(item['schema_sha256'], digest(self.runtime.registry.schemas([item['name']])[item['name']]))
            self.assertEqual(item['loaded'], item['name'] in loaded)
            if item['loaded']:
                schema = self.runtime.registry.schemas([item['name']])[item['name']]
                self.assertEqual(native[item['name']], {key: value for key, value in schema.items() if key != 'description'})
            else:
                self.assertEqual(item['reason'], 'context_budget')

    async def test_mixed_skill_deny_is_explicit_and_does_not_claim_any_activation(self):
        ctx = self.runtime.context(self.state)
        self.runtime.skills.activate('wtest', self.session['id'], 'payment-reconciliation', 'activate-payment', context=ctx)
        result = await self.call(['data_import', 'native_attach_result'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['data']['activated_tools'], [])
        by_name = {item['name']: item for item in result['data']['tools']}
        self.assertTrue(by_name['data_import']['allowed'])
        self.assertFalse(by_name['native_attach_result']['allowed'])
        self.assertEqual(by_name['native_attach_result']['error']['code'], 'skill_tool_denied')
        self.assertIn('payment-reconciliation', [item['name'] for item in self.runtime.skills.request_snapshot('wtest', self.session['id']).active])

    async def test_actual_third_load_scope_rejection_fits_without_removing_unique_source_facts(self):
        captured = json.loads((Path(__file__).parent / 'fixtures/s07-completed-tool-load.json').read_text('utf8'))
        self.runtime.registry.tools = {name: tool for name, tool in self.runtime.registry.tools.items()
                                       if name in captured['registered_tools']}
        self.runtime.skills.activate('wtest', self.session['id'], 'payment-reconciliation',
                                     'activate-payment', context=self.runtime.context(self.state))
        original = deepcopy(captured['messages'])
        described = original[-1]['result']['data']
        result = await self.runtime.admit_tools(self.runtime.context(self.state),
                                               described['activated_tools'], described['tools'])
        self.assertEqual(result['data']['activated_tools'], [])
        self.assertEqual(result['error']['code'], 'skill_tool_denied')
        messages = deepcopy(original); messages[-1]['result'] = result
        newly_requested = set(described['activated_tools'])
        visible = [name for name in captured['visible'] if name not in newly_requested]
        payload, metrics, selected = prepare_request(captured['config'], messages, self.runtime.registry, visible,
            system=captured['system'], system_components=captured['sections'])
        self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
        self.assertIn('skills_deactivate', selected)
        self.assertEqual(captured['messages'], original)
        summary = next(message['content'] for message in payload['messages']
            if message.get('content', '').startswith('[服务器保存的工具事实'))
        facts = json.loads(summary.split('\n', 1)[1])
        by_id = {item['call_id']: item for item in facts}
        for message in messages:
            if message['role'] == 'tool':
                self.assertIn(message['call_id'], by_id)
                if by_id[message['call_id']]['tool'] == 'skills_read_resource':
                    self.assertEqual(by_id[message['call_id']]['data'], message['result']['data'])
        goal = next(message['content'] for message in messages if message.get('id') == 's07RealNativeAgentGoal')
        self.assertIn(goal, [message.get('content') for message in payload['messages']])

    async def test_unknown_tool_is_reported_without_loading_known_siblings(self):
        result = await self.call(['data_import', 'unregistered_business_tool'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['data']['activated_tools'], [])
        self.assertEqual(result['data']['requested_tools'], ['data_import', 'unregistered_business_tool'])
        self.assertEqual(result['data']['tools'][1]['error']['code'], 'unknown_tool')

    async def test_actual_skill_exit_fits_and_preserves_denied_tools_without_granting_contracts(self):
        captured = json.loads((Path(__file__).parent / 'fixtures/s07-completed-skill-exit.json').read_text('utf8'))
        self.runtime.registry.tools = {name: tool for name, tool in self.runtime.registry.tools.items()
                                       if name in captured['registered_tools']}
        original = deepcopy(captured['messages'])
        payload, metrics, selected = prepare_request(captured['config'], original, self.runtime.registry,
            captured['visible'], protected=captured['protected'], system=captured['system'],
            system_components=captured['sections'])
        self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
        self.assertIn('tools_describe', selected)
        self.assertNotIn('native_attach_result', selected)
        self.assertEqual(original, captured['messages'])
        facts = json.loads(next(message['content'] for message in payload['messages']
            if message.get('content', '').startswith('[服务器保存的工具事实')).split('\n', 1)[1])
        by_id = {item['call_id']: item for item in facts}
        for message in original:
            if message['role'] == 'tool':
                self.assertIn(message['call_id'], by_id)
                if by_id[message['call_id']]['tool'] == 'skills_read_resource':
                    self.assertEqual(by_id[message['call_id']]['data'], message['result']['data'])
                if by_id[message['call_id']]['tool'] == 'tools_describe':
                    self.assertEqual(by_id[message['call_id']]['data']['activated_tools'], [])
                    denied = {item['name']: item for item in by_id[message['call_id']]['data']['tools']}
                    self.assertFalse(denied['native_attach_result']['allowed'])
                    self.assertFalse(denied['native_attach_result']['loaded'])
                    self.assertEqual(denied['native_attach_result']['error']['code'], 'skill_tool_denied')
        goal = next(message['content'] for message in original if message.get('id') == 's07RealNativeAgentGoal')
        self.assertIn(goal, [message.get('content') for message in payload['messages']])

    async def test_only_explicit_skill_deactivation_changes_denied_permission(self):
        ctx = self.runtime.context(self.state)
        self.runtime.skills.activate('wtest', self.session['id'], 'payment-reconciliation', 'activate-payment', context=ctx)
        blocked = await self.call(['native_attach_result'])
        self.assertFalse(blocked['data']['tools'][0]['allowed'])
        self.runtime.skills.deactivate('wtest', self.session['id'], 'payment-reconciliation', 'deactivate-payment', context=ctx)
        resumed = await self.call(['native_attach_result'])
        self.assertTrue(resumed['data']['tools'][0]['allowed'])
        self.assertFalse(resumed['data']['tools'][0]['loaded'])
        self.assertEqual(resumed['data']['tools'][0]['reason'], 'service_unavailable')

    async def test_loaded_schema_drift_missing_identity_or_false_permission_stops_before_model_request(self):
        result = await self.call(['data_import'])
        self.assertEqual(result['data']['activated_tools'], ['data_import'])
        for field, replacement in (('schema_sha256', '0' * 64), ('schema_sha256', None), ('allowed', False), ('loaded', False)):
            with self.subTest(field=field, replacement=replacement):
                invalid = deepcopy(result); invalid['data']['tools'][0][field] = replacement
                with self.assertRaises(ProviderFault) as caught:
                    self.next_request(invalid)
                self.assertIn(caught.exception.code, {'tool_contract_changed', 'tool_activation_invalid'})
        spec = self.runtime.registry.tools['data_import']
        self.runtime.registry.tools['data_import'] = replace(spec, description=spec.description + ' Changed contract.')
        with self.assertRaises(ProviderFault) as caught:
            self.next_request(result)
        self.assertEqual(caught.exception.code, 'tool_contract_changed')
        self.assertEqual(self.runtime.store.rows('SELECT id FROM model_requests'), [])

    async def test_unavailable_service_does_not_claim_loaded_contract(self):
        result = await self.call(['native_attach_result'])
        self.assertEqual(result['data']['activated_tools'], [])
        self.assertTrue(result['data']['tools'][0]['allowed'])
        self.assertEqual(result['data']['tools'][0]['reason'], 'service_unavailable')

    async def test_requested_business_contract_is_admitted_before_repeated_core_reads(self):
        names = ['audit_get_task', 'audit_get_project', 'data_create_session']
        result = await self.call(names)
        self.assertEqual(result['data']['requested_tools'], names)
        self.assertEqual([item['name'] for item in result['data']['tools']], names)
        self.assertEqual(result['data']['activated_tools'][0], 'data_create_session')
        payload, metrics, selected = self.next_request(result)
        self.assertLessEqual(metrics['input_tokens_estimate'], metrics['available_input_tokens'])
        self.assertIn('data_create_session', selected)

    async def test_future_tools_in_same_batch_cannot_be_predicted_as_completed_receipts(self):
        result = await self.call(['data_import'], extra_call=True)
        self.assertEqual(result['data']['activated_tools'], [])
        self.assertEqual(result['data']['tools'][0]['reason'], 'load_in_separate_tool_batch')

    async def test_wrong_checkpoint_workspace_never_claims_activation(self):
        self.forced_checkpoint_scope = 'wother'
        result = await self.call(['data_import'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], 'revision_conflict')
        self.assertFalse(result['data'].get('activated_tools'))

    async def test_without_runtime_budget_no_tool_is_reported_as_loaded(self):
        ctx = SimpleNamespace(services=SimpleNamespace(agent=SimpleNamespace(registry=self.runtime.registry),
                                                       data_workbench=self.data))
        result = await describe_tools(ToolDescribe(names=['data_import']), ctx)
        self.assertEqual(result['data']['activated_tools'], [])
        self.assertEqual(result['error']['code'], 'activation_context_unavailable')

    async def test_original_tool_receipt_is_pageable_by_exact_call_id_and_confined_to_session(self):
        result = await self.call(['data_import'])
        call_id = self.state['calls'][0]['call_id']
        pieces = []; offset = 0; proof = None
        while True:
            read = await self.runtime.registry.dispatch({'call_id': 'receipt-read-' + str(offset),
                'tool': 'session_read_history', 'arguments': {'call_id': call_id, 'offset': offset, 'chars': 173}},
                self.runtime.context(self.state))
            self.assertEqual(read['status'], 'success')
            data = read['data']; pieces.append(data['content'])
            self.assertEqual(data['result_sha256'], digest(result))
            if proof is not None:
                self.assertEqual(data['content_sha256'], proof)
            proof = data['content_sha256']
            if data['next_offset'] is None:
                break
            offset = data['next_offset']
        original = json.loads(''.join(pieces))
        self.assertEqual(original['result'], result)
        self.assertEqual(original['arguments'], {'names': ['data_import']})
        self.runtime.store.status(self.run['id'], self.run['generation'], 'succeeded')
        other_session = self.runtime.store.create_session('wtest', 'Other session', [], 'other-session')
        other_run, _ = self.runtime.store.start_run(other_session['id'], 'Other scope', 'other-run', 1)
        other_state = self.runtime.initial_state(other_run, 'Other scope')
        denied = await self.runtime.registry.dispatch({'call_id': 'other-read', 'tool': 'session_read_history',
            'arguments': {'call_id': call_id, 'run_id': self.run['id']}}, self.runtime.context(other_state))
        self.assertEqual(denied['status'], 'error')
        self.assertNotIn('content', denied.get('data', {}))

    async def test_actual_larger_denied_discovery_receipt_fits_with_immutable_readback_proof(self):
        captured = json.loads((Path(__file__).parent / 'fixtures/s07-larger-denied-load.json').read_text('utf8'))
        self.runtime.registry.tools = {name: tool for name, tool in self.runtime.registry.tools.items()
                                       if name in captured['registered_tools']}
        original = deepcopy(captured['messages'])
        payload, metrics, selected = prepare_request(captured['config'], original, self.runtime.registry,
            captured['visible'], protected=captured['protected'], system=captured['system'],
            system_components=captured['sections'])
        self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
        self.assertIn('tools_describe', selected)
        self.assertIn('session_read_history', selected)
        self.assertNotIn('native_attach_result', selected)
        summary = next(message['content'] for message in payload['messages']
            if message.get('content', '').startswith('[服务器保存的工具事实'))
        facts = json.loads(summary.split('\n', 1)[1])
        by_id = {item['call_id']: item for item in facts}
        for message in original:
            if message['role'] == 'tool':
                fact = by_id[message['call_id']]
                if fact['tool'] in {'tools_list', 'tools_describe'}:
                    self.assertEqual(fact['data']['archived_receipt_sha256'], digest(message['result']))
                    self.assertEqual(fact['call_id'], message['call_id'])
                    self.assertIn('每项call_id用session_read_history精确回读', summary.split('\n',1)[0])
                    if fact['tool'] == 'tools_describe':
                        self.assertEqual(fact['data']['requested_tools_ref'], 'arguments.names')
                        self.assertEqual(fact['arguments']['names'], message['result']['data']['requested_tools'])
                elif fact['tool'] == 'skills_read_resource':
                    self.assertEqual(fact['data'], message['result']['data'])
        self.assertEqual(original, captured['messages'])
        goal = next(message['content'] for message in original if message.get('id') == 's07RealNativeAgentGoal')
        self.assertIn(goal, [message.get('content') for message in payload['messages']])

    async def test_completed_read_phase_reclaims_reloadable_core_and_keeps_activation_decisions(self):
        captured = json.loads((Path(__file__).parent / 'fixtures/s07-completed-read-after-load.json').read_text('utf8'))
        self.runtime.registry.tools = {name: tool for name, tool in self.runtime.registry.tools.items()
                                       if name in captured['registered_tools']}
        original = deepcopy(captured['messages'])
        payload, metrics, selected = prepare_request(captured['config'], original, self.runtime.registry,
            captured['visible'], protected=captured['protected'], system=captured['system'],
            system_components=captured['sections'])
        self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
        self.assertIn('tools_describe', selected)
        self.assertIn('session_read_history', selected)
        self.assertNotIn('models_list', selected)
        facts = json.loads(next(message['content'] for message in payload['messages']
            if message.get('content', '').startswith('[服务器保存的工具事实')).split('\n', 1)[1])
        by_id = {item['call_id']: item for item in facts}
        calls = {call['call_id']: call for message in original for call in message.get('calls', [])}
        for message in original:
            if message['role'] == 'tool':
                fact = by_id[message['call_id']]
                if calls[message['call_id']]['tool'] == 'tools_describe':
                    self.assertEqual(fact['data']['activated_tools'], message['result']['data']['activated_tools'])
                    by_name = {item['name']: item for item in fact['data']['tools']}
                    for item in message['result']['data']['tools']:
                        for field in ('allowed','loaded','error','reason'):
                            self.assertEqual(by_name[item['name']].get(field), item.get(field))
                elif calls[message['call_id']]['tool'] == 'skills_read_resource':
                    self.assertEqual(fact['data'], message['result']['data'])
        self.assertEqual(original, captured['messages'])
