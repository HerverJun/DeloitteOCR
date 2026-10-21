"""Authoritative output limits precede argument parsing; serial tool controls reach HTTP."""
import json
import unittest

import httpx

from workbench_platform.agent.providers import ProviderFault, payload_for, request
from test_streaming_preview import config, sse, tool_parts, native_json


SECRET = 'partial-secret-never-in-diagnostics'
SCHEMAS = {'conversation_answer': {'type': 'object', 'properties': {'content': {'type': 'string'}},
                                    'required': ['content'], 'additionalProperties': False}}


class ProviderLimitTests(unittest.IsolatedAsyncioTestCase):
    async def failure(self, protocol, wire, *, stream=True):
        sent = []
        def endpoint(incoming):
            sent.append(json.loads(incoming.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream' if stream else 'application/json'},
                                  content=wire)
        with self.assertRaises(ProviderFault) as caught:
            await request(config(protocol), SECRET, {'stream': stream}, 'no-partial-response',
                          transport=httpx.MockTransport(endpoint))
        self.assertEqual(len(sent), 1)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, json.dumps(caught.exception.response_diagnostics))
        return caught.exception

    async def test_chat_length_keeps_exact_argument_byte_counts_for_complete_and_partial_calls(self):
        arguments = ['{"content":"完整但整批不可执行"}', '{"private":"' + SECRET + '","rows":["中文']
        prefix = {'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': index, 'id': 'private-call-' + str(index), 'type': 'function',
             'function': {'name': 'conversation_answer', 'arguments': value}}
            for index, value in enumerate(arguments)]}, 'finish_reason': None}]}
        terminal = sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'length'}]})
        for trailer in (b'data: [DONE]\n\n', b'', b'data: malformed-trailer\n\n'):
            with self.subTest(trailer=trailer):
                error = await self.failure('openai_chat_completions', sse(prefix) + terminal + trailer)
                self.assertEqual(error.code, 'truncated_response')
                sizes = [len(value.encode('utf-8')) for value in arguments]
                self.assertEqual(error.response_diagnostics, {'protocol': 'openai_chat_completions',
                    'finish_reason': 'length', 'observed_tool_calls': 2,
                    'tool_argument_bytes': sum(sizes), 'tool_argument_bytes_per_call': sizes})
                self.assertNotIn('conversation_answer', str(error))
                self.assertNotIn('private-call', str(error))

    async def test_chat_length_on_same_event_as_incomplete_arguments_wins(self):
        event = {'choices': [{'index': 0, 'delta': {'tool_calls': [{
            'index': 0, 'id': 'partial', 'type': 'function',
            'function': {'name': 'data_import', 'arguments': '{"name":'}}]}, 'finish_reason': 'length'}]}
        error = await self.failure('openai_chat_completions', sse(event))
        self.assertEqual(error.code, 'truncated_response')
        self.assertEqual(error.response_diagnostics['finish_reason'], 'length')
        self.assertEqual(error.response_diagnostics['tool_argument_bytes'], len(b'{"name":'))

    async def test_anthropic_max_tokens_wins_over_partial_json_and_unclosed_block(self):
        argument = '{"content":"中文 ' + SECRET
        parts = tool_parts('anthropic_messages', [argument])
        terminal = sse({'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}, 'usage': {'output_tokens': 2048}})
        for close_block in (False, True):
            with self.subTest(close_block=close_block):
                wire = b''.join(parts[:-3] + ([parts[-3]] if close_block else []) + [terminal, parts[-1]])
                error = await self.failure('anthropic_messages', wire)
                self.assertEqual(error.code, 'truncated_response')
                self.assertEqual(error.response_diagnostics, {'protocol': 'anthropic_messages',
                    'finish_reason': 'max_tokens', 'observed_tool_calls': 1,
                    'tool_argument_bytes': len(argument.encode('utf-8')),
                    'tool_argument_bytes_per_call': [len(argument.encode('utf-8'))]})

    async def test_nonstream_authoritative_limits_also_precede_tool_validation(self):
        chat = native_json('openai_chat_completions', 'conversation_answer', {'content': 'unused'})
        chat['choices'][0]['finish_reason'] = 'length'
        chat['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{"private":"' + SECRET
        anthropic = native_json('anthropic_messages', 'conversation_answer', {'content': 'unused'})
        anthropic['stop_reason'] = 'max_tokens'
        anthropic['content'][0]['input'] = {'private': SECRET}
        for protocol, raw, reason in (('openai_chat_completions', chat, 'length'),
                                      ('anthropic_messages', anthropic, 'max_tokens')):
            with self.subTest(protocol=protocol):
                error = await self.failure(protocol, json.dumps(raw).encode(), stream=False)
                self.assertEqual(error.code, 'truncated_response')
                self.assertEqual(error.response_diagnostics['finish_reason'], reason)
                self.assertEqual(error.response_diagnostics['observed_tool_calls'], 1)

    async def test_eof_without_authoritative_reason_never_claims_output_limit(self):
        parts = tool_parts('openai_chat_completions', ['{"content":"draft'])[:-2]
        error = await self.failure('openai_chat_completions', b''.join(parts))
        self.assertEqual(error.code, 'truncated_response')
        self.assertEqual(error.response_diagnostics['finish_reason'], 'eof')
        self.assertEqual(error.response_diagnostics['observed_tool_calls'], 1)

    async def test_malformed_arguments_with_normal_finish_remain_invalid_and_unexecuted(self):
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            with self.subTest(protocol=protocol):
                error = await self.failure(protocol, b''.join(tool_parts(protocol, ['{"content":"draft'])))
                self.assertEqual(error.code, 'invalid_response')
                self.assertIsNone(error.response_diagnostics)

    async def test_standard_serial_tool_controls_are_on_wire_only_when_tools_exist(self):
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            for tools in (True, False):
                with self.subTest(protocol=protocol, tools=tools):
                    configuration = {**config(protocol, streaming=False), 'max_output_tokens': 2048}
                    payload = payload_for(configuration, [{'role': 'user', 'content': 'Reply'}],
                                          schemas=SCHEMAS if tools else {})
                    sent = []
                    def endpoint(incoming):
                        sent.append(json.loads(incoming.content))
                        return httpx.Response(200, json=native_json(protocol,
                            'conversation_answer' if tools else None, {'content': 'done'}, 'done' if not tools else ''))
                    result = await request(configuration, SECRET, payload, 'serial-controls', transport=httpx.MockTransport(endpoint))
                    body = sent[0]
                    if protocol == 'openai_chat_completions':
                        self.assertEqual(body['max_completion_tokens'], 2048)
                        self.assertEqual(body.get('parallel_tool_calls'), False if tools else None)
                        self.assertNotIn('tool_choice', body)
                    else:
                        self.assertEqual(body['max_tokens'], 2048)
                        self.assertEqual(body.get('tool_choice'), {'type': 'auto', 'disable_parallel_tool_use': True} if tools else None)
                        self.assertNotIn('parallel_tool_calls', body)
                    self.assertEqual(len(result['calls']), 1 if tools else 0)

    async def test_gateway_rejection_of_standard_control_is_explicit_without_fallback_retry(self):
        for protocol in ('openai_chat_completions', 'anthropic_messages'):
            sent = []
            def endpoint(incoming):
                sent.append(json.loads(incoming.content))
                return httpx.Response(400, json={'error': {'type': 'invalid_request_error',
                    'message': 'Unsupported serial control; ' + SECRET}})
            payload = payload_for(config(protocol), [{'role': 'user', 'content': 'Reply'}], schemas=SCHEMAS)
            with self.subTest(protocol=protocol), self.assertRaises(ProviderFault) as caught:
                await request(config(protocol), SECRET, payload, 'reject-control', transport=httpx.MockTransport(endpoint))
            self.assertEqual(len(sent), 1)
            self.assertEqual(caught.exception.code, 'provider_unavailable')
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn(SECRET, str(caught.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
