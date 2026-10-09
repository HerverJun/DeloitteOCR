"""Production wire budgets, explicit discovery and intact resumable tool history."""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from pydantic import Field

from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult
from workbench_platform.agent.connection import ControllerConnection
from workbench_platform.agent.context import TokenEstimator, core_tools, prepare_request, wire_json
from workbench_platform.agent.environment import compose
from workbench_platform.agent.providers import ProviderFault, SYSTEM, payload_for
from workbench_platform.agent.registry import ToolRegistry, ToolSpec, compact_schema
from workbench_platform.agent.store import AgentStore, canonical
from workbench_platform.agent.tools import context, data, ocr, classification, forensics, jobs, pdf, results, audit, classification_management, ocr_management, ocr_processing, native_catalog, platform_management, documents, screen_batches


ROOT = Path('F:/OCR/output/agent-context-skills-development-20261005/evidence/context-tests')


def configuration(protocol='openai_chat_completions', cap=16384):
    return {'protocol': protocol, 'model': 'gpt-6.1-sol', 'context_cap': cap,
            'max_output_tokens': 2048, 'base_url': 'http://127.0.0.1:54321/v1'}


def full_registry():
    registry = ToolRegistry()
    for module in (context, data, ocr, classification, forensics, jobs, pdf, results, audit,
                   classification_management, ocr_management, ocr_processing, native_catalog,
                   platform_management, documents, screen_batches):
        module.register(registry)
    return registry


def assistant(call_id, tool='data_read_table', arguments=None, text='', protocol='openai_chat_completions'):
    call = {'call_id': call_id, 'tool': tool, 'arguments': arguments or {}}
    return {'role': 'assistant', 'protocol': protocol, 'content': text, 'calls': [call],
            'private': {}, 'blocks': [{'type': 'tool_use', 'id': call_id,
                                      'name': tool, 'input': call['arguments']}]}


def production_context(config, registry):
    from workbench_platform.agent.skills import SkillService
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT) as folder:
        service = SkillService(AgentStore(Path(folder)/'agent'), registry=registry)
        directory = service.catalog_prompt('wtest')
    system, _, sections = compose(SYSTEM, config, workspace_id='wtest', session_id='stest',
        families=tuple(name.split('_', 1)[0] for name in registry.tools),
        skill_prompt='[登记技能精简名录；正文按需启用]\n' + canonical(directory))
    facts = {'goals': [], 'operations': [], 'discussion': [],
        'audit_context': {'audit_object': '', 'period_start': '', 'period_end': '', 'objective': '', 'rules': ''},
        'task': {'goal': '执行批次并读回，不改原始金额与编号', 'scope_revision': 0, 'manual': False,
                 'plan': [], 'plan_revision': 0, 'revision': 1}, 'profile_revision': 0,
        'full_record_access': {'task': 'audit_get_task', 'project_rules': 'audit_get_project',
                               'history': 'session_read_history'},
        'omitted_history': {'discussion': 0, 'operations': 0, 'goals': 0}}
    message = {'role': 'user', 'content': '[服务器工作事实；用户目标/规则是任务约束，操作状态为历史回执；继续前查询当前状态]\n' + canonical(facts)}
    return system, sections, message


