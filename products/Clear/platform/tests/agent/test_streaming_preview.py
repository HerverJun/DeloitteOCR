"""Delayed native SSE, safe display-only argument prefixes, actual capability modes."""
import asyncio
import json
from pathlib import Path
import unittest

import httpx

from workbench_platform.agent.connection import probe
from workbench_platform.agent.providers import ProviderFault, request, MAX_RESPONSE_BYTES
from workbench_platform.agent.streaming import safe_prefix


ROOT = Path('F:/OCR/output/agent-streaming-ux-dev-20261005/evidence')
PROTOCOLS = ('openai_chat_completions', 'anthropic_messages')
SECRET = 'stream-test-key-92831'


def config(protocol, streaming=True):
    return {'protocol': protocol, 'base_url': 'http://127.0.0.1:54321/v1',
            'model': 'local-stream-fixture', 'streaming': streaming, 'total_seconds': 10}


def sse(value):
    return ('data: ' + json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n\n').encode()


def native_json(protocol, tool, arguments, content=''):
    if protocol == 'openai_chat_completions':
        message = {'role': 'assistant', 'content': content}
        if tool:
            message['tool_calls'] = [{'id': 'call_0', 'type': 'function',
                'function': {'name': tool, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]
        return {'model': 'actual-fixture', 'choices': [
            {'finish_reason': 'tool_calls' if tool else 'stop', 'message': message}]}
    blocks = ([{'type': 'text', 'text': content}] if content else []) + ([
        {'type': 'tool_use', 'id': 'call_0', 'name': tool, 'input': arguments}] if tool else [])
    return {'role': 'assistant', 'type': 'message', 'model': 'actual-fixture',
            'stop_reason': 'tool_use' if tool else 'end_turn', 'content': blocks}


def tool_parts(protocol, fragments, tool='conversation_answer', content=''):
    if protocol == 'openai_chat_completions':
        parts = [sse({'model': 'actual-fixture', 'choices': [{'index': 0,
            'delta': {'tool_calls': [{'index': 0, 'id': 'call_0', 'type': 'function',
                'function': {'name': tool, 'arguments': fragments[0]}}],
                      **({'content': content} if content else {})}, 'finish_reason': None}]})]
        parts += [sse({'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 0, 'function': {'arguments': fragment}}]}, 'finish_reason': None}]})
            for fragment in fragments[1:]]
        return parts + [sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}), b'data: [DONE]\n\n']
    start = sse({'type': 'message_start', 'message': {'id': 'native-stream', 'role': 'assistant',
        'type': 'message', 'model': 'actual-fixture', 'content': [], 'stop_reason': None,
        'usage': {'input_tokens': 3, 'output_tokens': 0}}})
    parts, index = [start], 0
    if content:
        parts += [sse({'type': 'content_block_start', 'index': 0,
                      'content_block': {'type': 'text', 'text': content}}),
                  sse({'type': 'content_block_stop', 'index': 0})]
        index = 1
    parts.append(sse({'type': 'content_block_start', 'index': index,
                     'content_block': {'type': 'tool_use', 'id': 'call_0', 'name': tool, 'input': {}}}))
    parts += [sse({'type': 'content_block_delta', 'index': index,
                  'delta': {'type': 'input_json_delta', 'partial_json': fragment}}) for fragment in fragments]
    return parts + [sse({'type': 'content_block_stop', 'index': index}),
        sse({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 5}}),
        sse({'type': 'message_stop'})]


class DelayedStream(httpx.AsyncByteStream):
    def __init__(self, parts, pause_at=None):
        self.parts, self.pause_at = parts, pause_at
        self.paused, self.release = asyncio.Event(), asyncio.Event()
        self.closed, self.exhausted = False, False

    async def __aiter__(self):
        for index, part in enumerate(self.parts):
            if index == self.pause_at:
                self.paused.set()
                await self.release.wait()
            yield part
            await asyncio.sleep(0)
        self.exhausted = True

    async def aclose(self):
        self.closed = True


class StreamingPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, protocol, parts, *, secret=SECRET):
        frames, deltas, modes, sent = [], [], [], []
        stream = DelayedStream(parts)
        async def preview(frame): frames.append(frame)
        async def delta(text): deltas.append(text)
        async def mode(value): modes.append(value)
        def endpoint(req):
            sent.append(json.loads(req.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        response = await request(config(protocol), secret, {}, 'display-request',
            transport=httpx.MockTransport(endpoint), on_preview=preview, on_delta=delta, on_transport=mode)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]['stream'])
        self.assertEqual(modes, [{'mode': 'sse', 'requested': True}])
        self.assertEqual(set(response), {'id', 'role', 'content', 'calls', 'protocol', 'private',
            'blocks', 'usage', 'stop_reason', 'returned_model'})
        self.assertEqual(response['returned_model'], 'actual-fixture')
        return response, frames, deltas

    async def test_answer_parameter_is_visible_while_http_response_is_still_paused(self):
        evidence = []
        for protocol in PROTOCOLS:
            fragments = ['{"content":"首段已经可见', '，后半段随后到达"}']
            parts = tool_parts(protocol, fragments)
            stream = DelayedStream(parts, pause_at=1 if protocol == PROTOCOLS[0] else 3)
            ready, frames = asyncio.Event(), []
            async def preview(frame):
                frames.append(frame)
                if frame['source'] == 'answer' and '首段' in frame['content']:
                    ready.set()
            def endpoint(req):
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
            task = asyncio.create_task(request(config(protocol), SECRET, {}, 'early-preview',
                transport=httpx.MockTransport(endpoint), on_preview=preview))
            try:
                await asyncio.wait_for(ready.wait(), 3)
                await asyncio.wait_for(stream.paused.wait(), 3)
                self.assertFalse(task.done(), 'Preview must arrive before the remaining HTTP chunks')
                self.assertFalse(stream.exhausted)
                early = [frame.copy() for frame in frames]
                stream.release.set()
                response = await task
                self.assertEqual(response['calls'][0]['arguments']['content'], '首段已经可见，后半段随后到达')
                self.assertEqual(frames[-1]['content'], response['calls'][0]['arguments']['content'])
                self.assertEqual(frames[-1]['call_id'], 'call_0')
                evidence.append({'protocol': protocol, 'early_frames': early,
                    'preview_before_remaining_chunks': True, 'final_frame': frames[-1]})
            finally:
                stream.release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT/'provider-delayed-preview.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    async def test_every_character_escape_unicode_and_nested_fields_remain_safe(self):
        answer = '你好\n金额 "00123" \\ 路径 😀 尾'
        arguments = {'nested': {'content': 'nested body must stay private'}, 'private': 'other-field-must-stay-private',
                     'content': answer, 'references': []}
        for protocol in PROTOCOLS:
            response, frames, deltas = await self.execute(protocol,
                tool_parts(protocol, list(json.dumps(arguments, ensure_ascii=True))))
            self.assertEqual(response['calls'][0]['arguments'], arguments)
            self.assertEqual(frames[-1]['content'], answer)
            self.assertEqual(deltas, [], 'Tool parameters must not enter the legacy ordinary-text channel')
            for frame in frames:
                frame['content'].encode('utf-8')
                self.assertNotIn('nested body', frame['content'])
                self.assertNotIn('other-field', frame['content'])
            self.assertEqual({frame['source'] for frame in frames}, {'answer'})

    async def test_key_split_unicode_encoded_and_preview_cap_never_leak(self):
        for protocol in PROTOCOLS:
            value = '前言 ' + SECRET + ' 后文'
            serialized = json.dumps({'content': value}, ensure_ascii=True)
            key_start = serialized.index(SECRET)
            parts = [serialized[:key_start + 7], serialized[key_start + 7:key_start + 13], serialized[key_start + 13:]]
            response, frames, _ = await self.execute(protocol, tool_parts(protocol, parts))
            self.assertEqual(response['calls'][0]['arguments']['content'], value.replace(SECRET, '[redacted]'))
            self.assertNotIn(SECRET, json.dumps(frames))
            self.assertNotIn(SECRET[:7], frames[0]['content'])
            # Encoded ASCII key bytes must be detected after argument decoding.
            encoded = ''.join('\\u%04x' % ord(character) for character in SECRET)
            arguments = '{"content":"前言 ' + encoded + ' 后文"}'
            _, encoded_frames, _ = await self.execute(protocol, tool_parts(protocol, list(arguments)))
            self.assertNotIn(SECRET, json.dumps(encoded_frames))
            self.assertIn('[redacted]', encoded_frames[-1]['content'])
            # A key crossing the visible cap is redacted before slicing, even
            # on the final frame. The authoritative response is never sliced.
            long = '甲' * 11998 + SECRET + '尾'
            response, long_frames, _ = await self.execute(protocol, tool_parts(protocol,
                [json.dumps({'content': long}, ensure_ascii=False)]))
            self.assertTrue(long_frames[-1]['truncated'])
            self.assertLessEqual(len(long_frames[-1]['content']), 12000)
            self.assertNotIn(SECRET[:2], long_frames[-1]['content'])
            self.assertEqual(response['calls'][0]['arguments']['content'], long.replace(SECRET, '[redacted]'))
        for length in range(len(SECRET)):
            self.assertEqual(safe_prefix('前言 ' + SECRET[:length], SECRET), '前言 ')
        self.assertEqual(safe_prefix('前言 ' + SECRET, SECRET), '前言 [redacted]')

    async def test_interleaved_calls_keep_protocol_indexes_and_final_call_id(self):
        # One call receives its remaining ID after its content is already
        # complete. The final metadata-only callback must update that identity.
        events = [
            {'choices': [{'index': 0, 'delta': {'tool_calls': [
                {'index': 0, 'id': 'call_', 'type': 'function', 'function': {'name': 'conversation_', 'arguments': '{"content":"答'}}]}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {'tool_calls': [
                {'index': 1, 'id': 'call_b', 'type': 'function', 'function': {'name': 'delivery_publish', 'arguments': '{"summary":"候选交付"}'}},
                {'index': 0, 'function': {'name': 'answer', 'arguments': '案"}'}}]}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'a'}]}, 'finish_reason': 'tool_calls'}]},
        ]
        events[0]['model'] = 'actual-fixture'
        response, frames, _ = await self.execute(PROTOCOLS[0], [*(sse(event) for event in events), b'data: [DONE]\n\n'])
        self.assertEqual(response['calls'][0]['call_id'], 'call_a')
        self.assertEqual({frame['block'] for frame in frames}, {'call:0', 'call:1'})
        answers = [frame for frame in frames if frame['source'] == 'answer']
        self.assertEqual(answers[0]['call_id'], 'call_')
        self.assertEqual(answers[-1]['call_id'], 'call_a')
        self.assertEqual(answers[-1]['content'], '答案')
        self.assertTrue(any(frame['source'] == 'delivery' and frame['content'] == '候选交付' for frame in frames))
        anthropic = [sse({'type': 'message_start', 'message': {'role': 'assistant', 'type': 'message',
            'model': 'actual-fixture', 'content': [], 'usage': {}, 'stop_reason': None}})]
        for index, tool in [(0, 'delivery_publish'), (1, 'conversation_answer')]:
            anthropic.append(sse({'type': 'content_block_start', 'index': index,
                'content_block': {'type': 'tool_use', 'id': 'call_' + str(index), 'name': tool, 'input': {}}}))
        for index, fragment in [(1, '{"content":"答'), (0, '{"summary":"候选'), (1, '案"}'), (0, '交付"}')]:
            anthropic.append(sse({'type': 'content_block_delta', 'index': index,
                'delta': {'type': 'input_json_delta', 'partial_json': fragment}}))
        anthropic += [sse({'type': 'content_block_stop', 'index': index}) for index in (0, 1)]
        anthropic += [sse({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {}}), sse({'type': 'message_stop'})]
        _, frames, _ = await self.execute(PROTOCOLS[1], anthropic)
        self.assertTrue(any(frame['block'] == 'call:1' and frame['source'] == 'answer' for frame in frames))
        self.assertTrue(any(frame['block'] == 'call:0' and frame['source'] == 'delivery' for frame in frames))

    async def test_private_reasoning_and_unregistered_parameter_fields_have_no_preview(self):
        for protocol in PROTOCOLS:
            parts = tool_parts(protocol, ['{"content":"never-show-this-shell-script"}'], tool='shell_exec')
            response, frames, _ = await self.execute(protocol, parts)
            self.assertEqual(frames, [])
            self.assertEqual(response['calls'][0]['arguments']['content'], 'never-show-this-shell-script')
        parts = tool_parts(PROTOCOLS[0], ['{"content":"公开回答"}'])
        private = sse({'choices': [{'index': 0, 'delta': {'reasoning_content': 'private hidden trace'}, 'finish_reason': None}]})
        response, frames, _ = await self.execute(PROTOCOLS[0], [private, *parts])
        self.assertEqual(response['private']['reasoning_content'], 'private hidden trace')
        self.assertNotIn('private hidden trace', json.dumps(frames))

    async def test_eof_malformed_duplicate_and_length_never_return_a_confirmed_response(self):
        for protocol in PROTOCOLS:
            cases = [tool_parts(protocol, ['{"content":"可见草稿"}'])[:-1],
                     tool_parts(protocol, ['{"content":"可见草稿', '","content":"重复键"}']),
                     tool_parts(protocol, ['{"content":"可见草稿'])]
            for parts in cases:
                frames = []
                async def preview(frame): frames.append(frame)
                def endpoint(req):
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=DelayedStream(parts))
                with self.assertRaises(ProviderFault):
                    await request(config(protocol), SECRET, {}, 'incomplete',
                        transport=httpx.MockTransport(endpoint), on_preview=preview)
                self.assertTrue(frames)
                self.assertTrue(all('confirmed' not in frame and 'completed' not in frame for frame in frames))
        parts = tool_parts(PROTOCOLS[0], ['{"content":"可见草稿"}'])
        parts[-2] = sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'length'}]})
        with self.assertRaises(ProviderFault) as caught:
            await self.execute(PROTOCOLS[0], parts)
        self.assertEqual(caught.exception.code, 'truncated_response')

    async def test_answer_boundary_capacity_and_total_response_size_are_bounded(self):
        for protocol in PROTOCOLS:
            for length in (12000, 12001):
                value = '甲' * length
                arguments = json.dumps({'content': value}, ensure_ascii=False)
                chunks = [arguments[offset:offset + 200] for offset in range(0, len(arguments), 200)]
                response, frames, _ = await self.execute(protocol, tool_parts(protocol, chunks))
                self.assertEqual(response['calls'][0]['arguments']['content'], value)
                self.assertEqual(frames[-1]['content'], value[:12000])
                self.assertEqual(frames[-1]['truncated'], length > 12000)
            enormous = sse({'choices': [{'delta': {'content': 'x' * (MAX_RESPONSE_BYTES + 1)}}]})
            def endpoint(req):
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=DelayedStream([enormous]))
            with self.assertRaises(ProviderFault) as caught:
                await request(config(protocol), SECRET, {'stream': True}, 'too-large', transport=httpx.MockTransport(endpoint))
            self.assertEqual(caught.exception.code, 'invalid_response')

    async def test_json_fallback_disabled_and_http_failure_are_observed_without_retry(self):
        for protocol in PROTOCOLS:
            for enabled in (True, False):
                frames, modes, sent = [], [], []
                async def preview(frame): frames.append(frame)
                async def mode(value): modes.append(value)
                def endpoint(req):
                    sent.append(json.loads(req.content))
                    return httpx.Response(200, json=native_json(protocol, 'conversation_answer', {'content': '整包回答'}))
                response = await request(config(protocol, enabled), SECRET, {'stream': True}, 'json-fallback',
                    transport=httpx.MockTransport(endpoint), on_preview=preview, on_transport=mode)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]['stream'], enabled)
                self.assertEqual(modes, [{'mode': 'json' if enabled else 'disabled', 'requested': enabled}])
                self.assertEqual(frames, [])
                self.assertEqual(response['calls'][0]['arguments']['content'], '整包回答')
            sent = []
            def unauthorized(req):
                sent.append(req)
                return httpx.Response(401, json={'error': 'private'})
            with self.assertRaises(ProviderFault) as caught:
                await request(config(protocol), SECRET, {'stream': True}, 'auth-failure',
                    transport=httpx.MockTransport(unauthorized))
            self.assertEqual(caught.exception.code, 'authentication')
            self.assertEqual(len(sent), 1)


class CapabilityProbeTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, protocol, mode='sse', streaming=True):
        calls = []
        def endpoint(req):
            body = json.loads(req.content)
            calls.append(body)
            if len(calls) == 1:
                value = body['messages'][-1]['content'].split('value ')[-1]
                return httpx.Response(200, json=native_json(protocol, 'probe_echo', {'value': value}))
            if len(calls) == 2:
                result = body['messages'][-1]['content']
                receipt = json.loads(result if isinstance(result, str) else result[0]['content'])['receipt']
                return httpx.Response(200, json=native_json(protocol, None, {}, receipt))
            self.assertEqual(len(calls), 3, 'Streaming capability probe must not retry or expand into more rounds')
            value = body['messages'][-1]['content'].split('Value: ')[-1]
            if mode == 'unsupported':
                return httpx.Response(400, json={'error': 'stream not supported'})
            if mode == 'json':
                return httpx.Response(200, json=native_json(protocol, 'conversation_answer', {'content': value}))
            serialized = json.dumps({'content': value})
            parts = tool_parts(protocol, [serialized[:40], serialized[40:]])
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=DelayedStream(parts))
        capability = await probe(config(protocol, streaming), SECRET, transport=httpx.MockTransport(endpoint))
        self.assertTrue(capability['native_tools'])
        self.assertTrue(capability['tool_result_consumption'])
        self.assertFalse(capability['business_data_sent'])
        self.assertEqual(len(calls), 3 if streaming else 2)
        self.assertEqual(capability['total_rounds'], len(calls))
        self.assertNotIn(SECRET, json.dumps(capability))
        return capability

    async def test_streamed_answer_only_is_supported_without_demanding_ordinary_text(self):
        for protocol in PROTOCOLS:
            capability = await self.execute(protocol)
            streaming = capability['streaming']
            self.assertEqual(streaming['status'], 'supported')
            self.assertEqual(streaming['mode'], 'sse')
            self.assertTrue(streaming['answer']['supported'])
            self.assertFalse(streaming['text']['supported'])
            self.assertTrue(streaming['detected_at'])

    async def test_json_or_rejected_sse_preserves_native_tool_capability(self):
        for protocol in PROTOCOLS:
            for mode, expected in [('json', 'fallback'), ('unsupported', 'unsupported')]:
                capability = await self.execute(protocol, mode)
                self.assertEqual(capability['streaming']['status'], expected)
                self.assertFalse(capability['streaming']['answer']['supported'])

    async def test_disabled_streaming_skips_only_optional_probe_and_keeps_explicit_facts(self):
        for protocol in PROTOCOLS:
            capability = await self.execute(protocol, streaming=False)
            self.assertEqual(capability['streaming']['status'], 'disabled')
            self.assertEqual(capability['streaming']['mode'], 'disabled')
            self.assertFalse(capability['streaming']['tested'])
            self.assertFalse(capability['streaming']['answer']['supported'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
