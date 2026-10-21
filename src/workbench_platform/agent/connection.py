"""Independent credentials and separate, data-free tool/SSE capability probes."""
from __future__ import annotations

import json
import secrets
import threading

from .credentials import CredentialVault, normalize_url
from packages.contracts.agent import ControllerDraft
from .store import Conflict, digest, now
from .providers import ProviderFault, payload_for, request
from .model_profiles import candidate, catalog_key, normalize_config

SETTING = "agent_controller_connection"
PROTOCOLS = {"openai_chat_completions", "anthropic_messages"}
PROBE_SCHEMA = {"probe_echo": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}
ANSWER_PROBE_SCHEMA = {'conversation_answer': {'type': 'object',
    'properties': {'content': {'type': 'string'}}, 'required': ['content'], 'additionalProperties': False}}


async def streaming_probe(config, key, *, transport=None):
    """One independent test, never a retry of a failed production POST.

    A valid native-tool connection can still be used when SSE is unsupported.
    Only random nonces are submitted, and no generated tool is dispatched.
    """
    if not config.get('streaming', True):
        disabled = {'mode': 'disabled', 'valid_response': False, 'preview_observed': False,
                    'preview_frames': 0, 'supported': False}
        return {'requested': False, 'tested': False, 'status': 'disabled', 'probe_rounds': 0,
                'mode': 'disabled', 'detected_at': now(), 'business_data_sent': False,
                'text': dict(disabled), 'answer': dict(disabled)}
    value = secrets.token_hex(48)
    instruction = ('First emit ordinary assistant text containing only the value below, then call '
                   'conversation_answer once with content set to that same value. A tool-only answer is also valid. Value: ') + value
    frames, modes, response, error_code = [], [], None, None
    async def preview(frame):
        frames.append(frame)
    async def observed(mode):
        modes.append(mode)
    try:
        payload = payload_for(config, [{'role': 'user', 'content': instruction}], schemas=ANSWER_PROBE_SCHEMA,
            system='Data-free connection streaming test. Follow the requested response format exactly; do not access files or perform any business operation.')
        payload['stream'] = True
        response = await request(config, key, payload, 'probe-stream', transport=transport,
            on_preview=preview, on_transport=observed)
    except ProviderFault as error:
        error_code = error.code
    except (ValueError, KeyError, TypeError):
        error_code = 'invalid_response'
    results, mode = {}, modes[-1]['mode'] if modes else 'unknown'
    for source in ('text', 'answer'):
        selected = [frame for frame in frames if frame['source'] == source]
        valid = bool(response and response['content'].strip() == value) if source == 'text' else bool(response and
            len(response['calls']) == 1 and response['calls'][0]['tool'] == 'conversation_answer' and
            response['calls'][0]['arguments'] == {'content': value})
        results[source] = {'mode': mode, 'valid_response': valid, 'preview_observed': bool(selected),
                           'preview_frames': len(selected), 'supported': mode == 'sse' and valid and bool(selected)}
        if error_code:
            results[source]['error_code'] = error_code
    status = 'supported' if results['answer']['supported'] else 'partial' if results['text']['supported'] else (
        'fallback' if mode == 'json' and response is not None else 'unsupported')
    return {'requested': True, 'tested': True, 'status': status, 'probe_rounds': 1,
            'mode': mode, 'detected_at': now(), 'business_data_sent': False, **results}


async def probe(config, key, *, transport=None):
    challenge, receipt = secrets.token_hex(12), secrets.token_hex(16)
    system = "Connection capability test only. Call probe_echo with the exact requested value. After its result, reply with only its receipt value."
    messages = [{"role": "user", "content": "Call probe_echo with value " + challenge}]
    first = await request(config, key, payload_for(config, messages, schemas=PROBE_SCHEMA, system=system), "probe-first", transport=transport, probe=True)
    if len(first["calls"]) != 1 or first["calls"][0]["arguments"].get("value") != challenge:
        raise ProviderFault("unsupported_capability", "主控未通过原生工具调用探针")
    messages.extend([first, {"role": "tool", "call_id": first["calls"][0]["call_id"], "result": {"status": "success", "receipt": receipt}}])
    second = await request(config, key, payload_for(config, messages, schemas=PROBE_SCHEMA, system=system), "probe-second", transport=transport, probe=True)
    if second["calls"] or second["content"].strip() != receipt:
        raise ProviderFault("unsupported_capability", "主控未通过工具结果消费探针")
    streaming = await streaming_probe(config, key, transport=transport)
    return {"version": "native-tools-probe-v2", "native_tools": True, "tool_result_consumption": True,
            "rounds": 2, "business_data_sent": False, "usage": [first["usage"], second["usage"]],
            'total_rounds': 2 + streaming['probe_rounds'], 'streaming': streaming}