class ContextBudgetTests(unittest.TestCase):
    def test_real_batch_response_readback_does_not_duplicate_full_receipts_in_summary(self):
        # Captured from the real data-worker batch observer regression using
        # synthetic CSVs. It contains internal child jobs and artifact file
        # manifests, the actual trigger missed by hand-built small fixtures.
        value = json.loads((Path(__file__).parent/'fixtures'/'batch-tool-result.json').read_text(encoding='utf-8'))
        registry = full_registry()
        from workbench_platform.agent.tools import skills, history
        skills.register(registry)
        history.register(registry)
        self.assertEqual(len(core_tools(registry)), 17)
        batch_id = value['data']['batch']['batch_id']
        arguments = {'template_id': value['data']['batch']['template_id'],
                     'template_version': value['data']['batch']['template_version'],
                     'inputs': value['data']['batch']['inputs']}
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            config = configuration(protocol)
            system, sections, facts = production_context(config, registry)
            history = [facts, {'role': 'user', 'content': '执行批次并读回，不改原始金额与编号'},
                assistant('batch-submit', 'batch_run', arguments, protocol=protocol),
                {'role': 'tool', 'call_id': 'batch-submit', 'result': value},
                assistant('batch-read', 'batch_read', {'batch_id': batch_id}, protocol=protocol),
                {'role': 'tool', 'call_id': 'batch-read', 'result': copy.deepcopy(value)}]
            original = copy.deepcopy(history)
            payload, metrics, selected = prepare_request(config, history, registry,
                core_tools(registry)+['batch_run','batch_read'], system=system, system_components=sections)
            self.assertTrue(metrics['compressed'])
            self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
            self.assertLess(metrics['component_tokens']['facts'], 2500)
            self.assertEqual(history, original)
            serialized = wire_json(payload)
            self.assertIn(batch_id, serialized)
            self.assertIn('不改原始金额与编号', serialized)
            self.assertIn('batch-read', serialized)
            self.assertIn('batch_read', selected)
            for ref in value['artifact_refs']:
                self.assertIn(ref['object_id'], serialized)
            # Every original asset version/hash still occurs in the exact
            # confirmed submission arguments inside the work-fact summary.
            for source in arguments['inputs']:
                self.assertIn(source['asset_id'], serialized)
                self.assertIn(source['blob_hash'], serialized)

    def test_first_oversized_generated_snapshot_fits_without_old_history_to_summarize(self):
        value = json.loads((Path(__file__).parent/'fixtures'/'batch-tool-result.json').read_text(encoding='utf-8'))
        registry = full_registry()
        from workbench_platform.agent.tools import skills, history
        skills.register(registry)
        history.register(registry)
        self.assertEqual(len(core_tools(registry)), 17)
        batch = value['data']['batch']
        batch['template']['config']['large_internal_snapshot'] = '内部生成模板细节' * 18000
        batch['receipts']['index']['raw_snapshot'] = '内部结果回执' * 18000
        batch['stage'] = 'index'
        batch['artifact_state'] = 'collecting'
        batch['capabilities'] = ['read', 'cancel']
        batch['delivery']['status'] = 'pending'
        batch['summary']['pending_operation'] = 'index'
        batch['summary']['pending_operations'] = 1
        batch['warnings'] = ['索引尚未完成，请继续读取当前状态。' + '风险详情' * 1000]
        value['coverage'] = {'processed': 2, 'failed': 0, 'pending': 1}
        value['warnings'] = ['有一项差异待处置']
        value['next_offset'] = 1
        arguments = {'batch_id': batch['batch_id']}
        evidence = []
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            config = configuration(protocol)
            system, sections, facts = production_context(config, registry)
            messages = [facts, {'role': 'user', 'content': '继续批次，不得覆盖原始金额'},
                assistant('first-large-read', 'batch_read', arguments, protocol=protocol),
                {'role': 'tool', 'call_id': 'first-large-read', 'result': copy.deepcopy(value)}]
            original = copy.deepcopy(messages)
            payload, metrics, selected = prepare_request(config, messages, registry,
                core_tools(registry)+['batch_read'], system=system, system_components=sections)
            self.assertTrue(metrics['compressed'])
            self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
            self.assertEqual(messages, original)
            serialized = wire_json(payload)
            self.assertIn('不得覆盖原始金额', serialized)
            self.assertIn('first-large-read', serialized)
            self.assertIn(batch['batch_id'], serialized)
            if protocol == 'openai_chat_completions':
                actual = json.loads(payload['messages'][-1]['content'])
            else:
                actual = json.loads(payload['messages'][-1]['content'][-1]['content'])
            for key in ('status', 'coverage', 'warnings', 'error', 'next_offset',
                        'result_refs', 'job_refs', 'artifact_refs'):
                self.assertEqual(actual[key], value[key])
            preview = actual['data']['batch']
            for key in ('batch_id', 'revision', 'status', 'stage', 'artifact_state', 'cancel_requested',
                        'capabilities', 'delivery', 'coverage', 'summary'):
                self.assertEqual(preview[key], batch[key])
            self.assertTrue(actual['data']['context_preview_only'])
            self.assertIn('batch', actual['data']['context_preview_snapshots'])
            self.assertIn('索引尚未完成', preview['warnings'][0])
            self.assertLess(len(preview['warnings'][0]), 600)
            self.assertIn('batch_read', selected)
            self.assertNotIn('large_internal_snapshot', serialized)
            evidence.append({'protocol': protocol, 'metrics': metrics})
        (ROOT/'first-generated-snapshot-budget.json').write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_tokenizer_requires_verified_local_cache_and_unknown_model_stays_explicit_estimate(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            url = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
            cache = Path(folder) / hashlib.sha1(url.encode()).hexdigest()
            ranks = b'verified-local-fixture'
            cache.write_bytes(ranks)
            expected = hashlib.sha256(ranks).hexdigest()
            loads = []
            encoding = SimpleNamespace(encode=lambda text, **kwargs: list(text))
            fake = SimpleNamespace(get_encoding=lambda name: loads.append(name) or encoding)
            public = SimpleNamespace(o200k_base=lambda: None)
            with patch.dict(os.environ, {'TIKTOKEN_CACHE_DIR': folder}), \
                 patch.dict('sys.modules', {'tiktoken': fake, 'tiktoken_ext': SimpleNamespace(openai_public=public)}), \
                 patch('workbench_platform.agent.context.inspect.getsource', return_value=f'expected_hash="{expected}"'):
                known = TokenEstimator('gpt-4o')
                self.assertEqual(known.method, 'tiktoken_o200k_base_serialized_wire_estimate')
                self.assertEqual(known.count('你好'), 2)
                self.assertEqual(loads, ['o200k_base'])
                self.assertEqual(TokenEstimator('gpt-6.1-sol').method, 'utf8_bytes_div2_estimate')
                self.assertEqual(TokenEstimator('gpt-4o-private-alias').method, 'utf8_bytes_div2_estimate')
                cache.write_bytes(b'corrupt cache must not initiate a replacement download')
                self.assertEqual(TokenEstimator('gpt-4o').method, 'utf8_bytes_div2_estimate')
                self.assertEqual(loads, ['o200k_base'])

    def test_real_business_tool_registry_first_user_sentence_fits_16k_for_both_protocols(self):
        registry = full_registry()
        self.assertGreaterEqual(len(registry.tools), 108)
        self.assertTrue({'ocr_screen_local_candidates', 'ocr_screen_local_candidates_read',
            'ocr_screen_local_candidates_control'} <= set(registry.tools))
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            config = configuration(protocol)
            system, _, sections = compose('真实平台基础说明', config, workspace_id='wtest',
                session_id='stest', families=('data', 'ocr', 'classification'), skill_prompt='付款核对：按需启用')
            history = [{'role': 'user', 'content': '你是什么模型，你在什么环境下工作？'}]
            payload, metrics, selected = prepare_request(config, history, registry, core_tools(registry),
                system=system, system_components=sections)
            self.assertLessEqual(metrics['input_tokens_estimate'], 14336)
            self.assertEqual(metrics['payload_bytes'], len(wire_json(payload).encode('utf-8')))
            self.assertEqual(metrics['input_tokens_estimate'], math.ceil(metrics['payload_bytes'] / 2))
            self.assertEqual(sum(metrics['component_tokens'].values()), metrics['input_tokens_estimate'])
            self.assertEqual(metrics['estimation_method'], 'utf8_bytes_div2_estimate')
            self.assertGreater(metrics['component_tokens']['environment'], 0)
            self.assertGreater(metrics['component_tokens']['skills'], 0)
            self.assertEqual(selected, core_tools(registry))
            expected = payload_for(config, history, schemas=registry.schemas(selected), system=system)
            expected['stream'] = True
            self.assertEqual(payload, expected)

    def test_directory_pages_do_not_activate_and_explicit_load_does_not_copy_schema(self):
        registry = full_registry()
        ctx = SimpleNamespace(services=SimpleNamespace(agent=SimpleNamespace(registry=registry), data_workbench=None))
        seen, offset, history = [], 0, [{'role': 'user', 'content': '列出全部工具，再继续付款核对'}]
        while offset is not None:
            page = context.list_tools(context.ToolQuery(offset=offset, limit=30), ctx)
            self.assertNotIn('discovered_tools', page['data'])
            self.assertNotIn('activated_tools', page['data'])
            seen.extend(item['name'] for item in page['data']['tools'])
            self.assertTrue(all('input_schema' not in item for item in page['data']['tools']))
            call_id = 'catalog' + str(len(seen))
            history.extend([assistant(call_id, 'tools_list', {'offset': offset, 'limit': 30}),
                            {'role': 'tool', 'call_id': call_id, 'result': page}])
            _, metrics, selected = prepare_request(configuration(), history, registry, core_tools(registry))
            self.assertLessEqual(metrics['input_tokens_estimate'], metrics['available_input_tokens'])
            self.assertEqual(selected, core_tools(registry))
            offset = page['next_offset']
        self.assertEqual(set(seen), set(registry.tools))
        result = asyncio.run(context.describe_tools(context.ToolDescribe(names=['data_read_table']), ctx))
        self.assertEqual(result['data']['activated_tools'], [])
        self.assertEqual(result['error']['code'], 'activation_context_unavailable')
        self.assertNotIn('input_schema', wire_json(result))
        with self.assertRaises(ValueError):
            registry.validate_batch([{'call_id': 'a', 'tool': 'data_read_table', 'arguments': {}}], core_tools(registry))

    def test_schema_compaction_preserves_named_properties_and_all_validation_constraints(self):
        class Entry(AgentModel):
            title: str = Field(pattern='^[A-Z]+$', min_length=2, max_length=10)
            examples: list[int] = Field(min_length=1, max_length=3)
            amount: float = Field(ge=0, lt=100)
        schema = Entry.model_json_schema()
        schema['properties']['title']['enum'] = ['AA', 'BB']
        compact = compact_schema(schema)
        self.assertNotIn('title', compact)
        self.assertIn('title', compact['properties'])
        self.assertIn('examples', compact['properties'])
        self.assertEqual(compact['properties']['title']['pattern'], '^[A-Z]+$')
        self.assertEqual(compact['properties']['title']['enum'], ['AA', 'BB'])
        self.assertEqual(compact['properties']['examples']['maxItems'], 3)
        self.assertEqual(compact['properties']['amount']['exclusiveMaximum'], 100)
        self.assertEqual(compact['required'], schema['required'])
        self.assertFalse(compact['additionalProperties'])

    def test_reclaims_successful_completed_business_contract_but_pins_explicit_activation_batch(self):
        registry = full_registry()
        schema_sha = hashlib.sha256(json.dumps(registry.schemas(['data_read_table'])['data_read_table'],
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')).hexdigest()
        history = [{'role': 'user', 'content': '改为OCR识别'},
                   assistant('initial-load', 'tools_describe', {'names': ['data_read_table']}),
                   {'role': 'tool', 'call_id': 'initial-load', 'result': {'status': 'success',
                    'data': {'format': 'platform-tool-activation-v1', 'activated_tools': ['data_read_table'],
                             'tools': [{'name': 'data_read_table', 'schema_sha256': schema_sha,
                                        'allowed': True, 'loaded': True}]}}}, assistant('latest', 'data_read_table'),
                   {'role': 'tool', 'call_id': 'latest',
                    'result': AgentToolResult(status='success', summary='刚读回台账').model_dump()}]
        _, metrics, selected = prepare_request(configuration(),
            history, registry, list(registry.tools), protected=['ocr_run'])
        self.assertIn('ocr_run', selected)
        self.assertNotIn('data_read_table', selected)
        self.assertIn('data_read_table', metrics['reclaimed_tools'])
        with self.assertRaises(ValueError):
            registry.validate_batch([{'call_id': 'reuse', 'tool': 'data_read_table', 'arguments': {}}], selected)
        # A durable old loaded receipt never activates a retired tool again.
        _, _, next_selected = prepare_request(configuration(), history, registry, selected)
        self.assertNotIn('data_read_table', next_selected)
        self.assertIn('tools_describe', next_selected)
        self.assertTrue(set(core_tools(registry)) <= set(selected))
        self.assertTrue(metrics['reclaimed_tools'])
        self.assertLessEqual(metrics['input_tokens_estimate'], metrics['available_input_tokens'])
        self.assertLess(len(selected), 101)

    def test_long_history_keeps_user_constraints_confirmed_arguments_all_references_and_last_pair(self):
        registry = full_registry()
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            history = [{'role': 'user', 'content': '保留前导零，只处理已选资料，金额必须按原值核对'}]
            for index in range(5):
                identifier = 'table' + str(index)
                result = AgentToolResult(status='pending' if index == 0 else 'success', summary='真实表读取',
                    data={'rows': [{'text': '长表内容' * 3000}] * 30, 'version': index,
                          'condition': {'keep_leading_zeroes': True}},
                    result_refs=[AgentRef(kind='table', workspace_id='wtest', object_id=identifier, revision=index)]).model_dump()
                history.extend([assistant('call' + str(index), arguments={'confirmed_key': '00123'},
                        text='模型历史说明' * (50 if index == 4 else 2000), protocol=protocol),
                    {'role': 'tool', 'call_id': 'call' + str(index), 'result': result}])
            history.insert(5, {'role': 'user', 'content': '补充约束：不得覆盖原表'})
            original = copy.deepcopy(history)
            payload, metrics, selected = prepare_request(configuration(protocol), history, registry,
                core_tools(registry) + ['data_read_table'], protected=['data_read_table'])
            self.assertTrue(metrics['compressed'])
            self.assertEqual(original, history)
            serialized = wire_json(payload)
            self.assertIn('保留前导零', serialized)
            self.assertIn('不得覆盖原表', serialized)
            self.assertIn('00123', serialized)
            for index in range(5):
                self.assertIn('table' + str(index), serialized)
            self.assertIn('call4', serialized)
            self.assertIn('pending', serialized)
            self.assertLessEqual(metrics['input_tokens_estimate'], metrics['available_input_tokens'])
            self.assertIn('data_read_table', selected)

    def test_latest_pending_error_or_protected_contract_cannot_be_retired(self):
        registry = full_registry()
        for status in ('pending', 'error', 'needs_verification', 'success'):
            with self.subTest(status=status):
                history = [{'role': 'user', 'content': '保留当前未决工具及原约束'},
                    assistant('latest', 'data_read_table'), {'role': 'tool', 'call_id': 'latest',
                    'result': AgentToolResult(status=status, summary='当前原生读取回执',
                        error={'code': 'failed', 'message': '原生失败'} if status == 'error' else None).model_dump()}]
                protected = ['data_read_table'] if status == 'success' else []
                _, metrics, selected = prepare_request(configuration(), history, registry,
                    list(registry.tools), protected=protected)
                self.assertIn('data_read_table', selected)
                self.assertNotIn('data_read_table', metrics['reclaimed_tools'])

    def test_direct_screen_batch_and_local_candidate_reads_keep_unresolved_contracts(self):
        registry = full_registry()
        for tool, field in (('ocr_screen_batch_status', 'status'),
                            ('ocr_screen_local_candidates_read', 'execution_status')):
            for state in ('pending', 'queued', 'running', 'paused', 'interrupted', 'failed', 'needs_review'):
                for evidence in ('state', 'count'):
                    with self.subTest(tool=tool, state=state, evidence=evidence):
                        data = {field: state if evidence == 'state' else 'succeeded',
                                'counts': {state: 1} if evidence == 'count' else {'pending': 0}}
                        history = [{'role': 'user', 'content': '继续现有批次，未决事项不得隐去'},
                            assistant('latest-direct', tool), {'role': 'tool', 'call_id': 'latest-direct',
                            'result': AgentToolResult(status='success', summary='读取实际未决状态', data=data).model_dump()}]
                        _, metrics, selected = prepare_request(configuration(), history, registry, list(registry.tools))
                        self.assertIn(tool, selected)
                        self.assertNotIn(tool, metrics['reclaimed_tools'])

    def test_business_data_status_words_do_not_make_completed_read_contract_pending(self):
        registry = full_registry()
        # These are literal table/document values, not native batch semantics.
        data = {'status': 'running', 'execution_status': 'failed', 'counts': {'needs_review': 4},
                'batch': {'status': 'queued', 'summary': {'pending': 3}},
                'rows': [{'status': 'paused', 'text': 'interrupted'}]}
        history = [{'role': 'user', 'content': '读取原表后继续其他工作'}, assistant('literal-status', 'data_read_table'),
                   {'role': 'tool', 'call_id': 'literal-status',
                    'result': AgentToolResult(status='success', summary='已读取业务字面值', data=data).model_dump()}]
        _, metrics, selected = prepare_request(configuration(), history, registry, list(registry.tools), protected=['ocr_run'])
        self.assertNotIn('data_read_table', selected)
        self.assertIn('data_read_table', metrics['reclaimed_tools'])

    def test_component_overflow_is_specific_and_original_user_message_is_never_truncated(self):
        registry = ToolRegistry()
        scenarios = [('user', '基础说明', [{'role': 'user', 'content': '用户明确约束' * 20000}], {}),
            ('environment', '环境' * 30000, [{'role': 'user', 'content': '你好'}], {'environment': '环境' * 30000}),
            ('skills', '技能' * 30000, [{'role': 'user', 'content': '你好'}], {'skills': '技能' * 30000})]
        for component, system, history, sections in scenarios:
            with self.assertRaises(ProviderFault) as caught:
                prepare_request(configuration(), history, registry, [], system=system, system_components=sections)
            metrics = caught.exception.context_metrics
            self.assertEqual(caught.exception.code, 'context_limit')
            self.assertEqual(metrics['overflow_component'], component)
            self.assertGreater(metrics['overflow_tokens'], 0)
        class Huge(AgentModel):
            value: str = Field(description='工具必要参数说明' * 20000)
        registry.register(ToolSpec('huge_tool', '真实大工具', Huge, lambda *_: {}))
        with self.assertRaises(ProviderFault) as caught:
            prepare_request(configuration(), [{'role': 'user', 'content': '加载工具'}], registry,
                            ['huge_tool'], protected=['huge_tool'])
        self.assertEqual(caught.exception.context_metrics['overflow_component'], 'tools')

    def test_invalid_pairing_cannot_be_hidden_by_compression(self):
        registry = full_registry()
        history = [{'role': 'user', 'content': '用户约束' * 20000},
                   {'role': 'tool', 'call_id': 'orphan', 'result': {'status': 'success'}}]
        with self.assertRaises(ProviderFault) as caught:
            prepare_request(configuration(), history, registry, core_tools(registry))
        self.assertEqual(caught.exception.code, 'invalid_response')


class ConnectionPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_real_production_preflight_stops_before_network_and_configuration_save(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            registry = full_registry()
            def production(config):
                return prepare_request(config, [{'role': 'user', 'content': '你好'}], registry,
                    core_tools(registry), system='真实超大运行环境' * 20000)[1]
            connection = ControllerConnection(AgentStore(Path(folder) / 'agent'), production_preflight=production)
            sent = []
            transport = httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(500))
            with self.assertRaises(ProviderFault) as caught:
                await connection.save({**configuration(), 'api_key': 'local'}, transport=transport)
            self.assertEqual(caught.exception.code, 'context_limit')
            self.assertFalse(sent)
            self.assertFalse(connection.view()['configured'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
