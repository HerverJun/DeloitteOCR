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
    def test_latest_history_read_of_archived_phase_requires_exact_retained_source(self):
        command = {'handoff_id': 'actual-handoff', 'device': 'cpu'}
        receipt = {'status': 'success', 'data': {'handoff_id': 'actual-handoff',
                    'warnings': ['低置信度待复核']},
                   'result_refs': [{'kind': 'result', 'object_id': 'actual-result',
                                    'revision': 2, 'sha256': 'a' * 64}]}
        content = wire_json({'arguments': command, 'result': receipt})
        page = {'status': 'success', 'data': {'call_id': 'inference',
            'tool': 'classification_infer', 'offset': 0, 'next_offset': None,
            'content': content, 'content_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'result_sha256': hashlib.sha256(json.dumps(receipt, sort_keys=True,
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}}
        messages = [
            {'role': 'assistant', 'calls': [{'call_id': 'inference',
                'tool': 'classification_infer', 'arguments': command}]},
            {'role': 'tool', 'call_id': 'inference', 'result': receipt},
            {'role': 'assistant', 'calls': [{'call_id': 'readback',
                'tool': 'session_read_history', 'arguments': {'call_id': 'inference'}}]},
            {'role': 'tool', 'call_id': 'readback', 'result': page}]
        saved = deepcopy(messages)
        facts = json.loads(_history_summary(messages,
            archive_tools=['classification_infer'])['content'].split('\n', 1)[1])
        self.assertNotIn('verified_readback', facts[-1].get('data', {}))
        self.assertIn('archived_operation_sha256', facts[-1])
        self.assertEqual(facts[0]['data']['retained_risks'], {'warnings': ['低置信度待复核']})
        self.assertEqual(messages, saved)
        for changed in ('no_archive', 'wrong_arguments', 'changed_source_receipt', 'missing_source'):
            with self.subTest(changed=changed):
                actual = deepcopy(messages)
                archives = ['classification_infer']
                if changed == 'no_archive':
                    archives = []
                elif changed == 'wrong_arguments':
                    actual[0]['calls'][0]['arguments']['device'] = 'gpu'
                elif changed == 'changed_source_receipt':
                    actual[1]['result']['result_refs'][0]['revision'] = 3
                else:
                    actual = actual[2:]
                value = json.loads(_history_summary(actual,
                    archive_tools=archives)['content'].split('\n', 1)[1])[-1]
                self.assertIn('verified_readback', value['data'])

    def test_latest_complete_history_read_exposes_actual_files_refs_and_hashes_after_compaction(self):
        artifact = {'kind': 'artifact', 'object_id': 'actual-export', 'revision': 1, 'application': 'classification',
                    'workspace_id': 'current-workspace', 'parent_id': 'actual-handoff'}
        original = {'arguments': {'handoff_id': 'actual-handoff'}, 'result': {'status': 'success',
            'summary': 'Actual published files', 'artifact_refs': [artifact],
            'data': {'bundle': {'bundle_id': 'actual-export', 'title': '真实分类成果',
                               'files': [{'name': 'classification-results.csv', 'size_bytes': 556, 'availability': 'available'},
                                         {'name': 'provenance.json', 'size_bytes': 1688, 'availability': 'available'}]}}}}
        content = json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        result_sha = hashlib.sha256(json.dumps(original['result'], sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode('utf8')).hexdigest()
        content_sha = hashlib.sha256(content.encode('utf8')).hexdigest()
        messages = []
        for index in range(3):
            call_id = 'read-' + str(index)
            messages.extend([{'role': 'assistant', 'calls': [{'call_id': call_id, 'tool': 'session_read_history',
                'arguments': {'call_id': 'original-export', 'offset': 0, 'chars': 8000}}]},
                {'role': 'tool', 'call_id': call_id, 'result': {'status': 'success', 'data': {'call_id': 'original-export',
                    'tool': 'classification_export', 'content': content, 'offset': 0, 'next_offset': None,
                    'result_sha256': result_sha, 'content_sha256': content_sha}}}])
        saved = deepcopy(messages)
        facts = json.loads(_history_summary(messages)['content'].split('\n', 1)[1])
        for index, fact in enumerate(facts[:2]):
            self.assertTrue(fact['data_preview_only'])
            self.assertEqual(fact['archived_operation_sha256'], hashlib.sha256(json.dumps({
                'arguments': messages[index * 2]['calls'][0]['arguments'], 'result': messages[index * 2 + 1]['result']},
                sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest())
        latest = facts[-1]['data']['verified_readback']
        self.assertEqual(latest['source_call_id'], 'original-export')
        self.assertEqual(latest['source_result_sha256'], result_sha)
        self.assertEqual(latest['source_operation_sha256'], content_sha)
        self.assertEqual(latest['restored_fields']['artifact_refs'], [artifact])
        self.assertEqual(latest['restored_fields']['files'], original['result']['data']['bundle']['files'])
        self.assertEqual(messages, saved)
        for changed in ({'next_offset': 100}, {'content_sha256': '0' * 64}, {'result_sha256': '0' * 64}):
            with self.subTest(changed=changed):
                invalid = deepcopy(messages[-2:]); invalid[-1]['result']['data'].update(changed)
                actual = json.loads(_history_summary(invalid)['content'].split('\n', 1)[1])[0]
                self.assertNotIn('verified_readback', actual.get('data', {}))
                self.assertNotIn('data_preview_only', actual)

    def test_completed_export_archive_keeps_actual_artifact_contract_and_all_risks(self):
        artifact = {'kind': 'artifact', 'object_id': 'actual-bundle', 'revision': 1, 'application': 'data', 'parent_id': 'actual-session'}
        result = {'status': 'success', 'data': {'version': 'actual-version', 'tables': {'left': {'columns': [{'header': 'original'}] * 8}},
            'bundle': {'bundle_id': 'actual-bundle', 'result_id': 'actual-comparison', 'title': 'Actual export',
                       'files': [{'name': 'results.csv', 'sha256': 'c' * 64}], 'warnings': ['待人工复核']}}, 'artifact_refs': [artifact]}
        command = {'data_session_id': 'actual-session', 'expected_version': 'actual-version', 'result_id': 'actual-comparison'}
        messages = [{'role': 'assistant', 'calls': [{'call_id': 'actual-export', 'tool': 'data_export', 'arguments': command}]},
                    {'role': 'tool', 'call_id': 'actual-export', 'result': result}]
        original = deepcopy(messages)
        fact = json.loads(_history_summary(messages, archive_tools=['data_export'])['content'].split('\n', 1)[1])[0]
        self.assertEqual(fact['artifact_refs'], [artifact])
        self.assertEqual(fact['data']['bundle']['bundle_id'], 'actual-bundle')
        self.assertEqual(fact['artifact_refs'][0]['parent_id'], 'actual-session')
        self.assertNotIn('tables', fact['data'])
        self.assertEqual(fact['data']['retained_risks'], {'bundle': {'warnings': ['待人工复核']}})
        self.assertEqual(fact['archived_operation_sha256'], hashlib.sha256(json.dumps({'arguments': command, 'result': result},
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest())
        self.assertEqual(messages, original)

    def test_superseded_table_sources_restore_by_hash_and_live_evidence_ref_is_retained(self):
        refs = [{'kind': 'result', 'object_id': 'table-v1', 'revision': 4, 'sha256': 'a' * 64}]
        columns = {'columns': [{'column_id': 'c1', 'header': '00123'}], 'revision': 'table-v1'}
        table = {'status': 'success', 'data': {'data_session_id': 'session', 'metadata': columns}, 'result_refs': refs}
        evidence = {'status': 'success', 'data': {'evidence_id': 'evidence', 'snapshot_hash': 'b' * 64,
                    'stale': False, 'observation': {'warnings': ['原图待人工复核']}}, 'result_refs': refs}
        messages = []
        for tool, result in [('data_read_table', table), ('audit_read_evidence', evidence)]:
            messages.extend([{'role': 'assistant', 'calls': [{'call_id': tool, 'tool': tool,
                'arguments': {'data_session_id': 'session', 'offset': 0}}]},
                {'role': 'tool', 'call_id': tool, 'result': result}])
        original = deepcopy(messages)
        def facts(keep_columns):
            return json.loads(_history_summary(messages, archive_tools=['data_read_table', 'audit_read_evidence'],
                              retain_table_columns=keep_columns)['content'].split('\n', 1)[1])
        live_table = facts(True)[0]
        self.assertEqual(live_table['data']['metadata'], columns)
        self.assertEqual(live_table['result_refs'], refs)
        archived_table, live_evidence = facts(False)
        self.assertNotIn('result_refs', archived_table)
        self.assertNotIn('arguments', archived_table)
        self.assertNotIn('metadata', archived_table['data'])
        for index, fact in enumerate([archived_table, live_evidence]):
            self.assertEqual(fact['archived_operation_sha256'], hashlib.sha256(json.dumps({
                'arguments': messages[index * 2]['calls'][0]['arguments'], 'result': messages[index * 2 + 1]['result']},
                sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest())
        self.assertEqual(live_evidence['result_refs'], refs)
        self.assertFalse(live_evidence['data']['stale'])
        self.assertEqual(live_evidence['data']['snapshot_hash'], 'b' * 64)
        self.assertEqual(live_evidence['data']['retained_risks'], {'observation': {'warnings': ['原图待人工复核']}})
        self.assertEqual(messages, original)
        messages[-1]['result']['data'].update(stale=True, outdated_reasons=['source revision changed'])
        stale = facts(False)[-1]
        self.assertNotIn('data_preview_only', stale)
        self.assertTrue(stale['data']['stale'])
        self.assertEqual(stale['data']['outdated_reasons'], ['source revision changed'])

    def test_completed_comparison_preview_keeps_only_actual_compared_literals_and_locations(self):
        rows = [{'category': 'different', 'left_row_id': 'left-' + str(index),
                 'right_row_id': 'right-' + str(index), 'left_source_row': index + 2,
                 'right_source_row': index + 2, 'key_json': '["00123"]',
                 'different_columns_json': '["c3:c3"]', 'L_c1_raw': '00123', 'R_c1_raw': '00123',
                 'L_c3_raw': 'OBFCI', 'R_c3_raw': 'OBFC1', 'L_c8_raw': 'uncompared-column'}
                for index in range(6)]
        result = {'status': 'success', 'data': {'rows': rows, 'offset': 0, 'total': 6,
            'metadata': {'id': 'actual-comparison', 'coverage': {'different_pairs': 6},
                         'policy': {'key_pairs': [['c1', 'c1']], 'compare_pairs': [['c3', 'c3']]}}},
            'result_refs': [{'kind': 'result', 'object_id': 'actual-comparison', 'revision': 4}]}
        messages = [{'role': 'assistant', 'calls': [{'call_id': 'differences', 'tool': 'data_read_result', 'arguments': {}}]},
                    {'role': 'tool', 'call_id': 'differences', 'result': result}]
        original = deepcopy(messages)
        fact = json.loads(_history_summary(messages, archive_tools=['data_read_result'])['content'].split('\n', 1)[1])[0]
        self.assertEqual(messages, original)
        self.assertEqual(fact['result_refs'], result['result_refs'])
        self.assertEqual(fact['data']['metadata']['coverage'], {'different_pairs': 6})
        self.assertEqual(fact['data']['difference_preview_count'], 5)
        self.assertEqual(fact['data']['page_difference_count'], 6)
        self.assertEqual(fact['data']['difference_preview'][0]['L_c1_raw'], '00123')
        self.assertEqual(fact['data']['difference_preview'][0]['L_c3_raw'], 'OBFCI')
        self.assertEqual(fact['data']['difference_preview'][0]['R_c3_raw'], 'OBFC1')
        self.assertEqual(fact['data']['difference_preview'][0]['left_source_row'], 2)
        self.assertNotIn('L_c8_raw', fact['data']['difference_preview'][0])
        self.assertTrue(fact['data_preview_only'])

    def test_completed_phase_archive_keeps_exact_sources_commands_and_all_risks(self):
        reference = {'kind': 'result', 'object_id': 'native-result', 'revision': 7,
                     'sha256': 'a' * 64, 'sources': [{'object_id': 'original-image', 'revision': 2}]}
        result = {'status': 'success', 'summary': '真实完整结构',
                  'data': {'version': 4, 'data_session_id': 'current',
                           'sample_cells': [{'row': 0, 'column': 0, 'text': '00123'}] * 40,
                           'tables': {'left': {'warnings': ['原图仍待人工复核']}}},
                  'result_refs': [reference], 'warnings': ['工程模拟不是业务准确率']}
        command = {'reference': reference, 'table_index': 0}
        messages = [{'role': 'assistant', 'calls': [{'call_id': 'source', 'tool': 'results_describe', 'arguments': command}]},
                    {'role': 'tool', 'call_id': 'source', 'result': result}]
        original = deepcopy(messages)
        fact = json.loads(_history_summary(messages, archive_tools=['results_describe'])['content'].split('\n', 1)[1])[0]
        self.assertEqual(messages, original)
        self.assertNotIn('arguments', fact)
        self.assertNotIn('result_refs', fact)
        self.assertEqual(fact['archived_operation_sha256'], hashlib.sha256(json.dumps({
            'arguments': command, 'result': result}, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode('utf8')).hexdigest())
        self.assertEqual(fact['warnings'], result['warnings'])
        self.assertEqual(fact['data']['retained_risks'], {'tables': {'left': {'warnings': ['原图仍待人工复核']}}})
        self.assertEqual(fact['data']['data_session_id'], 'current')
        self.assertEqual(fact['data']['version'], 4)
        self.assertNotIn('sample_cells', fact['data'])
        self.assertTrue(fact['data_preview_only'])
        self.assertEqual(fact['archived_receipt_sha256'], hashlib.sha256(json.dumps(result,
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest())
        for status in ('pending', 'error', 'needs_verification'):
            messages[-1]['result']['status'] = status
            actual = json.loads(_history_summary(messages, archive_tools=['results_describe'])['content'].split('\n', 1)[1])[0]
            self.assertNotIn('data_preview_only', actual)
            self.assertIn('sample_cells', actual['data'])

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
        try:
            _, metrics, selected = prepare_request(value['config'], value['messages'], registry, value['visible'],
                system=value['system'], system_components=value['sections'],
                protected=value['visible'])
        except ProviderFault as error:
            self.assertEqual(error.code, 'context_limit')
            self.assertFalse(set(value['visible']) & set(error.context_metrics['reclaimed_tools']))
        else:
            self.assertTrue(set(value['visible']) <= set(selected))
            self.assertFalse(set(value['visible']) & set(metrics['reclaimed_tools']))
            self.assertLessEqual(metrics['input_tokens_estimate'], 14336)

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
        fields = {'name', 'version', 'schema_sha256', 'availability', 'allowed', 'loaded', 'error', 'reason'}

        def data_at(position):
            fact = facts[position]
            if isinstance(fact.get('data_ref'), int):
                self.assertLess(fact['data_ref'], position)
                return data_at(fact['data_ref'])
            if 'data_ref' in fact:
                ref = fact['data_ref']
                previous = next(index for index, item in enumerate(facts) if item['call_id'] == ref['call_id'])
                self.assertLess(previous, position)
                return data_at(previous)
            return fact['data']

        def restore(call_id, index, visited=()):
            identity = (call_id, index)
            self.assertNotIn(identity, visited)
            position = next(index for index, item in enumerate(facts) if item['call_id'] == call_id)
            data = data_at(position)
            entry = data['tools'][index]
            restored = {}
            if 'metadata_ref' in entry:
                ref = entry['metadata_ref']
                if isinstance(ref, list):
                    self.assertLessEqual(ref[0], position)
                    prior = restore(facts[ref[0]]['call_id'], ref[1], (*visited, identity))
                else:
                    prior = restore(ref['call_id'], ref['tool_index'], (*visited, identity))
                restored.update({key: prior[key] for key in ('name', 'version', 'schema_sha256', 'availability') if key in prior})
            restored.update(deepcopy(data.get('tool_defaults', {})))
            restored.update({key: deepcopy(item) for key, item in entry.items() if key in fields})
            if 'availability_ref' in entry:
                ref = entry['availability_ref']
                if isinstance(ref, list):
                    self.assertLessEqual(ref[0], position)
                    prior_call, prior_index = facts[ref[0]]['call_id'], ref[1]
                else:
                    prior_call = ref['call_id']
                    prior_data = by_id[prior_call]['data']
                    prior_index = next(index for index, item in enumerate(prior_data['tools']) if item['name'] == ref['tool_name'])
                restored['availability'] = restore(prior_call, prior_index, (*visited, identity))['availability']
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
            self.assertEqual(data_at(next(index for index, item in enumerate(facts) if item['call_id'] == message['call_id']))['archived_receipt_sha256'], expected_hash)
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

    def test_archived_indexes_restore_duplicate_receipts_and_changed_local_decisions(self):
        base = {'name': 'data_import', 'version': 'v1', 'schema_sha256': 'a' * 64,
                'availability': {'state': 'ready', 'details': 'historical readiness ' * 12},
                'allowed': True, 'loaded': True}
        rows = [deepcopy(base), deepcopy(base), {**deepcopy(base), 'allowed': False, 'loaded': False,
            'error': {'code': 'denied', 'message': 'Actual local denial'}},
            {**deepcopy(base), 'version': 'v2', 'schema_sha256': 'b' * 64}]
        messages = []
        ref = {'kind': 'result', 'object_id': 'native-result', 'revision': 7, 'sha256': 'c' * 64}
        for index, row in enumerate(rows):
            result = {'status': 'success', 'summary': 'Same immutable discovery summary',
                'data': {'tools': [row], 'requested_tools': ['data_import'],
                         'activated_tools': ['data_import'] if row['loaded'] else []}, 'result_refs': [ref]}
            messages.extend([{'role': 'assistant', 'calls': [{'call_id': 'indexed-' + str(index),
                'tool': 'tools_describe', 'arguments': {'names': ['data_import']}}]},
                {'role': 'tool', 'call_id': 'indexed-' + str(index), 'result': result}])
        original = deepcopy(messages)
        summary = _history_summary(messages, archive_discovery=True)
        facts = json.loads(summary['content'].split('\n', 1)[1])

        def data_at(position):
            fact = facts[position]
            reference = fact.get('data_ref')
            if isinstance(reference, int):
                self.assertLess(reference, position)
                return data_at(reference)
            return fact['data']

        def restore(position, entry_index, visited=()):
            self.assertNotIn((position, entry_index), visited)
            data = data_at(position)
            item = data['tools'][entry_index]
            restored = {}
            if 'metadata_ref' in item:
                prior_position, prior_index = item['metadata_ref']
                self.assertLess(prior_position, position)
                prior = restore(prior_position, prior_index, (*visited, (position, entry_index)))
                restored.update({key: prior[key] for key in ('name', 'version', 'schema_sha256', 'availability')})
            restored.update(deepcopy(data.get('tool_defaults', {})))
            restored.update({key: deepcopy(value) for key, value in item.items() if not key.endswith('_ref')})
            if 'availability_ref' in item:
                prior_position, prior_index = item['availability_ref']
                self.assertLess(prior_position, position)
                restored['availability'] = restore(prior_position, prior_index, (*visited, (position, entry_index)))['availability']
            return restored

        self.assertEqual(facts[1]['data_ref'], 0)
        for index, expected in enumerate(rows):
            self.assertEqual(restore(index, 0), expected)
            self.assertEqual(facts[index]['arguments'], messages[index * 2]['calls'][0]['arguments'])
            self.assertEqual(data_at(index)['archived_receipt_sha256'], hashlib.sha256(json.dumps(messages[index * 2 + 1]['result'], sort_keys=True,
                    ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest())
            source = facts[index].get('result_refs_ref', index)
            self.assertLessEqual(source, index)
            self.assertEqual(facts[source]['result_refs'], [ref])
        self.assertNotIn('metadata_ref', data_at(3)['tools'][0])
        self.assertEqual(messages, original)


if __name__ == '__main__':
    unittest.main()