class ControllerConnection:
    def __init__(self, store, vault=None, *, production_preflight=None):
        self.store = store
        self.vault = vault or CredentialVault(store.root / "credentials")
        self.guard = threading.RLock()
        self.production_preflight = production_preflight

    def preflight(self, config):
        if self.production_preflight:
            return {**self.production_preflight(config), 'business_data_sent': False}
        # Standalone credential/protocol clients still verify real core
        # schemas locally. Runtime supplies its full dynamic environment and
        # registered Skill directory through the callback above.
        from .context import core_tools, prepare_request
        from .registry import ToolRegistry
        from .tools import context, audit
        registry = ToolRegistry()
        context.register(registry)
        audit.register(registry)
        _, metrics, selected = prepare_request(config,
            [{'role': 'user', 'content': '你好，请介绍平台的能力。'}], registry, core_tools(registry))
        return {'version': 'production-context-preflight-v1', 'passed': True,
                'business_data_sent': False, 'context_metrics': metrics,
                'tools': selected, 'scope': 'built-in-core-local'}

    def _read(self):
        rows = self.store.rows("SELECT value FROM settings WHERE key=?", (SETTING,))
        return json.loads(rows[0]["value"]) if rows else {"revision": 0, "deleted": True}

    def view(self):
        with self.guard:
            config = self._read()
            if config.get("deleted"):
                return {"configured": False, "available": False, "revision": config["revision"]}
            try:
                self.vault.read(config["credential_ref"])
                available, reason = True, ""
            except ValueError:
                available, reason = False, "此 Windows 用户无法读取主控凭据，请重新配置"
            return normalize_config({k: v for k, v in config.items() if k != "credential_ref"}) | {"configured": True, "available": available, "reason": reason}

    def draft(self, body):
        validated = ControllerDraft.model_validate(body)
        if not isinstance(body, dict):
            raise ValueError("连接配置字段无效")
        if body.get("protocol") not in PROTOCOLS:
            raise ValueError("请选择原生工具协议")
        model = body.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(ord(c) < 32 for c in model):
            raise ValueError("请填写有效的模型 ID")
        key = body.get("api_key", "")
        if not isinstance(key, str) or len(key) > 8192 or any(not 32 <= ord(c) <= 126 for c in key):
            raise ValueError("API Key 格式无效")
        config = {k: v for k, v in validated.model_dump().items() if k != 'api_key'}
        config.update(base_url=normalize_url(body.get('base_url')), model=model.strip())
        if config["token_parameter"] not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("不支持的输出 token 参数")
        with self.guard:
            old = self._read()
            if not key.strip():
                if old.get("deleted") or any(config[k] != old[k] for k in ("protocol", "base_url")):
                    raise ValueError("首次连接或更换地址、协议后请填写 API Key（本地匿名端点可填 local）")
                key = self.vault.read(old["credential_ref"])
            key = key.strip()
            cached = self.store.rows('SELECT value FROM settings WHERE key=?', (catalog_key(config),))
            if cached:
                catalog = json.loads(cached[0]['value'])
                if (isinstance(catalog, dict)
                        and catalog.get('credential_fingerprint') == digest(['model-catalog', key])):
                    config['endpoint_model_metadata'] = next((item for item in catalog.get('models', [])
                                                              if item['id'] == config['model']), {})
            config = normalize_config(config)
            profile = config['model_profile']
            if profile['available_input_tokens'] <= 0 or (profile['model_max_output_tokens'] is not None and
                                                         config['max_output_tokens'] > profile['model_max_output_tokens']):
                raise ValueError("输出 token 上限无效")
            return config, key.strip(), old["revision"]

    async def save(self, body, *, transport=None):
        config, key, revision = self.draft(body)
        local_check = self.preflight(config)
        # No SQLite transaction or threading lock across the bounded capability calls.
        capabilities = await probe(config, key, transport=transport)
        capabilities['production_preflight'] = local_check
        with self.guard:
            old = self._read()
            if old["revision"] != revision:
                raise Conflict("主控连接已在另一窗口更新，请重新测试")
            reference = self.vault.save(key)
            saved = normalize_config({**config, "revision": revision + 1, "credential_ref": reference, "tested_at": now(), "capabilities": capabilities})
            try:
                with self.store.transaction() as db:
                    if not old.get('deleted'):
                        db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',
                                   ('agent-controller-revision:' + str(old['revision']), json.dumps(old)))
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (SETTING, json.dumps(saved)))
            except BaseException:
                self.vault.delete(reference)
                raise
            # A running task owns its frozen connection revision. Keep its DPAPI
            # reference; changing the default must not revoke an in-flight run.
            return self.view()

    def clear(self):
        with self.guard:
            old = self._read()
            # Revoke the archived revision identities transactionally. Merely
            # deleting their files lets them become addressable after a later
            # save (and file deletion itself can fail).
            archived = self.store.rows("SELECT value FROM settings WHERE key LIKE 'agent-controller-revision:%'")
            with self.store.transaction() as db:
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (SETTING, json.dumps({"revision": old["revision"] + 1, "deleted": True})))
                db.execute("DELETE FROM settings WHERE key LIKE 'agent-controller-revision:%'")
            if not old.get("deleted"):
                try:
                    self.vault.delete(old["credential_ref"])
                except OSError:
                    pass
            for row in archived:
                try:
                    self.vault.delete(json.loads(row['value'])['credential_ref'])
                except (OSError, ValueError, KeyError):
                    pass
            return self.view()

    def resolve(self, revision):
        with self.guard:
            current = self._read()
            if current.get('deleted'):
                raise ProviderFault('config_changed', '主控连接已删除，后续发送已停止')
            if current['revision'] != revision:
                rows = self.store.rows('SELECT value FROM settings WHERE key=?', ('agent-controller-revision:' + str(revision),))
                if not rows:
                    raise ProviderFault('config_changed', '原连接版本不可读取，请核对后开始新运行')
                current = json.loads(rows[0]['value'])
            try:
                key = self.vault.read(current['credential_ref'])
            except (ValueError, KeyError):
                raise ProviderFault('config_changed', '原连接凭据不可读取，请重新配置后开始新运行') from None
            return normalize_config({k: v for k, v in current.items() if k != 'credential_ref'}), key

    def assert_current(self, revision):
        with self.guard:
            current = self._read()
            if current.get("deleted") or (current["revision"] != revision and not self.store.rows(
                    'SELECT key FROM settings WHERE key=?', ('agent-controller-revision:' + str(revision),))):
                raise ProviderFault("config_changed", "主控连接已变化，后续发送已停止")

    async def models(self, body, *, transport=None):
        """Explicit, bounded model-list request; never performed at startup."""
        import httpx
        from .providers import strict_json, redact, MAX_RESPONSE_BYTES
        if not isinstance(body, dict):
            raise ValueError('Invalid model-list parameters')
        # Discovery must remain possible while correcting an invalid model
        # budget. Authenticate/normalize only connection fields; it neither
        # saves the draft nor tests a model call.
        params = {key: body[key] for key in ('protocol', 'base_url', 'api_key') if key in body}
        config, secret, _ = self.draft({**params, 'model': 'model-list-probe', 'streaming': False})
        headers = {'authorization': 'Bearer ' + secret} if config['protocol'] == 'openai_chat_completions' else {
            'x-api-key': secret, 'anthropic-version': '2023-06-01'}
        items, cursor, seen_cursors, received = {}, None, set(), 0
        try:
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=30) as client:
                for _ in range(10):
                    query = {'limit': 100, **({'after_id': cursor} if cursor else {})} if config['protocol'] == 'anthropic_messages' else None
                    async with client.stream('GET', config['base_url'] + '/models', headers=headers, params=query) as response:
                        if response.status_code != 200:
                            raise ProviderFault('models_unavailable', f'模型列表返回 HTTP {response.status_code}；可手填模型ID并运行探针')
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise ValueError('Model response exceeds limit')
                            content.extend(chunk)
                    raw = redact(strict_json(content), secret)
                    if not isinstance(raw, dict):
                        raise ValueError('Invalid model list')
                    models = []
                    if isinstance(raw.get('data'), list):
                        models.extend(raw['data'])
                    if isinstance(raw.get('models'), dict):
                        models.extend({**value, 'id': name} for name, value in raw['models'].items() if isinstance(value, dict))
                    if not isinstance(raw.get('data'), list) and not isinstance(raw.get('models'), dict):
                        raise ValueError('Invalid model list')
                    received += len(models)
                    if received > 1000:
                        raise ValueError('Too many models')
                    for model in models:
                        parsed = candidate(model)
                        if parsed:
                            # data[] and models{} may describe the same id.
                            # Enrich missing fields without guessing aliases.
                            existing = items.setdefault(parsed['id'], parsed)
                            for field in ('context_window', 'max_output_tokens', 'max_input_tokens'):
                                if existing[field] is None:
                                    existing[field] = parsed[field]
                    if not raw.get('has_more') or config['protocol'] == 'openai_chat_completions':
                        values = list(items.values())
                        with self.store.transaction() as db:
                            db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                                       (catalog_key(config), json.dumps({'models': values, 'fetched_at': now(),
                                           'credential_fingerprint': digest(['model-catalog', secret])})))
                        return {'models': values, 'protocol_test_required': True}
                    next_cursor = raw.get('last_id')
                    if (not isinstance(next_cursor, str) or not 0 < len(next_cursor) <= 256
                            or next_cursor in seen_cursors):
                        raise ValueError('Invalid model cursor')
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except httpx.HTTPError:
            raise ProviderFault('models_unavailable', '模型列表连接失败；可手填模型ID并运行探针') from None
        except (ValueError, UnicodeError) as error:
            if isinstance(error, ProviderFault):
                raise
            raise ProviderFault('models_unavailable', '模型列表格式无效或超过大小上限；可手填模型ID并运行探针') from None
        raise ProviderFault('models_unavailable', '模型列表超过分页上限；可手填模型ID并运行探针')

