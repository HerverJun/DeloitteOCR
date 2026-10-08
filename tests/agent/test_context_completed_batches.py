"""Actual first-round cross-tool discovery regression, without controller calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from test_context_budget import full_registry
from workbench_platform.agent.context import (_history_summary, _reduce_history,
    _completed_history, prepare_request, wire_json)
from workbench_platform.agent.tools import skills, history, preferences
from workbench_platform.agent.providers import ProviderFault, payload_for


class CompletedBatchContextTests(unittest.TestCase):
    def fixture(self, name='s07-first-completed-discovery.json'):
        value = json.loads((Path(__file__).parent / 'fixtures' / name).read_text('utf-8'))
        registry = full_registry()
        for module in (skills, history, preferences):
            module.register(registry)
        names = set(value['visible']) | {call['tool'] for message in value['messages']
                                        for call in message.get('calls', [])}
        # These actual tools were registered but not all activated on round 1.
        names.update({'native_attach_result', 'native_read_result', 'results_describe', 'results_to_input',
            'data_create_session', 'data_inspect_input', 'data_import', 'audit_bind_evidence', 'audit_read_evidence',
            'assets_search', 'assets_describe', 'artifacts_get'})
        if 'registered_tools' in value:
            # A visible set is not the registered core. Actual admission may
            # have reclaimed all visible tools while core discovery controls
            # are still present in the next production request.
            names = set(value['registered_tools'])
        registry.tools = {name: spec for name, spec in registry.tools.items() if name in names}
        return value, registry

    def test_real_first_completed_six_tool_batch_fits_without_losing_constraints_or_discovery(self):
        value, registry = self.fixture()
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            with self.subTest(protocol=protocol):
                messages = deepcopy(value['messages'])
                for message in messages:
                    if message['role'] == 'assistant':
                        message['protocol'] = protocol
                original = deepcopy(messages)
                config = {**value['config'], 'protocol': protocol}
                payload, metrics, selected = prepare_request(config, messages, registry, value['visible'],
                    system=value['system'], system_components=value['sections'])
                self.assertTrue(metrics['compressed'])
                self.assertEqual(messages, original)
                self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
                serialized = wire_json(payload)
                goal = next(item['content'] for item in messages if item.get('id') == 's07RealNativeAgentGoal')
                self.assertIn(goal, [item.get('content') for item in payload['messages']])
                summary = next(item for item in payload['messages'] if item.get('content', '').startswith('[服务器保存的工具事实'))
                actual = json.loads(summary['content'].split('\n', 1)[1])
                by_id = {item['call_id']: item for item in actual}
                for message in messages:
                    if message['role'] != 'tool':
                        continue
                    self.assertIn(message['call_id'], by_id)
                    if by_id[message['call_id']]['tool'] == 'skills_read_resource':
                        self.assertEqual(by_id[message['call_id']]['data'], message['result']['data'])
                    if by_id[message['call_id']]['tool'] == 'tools_list':
                        for tool in message['result']['data']['tools']:
                            self.assertIn(tool['name'], serialized)
                            entry = next(entry for entry in by_id[message['call_id']]['data']['tools'] if entry['name'] == tool['name'])
                            if 'availability_ref' in entry:
                                ref = entry['availability_ref']
                                entry = next(item for item in by_id[ref['call_id']]['data']['tools'] if item['name'] == ref['tool_name'])
                            self.assertEqual(entry['availability'], tool['availability'])
                self.assertIn('tools_describe', selected)
                # Summary messages cannot leave orphan native tool_result blocks.
                self.assertFalse(any(item['role'] in {'tool', 'assistant'} for item in payload['messages']))

    def test_incomplete_or_duplicate_batch_cannot_be_collapsed_as_completed(self):
        value, _ = self.fixture()
        self.assertTrue(_completed_history(value['messages']))
        self.assertFalse(_completed_history(value['messages'][:-1]))
        self.assertFalse(_completed_history([*value['messages'], value['messages'][-1]]))

    def test_equal_completed_readback_keeps_both_operations_with_exact_snapshot_reference(self):
        value, _ = self.fixture()
        messages = value['messages']
        read = next(item for item in messages if item['role'] == 'tool' and item['result']['data'].get('goal'))
        duplicate = deepcopy(read); duplicate['call_id'] = 'repeated-task-read'
        messages += [{'role': 'assistant', 'calls': [{'call_id': duplicate['call_id'], 'tool': 'audit_get_task', 'arguments': {}}]}, duplicate]
        facts = json.loads(_history_summary(messages)['content'].split('\n', 1)[1])
        self.assertEqual(facts[-1]['data_ref']['call_id'], read['call_id'])
        self.assertIn('sha256', facts[-1]['data_ref'])
        self.assertNotIn('data', facts[-1])
        self.assertEqual(facts[-1]['status'], duplicate['result']['status'])

    def test_actual_repeated_read_reclaims_completed_reads_first_and_preserves_explicit_protection(self):
        value, registry = self.fixture('s07-repeated-completed-read.json')
        payload, metrics, selected = prepare_request(value['config'], value['messages'], registry, value['visible'],
            system=value['system'], system_components=value['sections'])
        self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
        self.assertIn('audit_get_task', metrics['reclaimed_tools'])
        self.assertIn('tools_describe', selected)
        self.assertIn('skills_read_resource', selected)
        _, protected_metrics, protected_selected = prepare_request(value['config'], value['messages'], registry,
            value['visible'], system=value['system'], system_components=value['sections'],
            protected=['audit_get_task', 'audit_get_project'])
        self.assertLessEqual(protected_metrics['input_tokens_estimate'], 14336)
        self.assertTrue({'audit_get_task','audit_get_project'} <= set(protected_selected))
        with self.assertRaises(ProviderFault) as caught:
            prepare_request(value['config'], value['messages'], registry, value['visible'],
                system=value['system'], system_components=value['sections'],
                protected=value['visible'])
        self.assertEqual(caught.exception.code, 'context_limit')

    def test_changed_goal_scope_and_project_rules_are_retained_exactly(self):
        value, _ = self.fixture()
        messages = value['messages']
        tool = next(item for item in messages if item['role'] == 'tool' and item['result']['data'].get('goal'))
        data = tool['result']['data']
        data['goal'] += '追加约束：不得四舍五入。'
        data['selected_assets'] += ['changed_scope_source']
        data['initial_refs'][0]['revision'] += 1
        data['selected_asset_set']['revision'] += 1
        project = next(item for item in messages if item['role'] == 'tool' and 'rules' in item['result']['data'])
        project['result']['data']['rules'] = '精确金额，不合并同名业务记录。'
        project['result']['data']['objective'] = '保留最新项目目标。'
        facts = json.loads(_history_summary(messages)['content'].split('\n', 1)[1])
        task = next(item['data'] for item in facts if item['tool'] == 'audit_get_task')
        for key in ('goal', 'selected_assets', 'initial_refs', 'selected_asset_set'):
            self.assertEqual(task[key], data[key])
        rules = next(item['data'] for item in facts if item['tool'] == 'audit_get_project')
        self.assertEqual(rules['rules'], project['result']['data']['rules'])
        self.assertEqual(rules['objective'], project['result']['data']['objective'])

    def test_server_goal_dedup_requires_exact_equality_and_retains_event_identity(self):
        value, _ = self.fixture()
        messages = value['messages']
        host = json.loads(messages[0]['content'].split('\n', 1)[1])
        host['task']['goal'] += '追加目标。'
        prefix = messages[0]['content'].split('\n', 1)[0]
        messages[0]['content'] = prefix + '\n' + wire_json(host)
        reduced = _reduce_history(messages, len(messages))
        actual = json.loads(reduced[0]['content'].split('\n', 1)[1])
        self.assertEqual(actual['task']['goal'], host['task']['goal'])
        self.assertNotIn('goal_ref', actual['task'])
        self.assertEqual(actual['current_user_instructions'][0]['event_seq'], host['current_user_instructions'][0]['event_seq'])
        self.assertIn('content_ref', actual['current_user_instructions'][0])

    def test_warnings_errors_coverage_and_references_survive_terminal_batch_summary(self):
        value, _ = self.fixture()
        result = value['messages'][-1]['result']
        result['status'] = 'error'
        result['warnings'] = ['模型尚未就绪，不能调用。']
        result['error'] = {'code': 'real_failure', 'message': '实际失败，不得隐去。'}
        result['coverage'] = {'processed': 2, 'failed': 1, 'pending': 3}
        result['next_offset'] = 4
        result['result_refs'] = [{'kind': 'result', 'object_id': 'actual', 'revision': 5, 'sha256': 'a' * 64}]
        facts = json.loads(_history_summary(value['messages'])['content'].split('\n', 1)[1])
        actual = facts[-1]
        for key in ('status', 'warnings', 'error', 'coverage', 'next_offset', 'result_refs'):
            self.assertEqual(actual[key], result[key])

    def test_repeated_descriptor_metadata_keeps_first_copy_and_each_permission_loading_error(self):
        item = {'name': 'data_import', 'version': '1', 'effect': 'versioned', 'has_handler': True,
                'has_lookup': True, 'schema_sha256': 'a' * 64, 'limits': {'page_items': 50},
                'availability': {'state': 'ready', 'reason': ''}, 'allowed': True, 'loaded': True}
        messages = []
        for index in range(2):
            current = deepcopy(item)
            if index:
                current.update(allowed=False, loaded=False, error={'code': 'skill_tool_denied', 'message': '实际权限已收紧'})
            call_id = 'descriptor-' + str(index)
            messages.extend([{'role': 'assistant', 'calls': [{'call_id': call_id, 'tool': 'tools_describe',
                'arguments': {'names': ['data_import']}}]}, {'role': 'tool', 'call_id': call_id,
                'result': {'status': 'success', 'data': {'tools': [current], 'activated_tools': []}}}])
        original = deepcopy(messages)
        facts = json.loads(_history_summary(messages)['content'].split('\n', 1)[1])
        first, second = [fact['data']['tools'][0] for fact in facts]
        self.assertEqual(first, item)
        self.assertEqual(second['metadata_ref']['call_id'], 'descriptor-0')
        self.assertEqual(second['metadata_ref']['tool_name'], 'data_import')
        self.assertFalse(second['allowed'])
        self.assertFalse(second['loaded'])
        self.assertEqual(second['error'], messages[-1]['result']['data']['tools'][0]['error'])
        self.assertEqual(messages, original)
        messages[-1]['result']['data']['tools'][0]['schema_sha256'] = 'b' * 64
        changed = json.loads(_history_summary(messages)['content'].split('\n', 1)[1])[-1]['data']['tools'][0]
        self.assertNotIn('metadata_ref', changed)
        self.assertEqual(changed['schema_sha256'], 'b' * 64)

    def test_actual_larger_reload_fits_and_archived_contracts_reconstruct_every_decision(self):
        value, registry = self.fixture('s07-larger-completed-reload.json')
        messages = deepcopy(value['messages'])
        original = deepcopy(messages)
        payload, metrics, selected = prepare_request(value['config'], messages, registry, value['visible'],
            system=value['system'], system_components=value['sections'], protected=value['protected'])
        self.assertEqual(messages, original)
        self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
        self.assertIn('skills_read_resource', metrics['reclaimed_tools'])
        self.assertIn('skills_deactivate', metrics['reclaimed_tools'])
        self.assertTrue({'tools_describe', 'session_read_history', 'skills_activate'} <= set(selected))
        goal = next(item['content'] for item in messages if item.get('id') == 's07RealNativeAgentGoal')
        self.assertIn(goal, [item.get('content') for item in payload['messages']])
        summary = next(item['content'] for item in payload['messages']
            if item.get('content', '').startswith('[服务器保存的工具事实'))
        self.assertIn('每项call_id用session_read_history精确回读', summary.split('\n', 1)[0])
        facts = json.loads(summary.split('\n', 1)[1])
        by_id = {item['call_id']: item for item in facts}
        fields = {'name', 'schema_sha256', 'availability', 'allowed', 'loaded', 'error', 'reason'}

        def restore(call_id, index, visited=()):
            identity = (call_id, index)
            self.assertNotIn(identity, visited)
            data = by_id[call_id]['data']
            entry = data['tools'][index]
            restored = {}
            if 'metadata_ref' in entry:
                ref = entry['metadata_ref']
                prior = restore(ref['call_id'], ref['tool_index'], (*visited, identity))
                restored.update({key: prior[key] for key in ('name', 'schema_sha256', 'availability') if key in prior})
            restored.update(deepcopy(data.get('tool_defaults', {})))
            restored.update({key: deepcopy(item) for key, item in entry.items() if key in fields})
            if 'availability_ref' in entry:
                ref = entry['availability_ref']
                prior_data = by_id[ref['call_id']]['data']
                prior_index = next(index for index, item in enumerate(prior_data['tools']) if item['name'] == ref['tool_name'])
                restored['availability'] = restore(ref['call_id'], prior_index, (*visited, identity))['availability']
            return restored

        for message in messages:
            if message['role'] != 'tool':
                continue
            self.assertIn(message['call_id'], by_id)
            fact = by_id[message['call_id']]
            if fact['tool'] == 'skills_read_resource':
                self.assertEqual(fact['data'], message['result']['data'])
            if fact['tool'] != 'tools_describe':
                continue
            result = message['result']
            expected_hash = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False,
                separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()
            self.assertEqual(fact['data']['archived_receipt_sha256'], expected_hash)
            self.assertEqual(fact['call_id'], message['call_id'])
            self.assertEqual(fact['arguments']['names'], result['data']['requested_tools'])
            for index, entry in enumerate(result['data']['tools']):
                self.assertEqual(restore(message['call_id'], index), {key: deepcopy(item) for key, item in entry.items() if key in fields})

    def test_completed_skill_exit_never_reclaims_an_explicitly_protected_contract(self):
        value, registry = self.fixture('s07-larger-completed-reload.json')
        for name in ('skills_read_resource', 'skills_deactivate'):
            with self.subTest(name=name):
                try:
                    _, metrics, selected = prepare_request(value['config'], value['messages'], registry, value['visible'],
                        system=value['system'], system_components=value['sections'], protected=[name])
                except ProviderFault as error:
                    self.assertEqual(error.code, 'context_limit')
                    self.assertNotIn(name, error.context_metrics['reclaimed_tools'])
                else:
                    self.assertNotIn(name, metrics['reclaimed_tools'])
                    self.assertIn(name, selected)


if __name__ == '__main__':
    unittest.main()
