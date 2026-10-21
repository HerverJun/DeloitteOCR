"""Assemble complete native SSE responses before validating any tool call."""
import time
from functools import lru_cache
import httpx


PREVIEW_FIELDS = {'conversation_answer': ('content', 'answer', 12000),
                  'delivery_publish': ('summary', 'delivery', 2000)}
TEXT_PREVIEW_LIMIT = 12000


@lru_cache(maxsize=8)
def _prefix_lengths(secret):
    lengths = [0] * len(secret)
    matched = 0
    for index in range(1, len(secret)):
        while matched and secret[index] != secret[matched]:
            matched = lengths[matched - 1]
        if secret[index] == secret[matched]:
            matched += 1
        lengths[index] = matched
    return lengths


def safe_prefix(value, secret, *, final=False):
    """Redact full keys and withhold only a suffix that may become that key.

    Withholding the entire key length suppresses short answers, and can make a
    published prefix shrink when a later fragment completes a redacted key.
    """
    from .providers import redact
    value = redact(value, secret)
    if secret and not final:
        lengths, matched = _prefix_lengths(secret), 0
        for character in value[-(len(secret) - 1):] if len(secret) > 1 else '':
            while matched and character != secret[matched]:
                matched = lengths[matched - 1]
            if character == secret[matched]:
                matched += 1
        if matched:
            return value[:-matched]
    return value


class _StringToken:
    """Incremental JSON string decoder; never emits a partial escape/surrogate."""
    def __init__(self, limit):
        self.limit, self.parts, self.length = limit, [], 0
        self.escape, self.hex_digits, self.high = False, None, None

    def append(self, code):
        if self.high is not None:
            if not 0xDC00 <= code <= 0xDFFF:
                raise ValueError('Unpaired high surrogate')
            code = 0x10000 + ((self.high - 0xD800) << 10) + code - 0xDC00
            self.high = None
        elif 0xD800 <= code <= 0xDBFF:
            self.high = code
            return
        elif 0xDC00 <= code <= 0xDFFF:
            raise ValueError('Unpaired low surrogate')
        self.length += 1
        if len(self.parts) < self.limit:
            self.parts.append(chr(code))

    def feed(self, character):
        if self.hex_digits is not None:
            if character not in '0123456789abcdefABCDEF':
                raise ValueError('Invalid Unicode escape')
            self.hex_digits += character
            if len(self.hex_digits) == 4:
                self.append(int(self.hex_digits, 16))
                self.hex_digits = None
        elif self.escape:
            self.escape = False
            if character == 'u':
                self.hex_digits = ''
            else:
                escapes = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f',
                           'n': '\n', 'r': '\r', 't': '\t'}
                if character not in escapes:
                    raise ValueError('Invalid string escape')
                self.append(ord(escapes[character]))
        elif character == '"':
            if self.high is not None:
                raise ValueError('Unpaired high surrogate')
            return True
        elif character == '\\':
            self.escape = True
        elif ord(character) < 32:
            raise ValueError('Unescaped control character')
        else:
            self.append(ord(character))
        return False

    def text(self):
        return ''.join(self.parts)


