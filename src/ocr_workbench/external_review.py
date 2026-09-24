"""One user-configured visual reviewer; credentials never enter task evidence."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit, urlunsplit

from ocr_workbench.atomic_files import write_json
from ocr_workbench.multimodal_runtime import (
    PROMPT, PROMPT_VERSION, ReviewCancelled, ReviewPipeline, ReviewProtocolError,
    validate_reply,
)

SETTING = 'external_visual_review'
LIMITS = {'max_targets': 256, 'max_target_chars': 4000, 'max_total_chars': 40000,
          'batch_size': 1, 'max_batch_chars': 6000, 'image_max_pixels': 1048576,
          'image_max_side': 1600, 'context_max_pixels': 262144,
          'max_output_tokens': 4096, 'request_timeout_seconds': 180}
MAX_REPLY = 1024 * 1024


def normalize_url(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError('请输入有效的 API URL')
    value = value.strip()
    if any(ord(char) < 33 for char in value) or '\\' in value:
        raise ValueError('API URL 不能含空白或反斜杠')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError('API URL 须为 HTTP/HTTPS 地址，不能包含账号、查询参数或锚点') from None
    path = parsed.path.rstrip('/')
    for suffix in ('/chat/completions', '/messages', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path or '/v1', '', ''))


class CredentialVault:
    """Current-user Windows DPAPI, stored outside projects and their exports."""
    def __init__(self, root=None):
        self.root = Path(root) if root else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'OfflineOCR' / 'Credentials'

    @staticmethod
    def _crypt(data, decrypt=False):
        if os.name != 'nt':
            raise ValueError('外部 API 凭据保存需要 Windows DPAPI')
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError('无法读取本机 API Key，请重新填写' if decrypt else '无法加密保存 API Key')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('API 凭据引用无效，请重新配置')
        return self.root / (identifier + '.json')

    def save(self, key):
        encrypted = self._crypt(key.encode('utf-8'))
        self.root.mkdir(parents=True, exist_ok=True)
        identifier = secrets.token_hex(16)
        write_json(self._path(identifier), {'dpapi': base64.b64encode(encrypted).decode()}, durable=True)
        return identifier

    def read(self, identifier):
        try:
            data = json.loads(self._path(identifier).read_text('utf-8'))
            return self._crypt(base64.b64decode(data['dpapi'], validate=True), True).decode('utf-8')
        except (OSError, KeyError, UnicodeError, ValueError):
            raise ValueError('本机 API Key 不可用，请重新填写；拷贝项目不会迁移 Key') from None

    def delete(self, identifier):
        self._path(identifier).unlink(missing_ok=True)


class ProviderError(ValueError):
    def __init__(self, status, unsupported_token_parameter=False):
        hints = {401: 'Key 无效或已过期', 403: '账号没有访问权限', 404: '地址或模型不存在',
                 405: '服务不支持此接口', 413: '图片或请求超过服务限制',
                 429: '达到服务限流或额度限制，请稍后手动重试'}
        super().__init__(f'外部 API 返回 HTTP {status}：{hints.get(status, "请求被拒绝或服务暂不可用，请检查地址、模型与服务状态")}')
        self.status = status
        self.unsupported_token_parameter = unsupported_token_parameter


async def _http(config, key, method, suffix, payload=None):
    try:
        import httpx
    except ImportError:
        raise ValueError('运行环境缺少外部 API 依赖，请安装新版服务运行时') from None
    headers = {'Content-Type': 'application/json'}
    if config['protocol'] == 'openai':
        headers['Authorization'] = 'Bearer ' + key
    else:
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=180) as client:
            async with client.stream(method, config['base_url'] + suffix, headers=headers, json=payload) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_REPLY:
                        raise ReviewProtocolError('外部 API 响应超过 1 MiB 上限')
                if response.status_code >= 300:
                    # Inspect only to negotiate an explicitly unsupported parameter.
                    # Never propagate provider error bodies, headers or credentials.
                    try:
                        detail = json.loads(body).get('error', {})
                        message = str(detail.get('message', '')).lower() if isinstance(detail, dict) else ''
                        unsupported = (response.status_code == 400 and 'max_completion_tokens' in message and
                                       any(word in message for word in ('unsupported', 'unknown', 'unrecognized', 'not supported', 'not permitted')))
                    except (ValueError, AttributeError):
                        unsupported = False
                    raise ProviderError(response.status_code, unsupported)
                try:
                    raw = json.loads(body)
                    # A misconfigured upstream may echo credentials in its response.
                    def redact(value):
                        if isinstance(value, str):
                            return value.replace(key, '[redacted]') if len(key) >= 8 or value == key else value
                        if isinstance(value, dict):
                            return {k: redact(v) for k, v in value.items()}
                        if isinstance(value, list):
                            return [redact(v) for v in value]
                        return value
                    return redact(raw)
                except (ValueError, UnicodeError):
                    raise ReviewProtocolError('外部 API 未返回有效 JSON') from None
    except httpx.HTTPError:
        raise ValueError('无法连接外部 API，请检查网络、URL 和 HTTPS 证书') from None


def run_cancelable(coroutine, timeout, cancel_event=None):
    async def run():
        async def watch():
            while not cancel_event or not cancel_event.is_set():
                await asyncio.sleep(.05)
        task = asyncio.create_task(coroutine)
        cancel = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({task, cancel}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if cancel in done or (cancel_event and cancel_event.is_set()):
                raise ReviewCancelled('已取消外部审校')
            if task not in done:
                raise ValueError('外部 API 响应超时，请手动重试')
            return await task
        finally:
            task.cancel()
            cancel.cancel()
            await asyncio.gather(task, cancel, return_exceptions=True)
    return asyncio.run(run())


def payload_for(config, content, max_tokens=4096, *, system_prompt=None):
    # The shared validator owns the contract; gateways need not implement JSON Schema.
    system = system_prompt or PROMPT + '\nJSON format: {"items":[{"target_id":"...","reading":"literal text or null","reason":"中文依据"}],"summary":"中文摘要"}. reading may be JSON null.'
    if config['protocol'] == 'openai':
        return {'model': config['model'], 'messages': [{'role': 'system', 'content': system},
                {'role': 'user', 'content': content}], 'stream': False,
                config.get('token_parameter', 'max_completion_tokens'): max_tokens}
    converted = []
    for item in content:
        if item['type'] == 'text':
            converted.append(item)
        else:
            converted.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                              'data': item['image_url']['url'].split(',', 1)[1]}})
    return {'model': config['model'], 'system': system, 'messages': [{'role': 'user', 'content': converted}],
            'max_tokens': max_tokens, 'stream': False}


def normalized_reply(config, raw):
    try:
        if config['protocol'] == 'openai':
            choices = raw['choices']
            if len(choices) != 1 or choices[0]['finish_reason'] != 'stop':
                raise ReviewProtocolError('审校输出被截断或未正常结束')
            message = choices[0]['message']
            if message.get('refusal') or message.get('tool_calls') or message.get('function_call'):
                raise ReviewProtocolError('外部模型拒绝审校或返回工具调用')
            text = message['content']
        else:
            if raw['stop_reason'] != 'end_turn':
                raise ReviewProtocolError('审校输出被截断、拒绝或请求工具调用')
            blocks = raw['content']
            if not isinstance(blocks, list) or any(b.get('type') not in {'text', 'thinking', 'redacted_thinking'} for b in blocks):
                raise ReviewProtocolError('外部模型返回了不支持的内容')
            text = ''.join(b['text'] for b in blocks if b['type'] == 'text')
        if not isinstance(text, str) or not text.strip():
            raise ReviewProtocolError('外部模型未返回审校文字')
        text = text.strip()
        match = re.fullmatch(r'```(?:json)?\s*\n([\s\S]*?)\n```', text)
        if match:
            text = match[1]
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}
    except (KeyError, TypeError, IndexError, AttributeError):
        raise ReviewProtocolError('外部 API 返回格式与所选协议不符') from None


async def call_model(config, key, content, max_tokens=4096):
    raw = await _http(config, key, 'POST', '/chat/completions' if config['protocol'] == 'openai' else '/messages',
                      payload_for(config, content, max_tokens))
    return normalized_reply(config, raw)


def make_probe():
    from PIL import Image, ImageDraw, ImageFont
    expected = 'OCR-' + secrets.token_hex(3).upper()
    image = Image.new('RGB', (640, 120), 'white')
    ImageDraw.Draw(image).text((24, 28), expected, fill='black', font=ImageFont.load_default(size=44))
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue(), expected


def test_connection(config, key):
    image, expected = make_probe()
    targets = [{'id': 'probe', 'before': '?'}]
    content = [{'type': 'text', 'text': 'PAGE CONTEXT; target_id=probe: read the single printed line.'},
               {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}},
               {'type': 'text', 'text': 'OCR_TARGET_DATA_JSON\n[{"target_id":"probe","before":"?"}]'}]
    async def test():
        try:
            reply = await call_model(config, key, content, 1024)
        except ProviderError as error:
            if config['protocol'] != 'openai' or not error.unsupported_token_parameter:
                raise
            config['token_parameter'] = 'max_tokens'
            reply = await call_model(config, key, content, 1024)
        checked = validate_reply(reply, targets)
        if checked['readings'][0]['reading'] != expected:
            raise ValueError('连接可用，但未正确读出测试图片；请选择支持视觉的模型后重试')
    run_cancelable(test(), 180)


class ExternalConnection:
    def __init__(self, store, vault=None):
        self.store = store
        self.vault = vault or CredentialVault()
        self.guard = threading.RLock()

    def _read(self):
        rows = self.store.rows('SELECT value FROM settings WHERE key=?', (SETTING,))
        return json.loads(rows[0]['value']) if rows else None

    def view(self):
        with self.guard:
            config = self._read()
            if not config:
                return {'configured': False, 'has_key': False}
            try:
                self.vault.read(config['credential_id'])
                ready, reason = True, ''
            except ValueError as error:
                ready, reason = False, str(error)
            return {k: config[k] for k in ('protocol', 'base_url', 'model', 'revision', 'tested_at')} | {
                'configured': True, 'has_key': ready, 'available': ready, 'reason': reason,
                'model_id': 'external:' + config['revision']}

    def draft(self, body, need_model=True):
        if not isinstance(body, dict) or body.get('protocol') not in {'openai', 'anthropic'}:
            raise ValueError('请选择 OpenAI 或 Anthropic 协议')
        config = {'protocol': body['protocol'], 'base_url': normalize_url(body.get('base_url'))}
        model = body.get('model', '')
        if not isinstance(model, str) or len(model) > 256 or any(ord(c) < 32 for c in model):
            raise ValueError('模型 ID 无效')
        if need_model and not model.strip():
            raise ValueError('请选择或填写模型 ID')
        config.update(model=model.strip(), token_parameter='max_completion_tokens')
        key = body.get('api_key', '')
        if not isinstance(key, str) or len(key) > 8192 or any(not 32 <= ord(c) <= 126 for c in key):
            raise ValueError('API Key 格式无效')
        with self.guard:
            old = self._read()
            revision = old['revision'] if old else None
            if not key.strip():
                if not old or any(config[k] != old[k] for k in ('protocol', 'base_url')):
                    raise ValueError('首次配置或修改地址、协议后，请重新填写 API Key')
                key = self.vault.read(old['credential_id'])
        return config, key.strip(), revision

    def models(self, body):
        config, key, _ = self.draft(body, False)
        async def fetch():
            from urllib.parse import urlencode
            models, seen, cursor = [], set(), None
            for _ in range(100):
                suffix = '/models'
                if config['protocol'] == 'anthropic':
                    suffix += '?' + urlencode({'limit': 100, **({'after_id': cursor} if cursor else {})})
                raw = await _http(config, key, 'GET', suffix)
                if not isinstance(raw, dict) or not isinstance(raw.get('data'), list):
                    raise ValueError('模型列表格式不兼容，可直接填写模型 ID')
                for item in raw['data']:
                    ident = item.get('id') if isinstance(item, dict) else None
                    if isinstance(ident, str) and ident.strip() and len(ident) <= 256:
                        models.append({'id': ident, 'label': str(item.get('display_name') or ident)[:256]})
                if config['protocol'] != 'anthropic' or not raw.get('has_more'):
                    return {'models': list({m['id']: m for m in models}.values())}
                cursor = raw.get('last_id')
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError('模型列表分页异常，可直接填写模型 ID')
                seen.add(cursor)
            raise ValueError('模型列表页数过多，可直接填写模型 ID')
        return run_cancelable(fetch(), 15)

    def save(self, body):
        from ocr_workbench.store import Conflict, now
        config, key, previous_revision = self.draft(body)
        test_connection(config, key)
        with self.guard:
            old = self._read()
            if (old['revision'] if old else None) != previous_revision:
                raise Conflict('连接已在另一个窗口更新，请重新打开配置')
            identifier = self.vault.save(key)
            config.update(credential_id=identifier, revision=secrets.token_hex(16), tested_at=now())
            try:
                with self.store.transaction() as db:
                    db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (SETTING, json.dumps(config)))
            except BaseException:
                self.vault.delete(identifier)
                raise
            if old:
                try:
                    self.vault.delete(old['credential_id'])
                except OSError:
                    # The new configuration is already durable. An inaccessible
                    # obsolete ciphertext must not report the save as failed.
                    pass
            return self.view()

    def clear(self):
        with self.guard:
            old = self._read()
            if old:
                self.vault.delete(old['credential_id'])
                with self.store.transaction() as db:
                    db.execute('DELETE FROM settings WHERE key=?', (SETTING,))
        return self.view()

    def resolve(self, model_id):
        from ocr_workbench.store import Conflict
        with self.guard:
            stored = self._read()
            if not stored or model_id != 'external:' + stored['revision']:
                raise Conflict('外部连接已变化，请刷新模型并重新提交审校')
            key = self.vault.read(stored['credential_id'])
            config = {k: stored[k] for k in ('protocol', 'base_url', 'model', 'revision', 'token_parameter')}
            config.update(backend='external', profile_id=model_id, prompt_version=PROMPT_VERSION,
                          limits=deepcopy(LIMITS))
            config['config_sha256'] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
            return config, key

    def session(self, bundle, config, output, cancel_event):
        current, key = self.resolve(config['profile_id'])
        if current != config:
            raise ValueError('外部审校配置或提示词已变化，请重新提交')
        def check_connection():
            with self.guard:
                stored = self._read()
                if not stored or stored['revision'] != config['revision']:
                    raise ValueError('外部连接已变化，剩余批次未发送；请重新提交')
        return ExternalReviewSession(bundle, config, output, cancel_event, key=key, check_connection=check_connection)


class ExternalReviewSession(ReviewPipeline):
    def review(self, image_path, snapshot, progress_callback=None):
        if snapshot.get('review_kind') == 'structure':
            from ocr_workbench.structure_arbitration import review, LIMITS
            self.config = deepcopy(self.config)
            self.config['limits']['request_timeout_seconds'] = min(
                self.config['limits']['request_timeout_seconds'], LIMITS['timeout_seconds'])
            return review(self, image_path, snapshot, progress_callback)
        return super().review(image_path, snapshot, progress_callback)

    def __init__(self, bundle, config, output, cancel_event=None, *, key, check_connection=None):
        self.config, self.output, self.key = config, Path(output), key
        self.cancel_event = cancel_event or threading.Event()
        self.loaded, self.progress_callback = 0, None
        self.check_connection = check_connection

    def _cancel(self):
        if self.cancel_event.is_set():
            raise ReviewCancelled('已取消外部审校')

    def _payload(self, content, targets):
        return payload_for(self.config, content, self.config['limits']['max_output_tokens'])

    def _identity(self):
        return {k: self.config[k] for k in ('backend', 'protocol', 'base_url', 'model', 'revision', 'config_sha256', 'prompt_version')}

    def _request(self, payload, evidence_path):
        self._cancel()
        if self.check_connection:
            self.check_connection()
        async def request():
            raw = await _http(self.config, self.key, 'POST',
                              '/chat/completions' if self.config['protocol'] == 'openai' else '/messages', payload)
            return normalized_reply(self.config, raw)
        result = run_cancelable(request(), self.config['limits']['request_timeout_seconds'], self.cancel_event)
        self._cancel()
        write_json(evidence_path, result)
        return result

    def __enter__(self):
        self._cancel()
        return self

    def __exit__(self, *args):
        self.key = ''
