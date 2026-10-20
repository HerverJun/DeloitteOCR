"""Capability provenance, revision authority, and actual HTTP wire budgeting."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from packages.contracts.agent import ControllerDraft
from workbench_platform.agent.connection import ControllerConnection
from workbench_platform.agent.context import request_metrics, wire_json
from workbench_platform.agent.model_profiles import candidate, normalize_config, resolve_profile, HARNESS_REVISION
from workbench_platform.agent.providers import MAX_ERROR_BYTES, ProviderFault, payload_for, request
from workbench_platform.agent.store import AgentStore


ROOT = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence/model-profile-tests')


def config(**changes):
    return {'protocol': 'openai_chat_completions', 'model': 'gpt-6.1-sol',
            'base_url': 'http://127.0.0.1:54321/v1', 'context_cap': 65536,
            'max_output_tokens': 2048, 'streaming': False, **changes}


class MemoryVault:
    def __init__(self):
        self.values = {}
        self.sequence = 0
        self.fail_delete = False

    def save(self, key):
        self.sequence += 1
        reference = 'reference_' + str(self.sequence)
        self.values[reference] = key
        return reference

    def read(self, reference):
        if reference not in self.values:
            raise ValueError('Unavailable credential')
        return self.values[reference]

    def delete(self, reference):
        if self.fail_delete:
            raise OSError('Simulated failed file deletion')
        self.values.pop(reference, None)


async def local_probe(*args, **kwargs):
    return {'native_tools': True, 'business_data_sent': False}


class ModelProfileTests(unittest.TestCase):
    def test_unknown_gateway_alias_keeps_local_64k_without_claiming_capacity(self):
        profile = resolve_profile(config())
        self.assertIsNone(profile['context_window'])
        self.assertEqual(profile['capacity_status'], 'unknown')
        self.assertFalse(profile['capacity_verified'])
        self.assertEqual(profile['runtime_context_cap'], 65536)
        self.assertEqual(profile['effective_context_window'], 65536)
        self.assertEqual(profile['available_input_tokens'], 61440)
        self.assertIsNone(profile['model_max_output_tokens'])

    def test_exact_pinned_catalog_identity_only_and_no_request_default_as_capacity(self):
        official = config(protocol='anthropic_messages', base_url='https://api.deepseek.com/anthropic', model='deepseek-flash')
        profile = resolve_profile(official)
        self.assertEqual(profile['context_window'], 1_000_000)
        self.assertEqual(profile['capacity_sources']['context_window'], 'identity_matched_catalog')
        self.assertEqual(profile['catalog_provenance']['revision'], HARNESS_REVISION)
        self.assertFalse(profile['capacity_verified'])
        self.assertIsNone(profile['model_max_output_tokens'])
        for changed in ({'base_url': 'https://gateway.invalid/anthropic'}, {'protocol': 'openai_chat_completions'},
                        {'model': 'deepseek-flash-alias'}, {'base_url': 'http://api.deepseek.com/anthropic'}):
            self.assertIsNone(resolve_profile({**official, **changed})['context_window'])

    def test_manual_then_endpoint_then_catalog_precedence_reports_conflicts(self):
        base = config(protocol='anthropic_messages', base_url='https://api.deepseek.com/anthropic',
                      model='deepseek-flash', runtime_context_cap=2_000_000,
                      endpoint_model_metadata={'id': 'deepseek-flash', 'context_window': 300_000,
                                               'max_output_tokens': 16_000})
        remote = resolve_profile(base)
        self.assertEqual(remote['context_window'], 300_000)
        self.assertEqual(remote['capacity_sources']['context_window'], 'endpoint_metadata')
        manual = resolve_profile({**base, 'model_context_window': 400_000, 'model_max_output_tokens': 32_000})
        self.assertEqual(manual['effective_context_window'], 400_000)
        self.assertEqual(manual['model_max_output_tokens'], 32_000)
        self.assertEqual(manual['capacity_sources']['context_window'], 'explicit_declaration')
        self.assertEqual({item['field'] for item in manual['capacity_conflicts']}, {'context_window', 'max_output_tokens'})
        self.assertFalse(manual['capacity_verified'])

    def test_other_model_metadata_is_not_applied(self):
        profile = resolve_profile(config(endpoint_model_metadata={'id': 'different', 'context_window': 100_000}))
        self.assertIsNone(profile['context_window'])

    def test_frozen_profile_survives_catalog_change_and_returned_values_are_copies(self):
        frozen = normalize_config(config(protocol='anthropic_messages', base_url='https://api.deepseek.com/anthropic',
                                         model='deepseek-flash', revision=5))
        original = deepcopy(frozen)
        with patch('workbench_platform.agent.model_profiles.OFFICIAL_CATALOG', ()):
            resolved = resolve_profile(frozen)
            self.assertEqual(resolved, original['model_profile'])
            resolved['capacity_sources']['context_window'] = 'tampered'
            self.assertEqual(frozen, original)
            self.assertIsNone(resolve_profile({**frozen, 'revision': 6})['context_window'])
        changed_output = resolve_profile({**frozen, 'max_output_tokens': 8192})
        self.assertEqual(changed_output['output_reserve'], 8192)
        self.assertNotEqual(changed_output['profile_id'], frozen['model_profile']['profile_id'])

    def test_contract_alias_conflicts_output_capacity_and_unbounded_product_limits(self):
        for changes in ({'context_cap': 65536, 'runtime_context_cap': 32768},
                        {'model_context_window': 4096, 'max_output_tokens': 4096},
                        {'model_max_output_tokens': 1024}, {'safety_margin_tokens': 65536}):
            with self.assertRaises(ValidationError):
                ControllerDraft.model_validate(config(**changes))
        large = ControllerDraft.model_validate(config(context_cap=2_000_000, max_output_tokens=256_000))
        self.assertIsNone(large.round_limit)
        self.assertIsNone(large.tool_limit)
        self.assertIsNone(large.active_seconds_limit)
        self.assertEqual(large.context_cap, 2_000_000)
        runtime_only = ControllerDraft.model_validate({**config(), 'context_cap': 32768, 'runtime_context_cap': 32768})
        self.assertEqual(runtime_only.runtime_context_cap, 32768)

    def test_nested_metadata_allowlist_does_not_mislabel_input_only_capacity(self):
        item = candidate({'id': 'alias', 'metadata': {'context': {'contextWindow': 128_000}},
                          'top_provider': {'max_completion_tokens': 64_000}, 'secret': 'discard-me'})
        self.assertEqual(item['context_window'], 128_000)
        self.assertEqual(item['max_output_tokens'], 64_000)
        self.assertNotIn('secret', item)
        input_only = candidate({'id': 'small', 'max_input_tokens': 12_000, 'context_length': True})
        self.assertIsNone(input_only['context_window'])
        self.assertEqual(input_only['max_input_tokens'], 12_000)
        self.assertIsNone(candidate({'id': 'bad\nmodel'}))


class ConnectionProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.store = AgentStore(Path(self.temporary.name) / 'agent')
        self.vault = MemoryVault()
        self.connection = ControllerConnection(self.store, vault=self.vault, production_preflight=lambda _: {'passed': True})
        self.probe_patch = patch('workbench_platform.agent.connection.probe', local_probe)
        self.probe_patch.start()

    def tearDown(self):
        self.probe_patch.stop()
        self.temporary.cleanup()

    async def test_connection_revisions_keep_frozen_metadata_and_credentials(self):
        await self.connection.models({**config(), 'api_key': 'secret-one'}, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={'data': [{'id': 'gpt-6.1-sol', 'context_window': 128_000}]})))
        first = await self.connection.save({**config(), 'api_key': 'secret-one'})
        await self.connection.models({**config(), 'api_key': 'secret-one'}, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={'data': [{'id': 'gpt-6.1-sol', 'context_window': 200_000}]})))
        second = await self.connection.save({**config(), 'api_key': ''})
        old, key = self.connection.resolve(first['revision'])
        self.assertEqual(key, 'secret-one')
        self.assertEqual(old['model_profile']['context_window'], 128_000)
        self.assertEqual(second['model_profile']['context_window'], 200_000)
        self.assertEqual(old['model_profile']['connection_revision'], first['revision'])
        self.assertNotIn('credential_ref', first)
        self.connection.assert_current(first['revision'])

    async def test_deleting_revokes_archives_even_when_credential_deletion_fails_and_default_recreated(self):
        first = await self.connection.save({**config(), 'api_key': 'secret-one'})
        second = await self.connection.save({**config(), 'api_key': 'secret-two'})
        self.vault.fail_delete = True
        self.connection.clear()
        await self.connection.save({**config(), 'api_key': 'secret-three'})
        self.assertFalse(self.store.rows("SELECT key FROM settings WHERE key LIKE 'agent-controller-revision:%'"))
        for revision in (first['revision'], second['revision']):
            for operation in (self.connection.resolve, self.connection.assert_current):
                with self.assertRaises(ProviderFault) as caught:
                    operation(revision)
                self.assertEqual(caught.exception.code, 'config_changed')

    async def test_catalogs_do_not_cross_authenticated_accounts(self):
        await self.connection.models({**config(), 'api_key': 'secret-one'}, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={'data': [{'id': 'gpt-6.1-sol', 'context_window': 128_000}]})))
        saved = await self.connection.save({**config(), 'api_key': 'secret-two'})
        self.assertIsNone(saved['model_profile']['context_window'])
        self.assertNotIn('secret-one', self.store.path.read_bytes().decode('latin1'))

    async def test_data_and_models_map_are_combined_deduplicated_and_enriched(self):
        body = {'data': [{'id': 'a'}, {'id': 'b', 'context_length': 64_000}],
                'models': {'a': {'context': {'contextWindow': 128_000}, 'maxTokens': 16_000},
                           'c': {'metadata': {'limits': {'max_output_tokens': 8192}}}}}
        view = await self.connection.models({**config(), 'api_key': 'secret'},
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
        items = {item['id']: item for item in view['models']}
        self.assertEqual(set(items), {'a', 'b', 'c'})
        self.assertEqual(items['a']['context_window'], 128_000)
        self.assertEqual(items['a']['max_output_tokens'], 16_000)
        self.assertEqual(items['c']['max_output_tokens'], 8192)

    async def test_discovery_bound_and_cyclic_pagination_rejected(self):
        for body in ({'data': [{'id': str(index)} for index in range(1001)]}, {'data': 'wrong'}):
            with self.assertRaises(ProviderFault) as caught:
                await self.connection.models({**config(), 'api_key': 'secret'}, transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json=body)))
            self.assertEqual(caught.exception.code, 'models_unavailable')
        calls = []
        def cycle(req):
            calls.append(req)
            return httpx.Response(200, json={'data': [], 'has_more': True, 'last_id': 'same'})
        with self.assertRaises(ProviderFault):
            await self.connection.models({**config(protocol='anthropic_messages'), 'api_key': 'secret'},
                                         transport=httpx.MockTransport(cycle))
        self.assertEqual(len(calls), 2)

    async def test_gateway_output_metadata_conflict_stops_before_model_probe(self):
        await self.connection.models({**config(), 'api_key': 'secret'}, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={'data': [{'id': 'gpt-6.1-sol', 'max_output_tokens': 1024}]})))
        with self.assertRaises(ValueError):
            await self.connection.save({**config(), 'api_key': 'secret'})
        self.assertFalse(self.connection.view()['configured'])
        manual = await self.connection.save({**config(), 'api_key': 'secret', 'model_max_output_tokens': 4096})
        self.assertEqual(manual['model_profile']['model_max_output_tokens'], 4096)


class ProviderWireProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_metrics_match_actual_httpx_utf8_wire_for_both_protocols(self):
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            settings = normalize_config(config(protocol=protocol, safety_margin_tokens=512))
            messages = [{'role': 'user', 'content': '核实金额与前导零：000001，10.00'}]
            system = '系统约束\n[平台运行环境]Windows\n[Skill目录]按需加载'
            payload = payload_for(settings, messages, system=system, schemas={
                'read': {'type': 'object', 'properties': {'name': {'type': 'string', 'description': '名称'}}, 'required': ['name']}})
            metrics = request_metrics(settings, payload, messages=messages, system=system)
            sent = []
            def response(req):
                sent.append(req.content)
                raw = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '核实完成'}}]} if protocol == 'openai_chat_completions' else {
                    'role': 'assistant', 'type': 'message', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '核实完成'}]}
                return httpx.Response(200, json=raw)
            result = await request(settings, 'local-secret', payload, 'wire', transport=httpx.MockTransport(response))
            self.assertEqual(sent[0], wire_json(payload).encode('utf-8'))
            self.assertEqual(metrics['payload_bytes'], len(sent[0]))
            self.assertEqual(metrics['payload_sha256'], hashlib.sha256(sent[0]).hexdigest())
            self.assertEqual(sum(metrics['component_tokens'].values()), metrics['input_tokens_estimate'])
            self.assertEqual(metrics['available_input_tokens'], 65536 - 2048 - 512)
            self.assertEqual(result['usage'], {'input_tokens': None, 'output_tokens': None})

    async def test_only_confirmed_context_rejections_and_rate_limits_are_classified(self):
        cases = [
            (400, {'error': {'code': 'context_length_exceeded', 'message': 'local-secret'}}, 'CONTEXT_WINDOW_EXCEEDED', False),
            (400, {'error': {'type': 'invalid_request_error', 'message': 'prompt is too long: 2000 > 1000'}}, 'CONTEXT_WINDOW_EXCEEDED', False),
            (422, {'error': {'message': 'This model maximum context length is 8192 tokens'}}, 'CONTEXT_WINDOW_EXCEEDED', False),
            (400, {'error': {'message': 'context_window value is invalid'}}, 'provider_unavailable', False),
            (500, {'error': {'code': 'context_length_exceeded'}}, 'provider_unavailable', False),
            (429, {'error': {'code': 'rate_limit_exceeded'}}, 'rate_limited', True),
            (429, {'error': {'code': 'insufficient_quota'}}, 'provider_unavailable', False),
            (401, {'error': {'message': 'local-secret'}}, 'authentication', False),
        ]
        for status, body, expected, retryable in cases:
            with self.subTest(status=status, expected=expected), self.assertRaises(ProviderFault) as caught:
                await request(config(), 'local-secret', payload_for(config(), [{'role': 'user', 'content': 'test'}]), 'error',
                              transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)))
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn('local-secret', str(caught.exception))

    async def test_oversized_error_and_unknown_transport_never_gain_retry_authority(self):
        with self.assertRaises(ProviderFault) as oversized:
            await request(config(), 'local-secret', payload_for(config(), [{'role': 'user', 'content': 'test'}]), 'error',
                transport=httpx.MockTransport(lambda _: httpx.Response(400, content=b'x' * (MAX_ERROR_BYTES + 1))))
        self.assertEqual(oversized.exception.code, 'provider_unavailable')
        def timeout(_):
            raise httpx.ReadTimeout('unknown forwarding state')
        with self.assertRaises(ProviderFault) as unknown:
            await request(config(), 'local-secret', payload_for(config(), [{'role': 'user', 'content': 'test'}]), 'error',
                          transport=httpx.MockTransport(timeout))
        self.assertEqual(unknown.exception.code, 'response_unknown')
        self.assertFalse(unknown.exception.retryable)

    async def test_explicit_sse_errors_match_http_classification_without_body_leakage(self):
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            for error, expected, retryable in (
                ({'type': 'invalid_request_error', 'message': 'prompt is too long: local-secret'}, 'CONTEXT_WINDOW_EXCEEDED', False),
                ({'code': 'context_length_exceeded'}, 'CONTEXT_WINDOW_EXCEEDED', False),
                ({'type': 'rate_limit_error'}, 'rate_limited', True),
                ({'type': 'overloaded_error', 'message': 'local-secret'}, 'provider_unavailable', False),
                ('context_length_exceeded', 'provider_unavailable', False),
            ):
                event = json.dumps({'type': 'error', 'error': error})
                with self.subTest(protocol=protocol, error=error), self.assertRaises(ProviderFault) as caught:
                    await request(config(protocol=protocol, streaming=True), 'local-secret', {'stream': True}, 'stream-error',
                        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                                                             content=('data: ' + event + '\n\n').encode())))
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn('local-secret', str(caught.exception))

    async def test_sse_context_like_assistant_content_does_not_authorize_recovery(self):
        event = {'choices': [{'index': 0, 'delta': {'content': 'context_length_exceeded: local-secret'}, 'finish_reason': None}]}
        with self.assertRaises(ProviderFault) as caught:
            await request(config(streaming=True), 'local-secret', {'stream': True}, 'incomplete-stream',
                transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'},
                    content=('data: ' + json.dumps(event) + '\n\n').encode())))
        self.assertEqual(caught.exception.code, 'truncated_response')
        self.assertFalse(caught.exception.retryable)

    async def test_sse_rejection_after_tool_prefix_does_not_return_partial_calls(self):
        prefix = {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'pending', 'type': 'function',
                   'function': {'name': 'data_import', 'arguments': '{"name":'}}]}, 'finish_reason': None}]}
        error = {'error': {'code': 'context_window_exceeded'}}
        wire = ''.join('data: ' + json.dumps(value) + '\n\n' for value in (prefix, error))
        with self.assertRaises(ProviderFault) as caught:
            await request(config(streaming=True), 'local-secret', {'stream': True}, 'partial-tool',
                transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire.encode())))
        self.assertEqual(caught.exception.code, 'CONTEXT_WINDOW_EXCEEDED')

    async def test_sse_oversized_error_never_classifies_and_transport_remains_unknown(self):
        wire = 'data: ' + json.dumps({'error': {'code': 'context_window_exceeded', 'message': 'x' * MAX_ERROR_BYTES}}) + '\n\n'
        with self.assertRaises(ProviderFault) as oversized:
            await request(config(streaming=True), 'local-secret', {'stream': True}, 'large-error',
                transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire.encode())))
        self.assertEqual(oversized.exception.code, 'provider_unavailable')
        self.assertFalse(oversized.exception.retryable)

        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices": []}\n\n'
                raise httpx.ReadError('Unknown upstream state')

        with self.assertRaises(ProviderFault) as unknown:
            await request(config(streaming=True), 'local-secret', {'stream': True}, 'lost-stream',
                transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=BrokenStream())))
        self.assertEqual(unknown.exception.code, 'response_unknown')
        self.assertFalse(unknown.exception.retryable)


if __name__ == '__main__':
    unittest.main(verbosity=2)