class ArgumentPreview:
    """Read top-level allow-listed string fields without completing JSON.

    This parser only supplies an unconfirmed display prefix. The authoritative
    parser still rejects malformed/duplicate keys and validates the whole call.
    Nested values, other parameter fields and script bodies never enter previews.
    """
    def __init__(self, padding=0):
        self.depth, self.stage, self.key = 0, 'start', None
        self.token, self.token_kind = None, None
        self.values, self.lengths, self.seen, self.invalid = {}, {}, set(), False
        self.padding = padding

    def feed(self, fragment):
        if self.invalid:
            return
        try:
            for character in fragment:
                if self.token is not None:
                    closed = self.token.feed(character)
                    if closed:
                        if self.token_kind == 'key':
                            self.key = self.token.text() if self.token.length <= 128 else None
                            self.stage = 'colon'
                        elif self.depth == 1:
                            if self.token_kind == 'public':
                                self.values[self.key] = self.token.text()
                                self.lengths[self.key] = self.token.length
                            self.stage = 'comma'
                        self.token, self.token_kind = None, None
                    continue
                if character.isspace():
                    continue
                if self.depth > 1:
                    if character == '"':
                        self.token, self.token_kind = _StringToken(0), 'ignored'
                    elif character in '{[':
                        self.depth += 1
                    elif character in '}]':
                        self.depth -= 1
                        if self.depth == 1:
                            self.stage = 'comma'
                    continue
                if self.stage == 'start':
                    if character != '{':
                        raise ValueError('Arguments must be an object')
                    self.depth, self.stage = 1, 'key'
                elif self.stage == 'key':
                    if character == '}':
                        self.depth, self.stage = 0, 'done'
                    elif character == '"':
                        self.token, self.token_kind = _StringToken(128), 'key'
                    else:
                        raise ValueError('Expected property key')
                elif self.stage == 'colon':
                    if character != ':':
                        raise ValueError('Expected colon')
                    self.stage = 'value'
                elif self.stage == 'value':
                    if character == '"':
                        public = self.key in {'content', 'summary'}
                        if public and self.key in self.seen:
                            raise ValueError('Duplicate public field')
                        if public:
                            self.seen.add(self.key)
                        self.token = _StringToken((12000 if self.key == 'content' else 2000) + self.padding if public else 0)
                        self.token_kind = 'public' if public else 'ignored'
                    elif character in '{[':
                        self.depth, self.stage = 2, 'nested'
                    elif character in '-0123456789tfn':
                        self.stage = 'primitive'
                    else:
                        raise ValueError('Invalid property value')
                elif self.stage in {'comma', 'primitive'}:
                    if character == ',':
                        self.stage = 'key'
                    elif character == '}':
                        self.depth, self.stage = 0, 'done'
                    elif self.stage != 'primitive':
                        raise ValueError('Expected separator')
                else:
                    raise ValueError('Data after argument object')
            if self.token_kind == 'public':
                self.values[self.key] = self.token.text()
                self.lengths[self.key] = self.token.length
        except (ValueError, TypeError):
            self.invalid = True


