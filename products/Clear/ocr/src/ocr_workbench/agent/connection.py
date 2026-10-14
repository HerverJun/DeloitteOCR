"""Independent controller credentials and a two-round, data-free tool probe."""
from __future__ import annotations

import json
import secrets
import threading

from ocr_workbench.external_review import CredentialVault, normalize_url
from ocr_workbench.store import Conflict, now
from .providers import ProviderFault, payload_for, request

SETTING = "agent_controller_connection"
PROTOCOLS = {"openai_chat_completions", "anthropic_messages"}
PROBE_SCHEMA = {"probe_echo": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}


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
    return {"version": "native-tools-probe-v1", "native_tools": True, "tool_result_consumption": True,
            "rounds": 2, "business_data_sent": False, "usage": [first["usage"], second["usage"]]}


class ControllerConnection:
    def __init__(self, store, vault=None):
        self.store = store
        self.vault = vault or CredentialVault(CredentialVault().root / "AgentController")
        self.guard = threading.RLock()

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
            return {k: v for k, v in config.items() if k != "credential_ref"} | {"configured": True, "available": available, "reason": reason}

    def draft(self, body):
        if not isinstance(body, dict) or set(body) - {"protocol", "base_url", "model", "api_key", "token_parameter", "context_cap", "max_output_tokens"}:
            raise ValueError("连接配置字段无效")
        if body.get("protocol") not in PROTOCOLS:
            raise ValueError("请选择原生工具协议")
        model = body.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(ord(c) < 32 for c in model):
            raise ValueError("请填写有效的模型 ID")
        key = body.get("api_key", "")
        if not isinstance(key, str) or len(key) > 8192 or any(not 32 <= ord(c) <= 126 for c in key):
            raise ValueError("API Key 格式无效")
        config = {"protocol": body["protocol"], "base_url": normalize_url(body.get("base_url")), "model": model.strip(),
                  "token_parameter": body.get("token_parameter", "max_completion_tokens"),
                  "context_cap": body.get("context_cap", 16384), "max_output_tokens": body.get("max_output_tokens", 2048)}
        if config["token_parameter"] not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("不支持的输出 token 参数")
        if type(config["context_cap"]) is not int or not 4096 <= config["context_cap"] <= 200000:
            raise ValueError("上下文上限须为 4096–200000")
        if type(config["max_output_tokens"]) is not int or not 256 <= config["max_output_tokens"] <= min(8192, config["context_cap"] // 2):
            raise ValueError("输出 token 上限无效")
        with self.guard:
            old = self._read()
            if not key.strip():
                if old.get("deleted") or any(config[k] != old[k] for k in ("protocol", "base_url")):
                    raise ValueError("首次连接或更换地址、协议后请填写 API Key")
                key = self.vault.read(old["credential_ref"])
            return config, key.strip(), old["revision"]

    async def save(self, body, *, transport=None):
        config, key, revision = self.draft(body)
        # No SQLite transaction or threading lock across the two network calls.
        capabilities = await probe(config, key, transport=transport)
        with self.guard:
            old = self._read()
            if old["revision"] != revision:
                raise Conflict("主控连接已在另一窗口更新，请重新测试")
            reference = self.vault.save(key)
            saved = {**config, "revision": revision + 1, "credential_ref": reference, "tested_at": now(), "capabilities": capabilities}
            try:
                with self.store.transaction() as db:
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (SETTING, json.dumps(saved)))
            except BaseException:
                self.vault.delete(reference)
                raise
            if not old.get("deleted"):
                try:
                    self.vault.delete(old["credential_ref"])
                except OSError:
                    pass
            return self.view()

    def clear(self):
        with self.guard:
            old = self._read()
            with self.store.transaction() as db:
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (SETTING, json.dumps({"revision": old["revision"] + 1, "deleted": True})))
            if not old.get("deleted"):
                try:
                    self.vault.delete(old["credential_ref"])
                except OSError:
                    pass
            return self.view()

    def resolve(self, revision):
        with self.guard:
            current = self._read()
            if current.get("deleted") or current["revision"] != revision:
                raise ProviderFault("config_changed", "主控连接已更改或删除，请核对后开始新运行")
            key = self.vault.read(current["credential_ref"])
            return {k: v for k, v in current.items() if k != "credential_ref"}, key

    def assert_current(self, revision):
        with self.guard:
            current = self._read()
            if current.get("deleted") or current["revision"] != revision:
                raise ProviderFault("config_changed", "主控连接已变化，后续发送已停止")

    async def models(self, body):
        from urllib.parse import urlencode
        from ocr_workbench.external_review import _http
        draft = {**body, "model": body.get("model") or "model-list-probe"}
        config, key, _ = self.draft(draft)
        transport_config = {**config, "protocol": "openai" if config["protocol"] == "openai_chat_completions" else "anthropic"}
        results, seen, cursor = {}, set(), None
        for _ in range(100):
            suffix = "/models"
            if transport_config["protocol"] == "anthropic":
                suffix += "?" + urlencode({"limit": 100, **({"after_id": cursor} if cursor else {})})
            raw = await _http(transport_config, key, "GET", suffix)
            if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
                raise ValueError("模型列表不可用，可手填 ID 后运行工具探针")
            for item in raw["data"]:
                identifier = item.get("id") if isinstance(item, dict) else None
                if isinstance(identifier, str) and 0 < len(identifier) <= 256:
                    results[identifier] = {"id": identifier}
            if transport_config["protocol"] == "openai" or not raw.get("has_more"):
                return {"models": list(results.values())}
            cursor = raw.get("last_id")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("模型列表游标无效，可手填 ID 后测试")
            seen.add(cursor)
        raise ValueError("模型列表超过分页上限，可手填 ID 后测试")