async def collect(response, protocol, on_delta, secret, *, on_preview=None):
    from .providers import ProviderFault, strict_json, redact, MAX_RESPONSE_BYTES, MAX_ERROR_BYTES, provider_error_fault, truncated_response_fault
    total, terminated, data_lines = 0, False, []
    text, last_text, last_emit = '', '', time.monotonic()
    raw = {'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': []}, 'finish_reason': None}], 'usage': {}} if protocol == 'openai_chat_completions' else None
    calls, blocks, argument_text, closed_blocks = {}, {}, {}, set()
    terminal_reason = None
    previews, published = {}, {}
    def truncation(reason):
        if protocol == 'openai_chat_completions':
            arguments = [call['function']['arguments'] for _, call in sorted(calls.items())]
        else:
            arguments = [argument_text.get(index, block.get('input', {}))
                         for index, block in sorted(blocks.items()) if block.get('type') == 'tool_use']
        return truncated_response_fault(protocol, reason, arguments)
    async def emit(frame, final=False):
        if on_preview is None:
            return
        identity = frame['block'], frame['source']
        previous, emitted, previous_metadata = published.get(identity, ('', 0, None))
        limit = 2000 if frame['source'] == 'delivery' else TEXT_PREVIEW_LIMIT
        content = safe_prefix(frame['content'], secret, final=final)[:limit]
        if frame.get('call_id'):
            frame['call_id'] = safe_prefix(frame['call_id'], secret, final=final)
        if frame.get('tool'):
            frame['tool'] = redact(frame['tool'], secret)
        metadata = frame.get('call_id'), frame.get('tool'), frame.get('truncated', False)
        if not content and not previous:
            return
        if content == previous and metadata == previous_metadata and not (final and frame.get('call_id')):
            return
        if metadata != previous_metadata or not previous or final or len(content) - len(previous) >= 64 or time.monotonic() - emitted >= .15:
            frame = {**frame, 'content': content}
            await on_preview(frame)
            published[identity] = content, time.monotonic(), metadata

    async def publish(final=False):
        nonlocal last_text, last_emit
        # Private reasoning blocks and unselected parameter fields stay private.
        safe = safe_prefix(text, secret, final=final)
        if on_delta is not None and safe != last_text and (not last_text or final or len(safe) - len(last_text) >= 80 or time.monotonic() - last_emit > .15):
            await on_delta(safe)
            last_text, last_emit = safe, time.monotonic()
        await emit({'block': 'text', 'source': 'text', 'content': text,
                    'truncated': len(text) > TEXT_PREVIEW_LIMIT}, final)
        source_calls = calls if protocol == 'openai_chat_completions' else {
            index: {'id': block.get('id'), 'function': {'name': block.get('name')}}
            for index, block in blocks.items() if block['type'] == 'tool_use'}
        for index, call in source_calls.items():
            name = call['function'].get('name', '')
            parser = previews.get(index)
            if name not in PREVIEW_FIELDS or parser is None or parser.invalid:
                continue
            field, source, limit = PREVIEW_FIELDS[name]
            await emit({'block': 'call:' + str(index), 'source': source,
                'content': parser.values.get(field, ''), 'call_id': call.get('id'), 'tool': name,
                'truncated': parser.lengths.get(field, 0) > limit}, final)
    async def consume(data):
        nonlocal raw, text, terminated, terminal_reason
        if data == '[DONE]':
            terminated = True; return
        value = strict_json(data)
        if isinstance(value, dict) and (value.get('error') or value.get('type') == 'error'):
            # An explicit rejection permits context recovery; incomplete
            # streams and arbitrary malformed events still cannot authorize
            # replay. Do not expose echoed prompt or credential text.
            if len(data.encode('utf-8')) <= MAX_ERROR_BYTES:
                raise provider_error_fault(redact(value, secret))
            raise ProviderFault('provider_unavailable', '主控流错误超过诊断大小上限；未自动重试')
        if not isinstance(value, dict):
            raise ValueError('Invalid SSE event')
        if protocol == 'openai_chat_completions':
            if isinstance(value.get('model'), str):
                raw['model'] = value['model']
            if value.get('usage'):
                raw['usage'] = value['usage']
            choices = value.get('choices', [])
            if len(choices) > 1:
                raise ValueError('Multiple stream choices')
            if not choices:
                return
            item = choices[0]
            if item.get('index', 0) != 0:
                raise ValueError('Unknown choice index')
            if item.get('finish_reason') == 'length':
                terminal_reason = 'length'
            if item.get('finish_reason'):
                raw['choices'][0]['finish_reason'] = item['finish_reason']
            delta = item.get('delta') or {}
            if delta.get('content'):
                text += delta['content']
            for update in delta.get('tool_calls', []):
                index = update['index']
                if type(index) is not int or not 0 <= index < 20:
                    raise ValueError('Invalid tool index')
                call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if update.get('type') and update['type'] != 'function':
                    raise ValueError('Unknown call type')
                for field in ('id',):
                    if update.get(field): call[field] += update[field]
                for field in ('name', 'arguments'):
                    if (update.get('function') or {}).get(field): call['function'][field] += update['function'][field]
                fragment = (update.get('function') or {}).get('arguments')
                if fragment:
                    previews.setdefault(index, ArgumentPreview(len(secret))).feed(fragment)
            for field in ('reasoning_content',):
                if delta.get(field):
                    raw['choices'][0]['message'][field] = raw['choices'][0]['message'].get(field, '') + delta[field]
            if delta.get('refusal'):
                raw['choices'][0]['message']['refusal'] = delta['refusal']
        else:
            kind = value.get('type')
            if kind == 'message_start':
                if raw is not None:
                    raise ValueError('Repeated message start')
                raw = value['message']
            elif kind == 'content_block_start':
                index = value['index']
                if type(index) is not int or not 0 <= index < 40 or index in blocks:
                    raise ValueError('Invalid block index')
                blocks[index] = value['content_block']
                if blocks[index]['type'] == 'text':
                    text = '\n'.join(block['text'] for _, block in sorted(blocks.items()) if block['type'] == 'text')
                elif blocks[index]['type'] == 'tool_use' and blocks[index].get('input'):
                    from .store import canonical
                    previews.setdefault(index, ArgumentPreview(len(secret))).feed(canonical(blocks[index]['input']))
            elif kind == 'content_block_delta':
                index, delta = value['index'], value['delta']
                if index in closed_blocks:
                    raise ValueError('Delta after block end')
                block = blocks[index]
                subtype = delta['type']
                if subtype == 'text_delta' and block['type'] == 'text':
                    block['text'] += delta['text']
                    text = '\n'.join(item['text'] for _, item in sorted(blocks.items()) if item['type'] == 'text')
                elif subtype == 'input_json_delta' and block['type'] == 'tool_use':
                    if block.get('input'):
                        raise ValueError('Argument delta after complete initial input')
                    argument_text[index] = argument_text.get(index, '') + delta['partial_json']
                    previews.setdefault(index, ArgumentPreview(len(secret))).feed(delta['partial_json'])
                elif subtype == 'thinking_delta' and block['type'] == 'thinking':
                    block['thinking'] += delta['thinking']
                elif subtype == 'signature_delta' and block['type'] == 'thinking':
                    block['signature'] = block.get('signature', '') + delta['signature']
                else:
                    raise ValueError('Invalid block delta')
            elif kind == 'message_delta':
                if (value.get('delta') or {}).get('stop_reason') == 'max_tokens':
                    terminal_reason = 'max_tokens'
                if raw is None or set(blocks) != closed_blocks:
                    raise ValueError('Missing message start')
                raw.update(value['delta']); raw['usage'].update(value.get('usage') or {})
            elif kind == 'content_block_stop':
                index = value['index']
                if index not in blocks or index in closed_blocks:
                    raise ValueError('Invalid block end')
                closed_blocks.add(index)
            elif kind == 'message_stop':
                if set(blocks) != closed_blocks:
                    raise ValueError('Incomplete content blocks')
                terminated = True
            elif kind != 'ping':
                raise ValueError('Unknown SSE event')
        if terminal_reason is not None:
            raise truncation(terminal_reason)
        await publish()
    try:
        async for line in response.aiter_lines():
            total += len(line.encode('utf-8'))
            if total > MAX_RESPONSE_BYTES:
                raise ValueError('Stream too large')
            if not line:
                if data_lines:
                    await consume('\n'.join(data_lines)); data_lines.clear()
            elif line.startswith('data:'):
                if terminated:
                    raise ValueError('Data after stream end')
                data_lines.append(line[5:].lstrip(' '))
        if data_lines:
            await consume('\n'.join(data_lines))
        if terminal_reason is not None:
            raise truncation(terminal_reason)
        if not terminated or raw is None:
            raise truncation('eof')
        if protocol == 'openai_chat_completions':
            if sorted(calls) != list(range(len(calls))):
                raise ValueError('Noncontiguous tool indexes')
            raw['choices'][0]['message'].update(content=text, tool_calls=[calls[i] for i in sorted(calls)])
        else:
            if sorted(blocks) != list(range(len(blocks))):
                raise ValueError('Noncontiguous block indexes')
            for index, arguments in argument_text.items():
                blocks[index]['input'] = strict_json(arguments)
            raw['content'] = [blocks[i] for i in sorted(blocks)]
        await publish(final=True)
        return redact(raw, secret)
    except ProviderFault:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        if terminal_reason is not None:
            raise truncation(terminal_reason) from None
        raise ProviderFault('invalid_response', '模型流协议或工具参数不完整，未执行工具') from None
    except httpx.HTTPError:
        if terminal_reason is not None:
            raise truncation(terminal_reason) from None
        raise
