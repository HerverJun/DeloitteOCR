import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from ocr_workbench.store import Store
from ocr_workbench.external_review import CredentialVault
from ocr_workbench.agent.connection import ControllerConnection
from ocr_workbench.agent.providers import ProviderFault


def echo_probe(req):
    payload = json.loads(req.content)
    protocol = "anthropic" if req.url.path.endswith("messages") else "openai"
    messages = payload["messages"]
    last = messages[-1]
    if protocol == "openai":
        if last["role"] == "tool":
            text = json.loads(last["content"])["receipt"]
            message = {"role": "assistant", "content": text}
            stop = "stop"
        else:
            message = {"role": "assistant", "tool_calls": [{"id": "echo", "type": "function", "function": {"name": "probe_echo", "arguments": json.dumps({"value": last["content"].split()[-1]})}}]}
            stop = "tool_calls"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": stop}]})
    if isinstance(last["content"], list):
        blocks = [{"type": "text", "text": json.loads(last["content"][0]["content"])["receipt"]}]
        stop = "end_turn"
    else:
        blocks = [{"type": "tool_use", "id": "echo", "name": "probe_echo", "input": {"value": last["content"].split()[-1]}}]
        stop = "tool_use"
    return httpx.Response(200, json={"type": "message", "role": "assistant", "content": blocks, "stop_reason": stop})


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "workspace")
        self.vault = CredentialVault(Path(self.temp.name) / "controller-keys")
        self.connection = ControllerConnection(self.store, self.vault)
        self.body = {"protocol": "openai_chat_completions", "base_url": "https://synthetic.invalid/v1", "model": "synthetic", "api_key": "synthetic-controller-key"}

    async def test_two_rounds_dpapi_private_and_old_config_preserved(self):
        for protocol in ("openai_chat_completions", "anthropic_messages"):
            view = await self.connection.save({**self.body, "protocol": protocol}, transport=httpx.MockTransport(echo_probe))
            self.assertTrue(view["available"])
            self.assertNotIn("credential_ref", view)
            self.assertNotIn(self.body["api_key"], json.dumps(view))
            self.assertEqual(view["capabilities"]["rounds"], 2)
            self.assertFalse(view["capabilities"]["business_data_sent"])
        previous = self.connection.view()
        with self.assertRaises(ProviderFault):
            await self.connection.save({**self.body, "model": "bad"}, transport=httpx.MockTransport(lambda r: httpx.Response(401, text="do not expose")))
        self.assertEqual(self.connection.view(), previous)
        self.assertFalse(self.store.rows("SELECT * FROM settings WHERE key='external_visual_review'"))
        raw = json.dumps(self.store.rows("SELECT * FROM settings"))
        self.assertNotIn(self.body["api_key"], raw)
        for file in self.vault.root.glob("*.json"):
            self.assertNotIn(self.body["api_key"], file.read_text("utf-8"))

    async def test_read_and_clear_have_no_network_and_invalidate_snapshot(self):
        view = await self.connection.save(self.body, transport=httpx.MockTransport(echo_probe))
        config, key = self.connection.resolve(view["revision"])
        self.assertEqual(key, self.body["api_key"])
        self.assertNotIn("credential_ref", config)
        with patch("httpx.AsyncClient", side_effect=AssertionError("network")):
            self.assertTrue(self.connection.view()["available"])
            self.connection.clear()
            with self.assertRaises(ProviderFault):
                self.connection.resolve(view["revision"])
        with self.assertRaises(ValueError):
            self.connection.draft({**self.body, "api_key": ""})

    async def test_cannot_reuse_key_at_new_endpoint_and_missing_dpapi(self):
        await self.connection.save(self.body, transport=httpx.MockTransport(echo_probe))
        with self.assertRaises(ValueError):
            self.connection.draft({**self.body, "base_url": "https://other.invalid", "api_key": ""})
        with patch.object(self.vault, "read", side_effect=ValueError("wrong user")):
            self.assertFalse(self.connection.view()["available"])

    async def test_text_only_model_and_wrong_receipt_fail_probe(self):
        raw = {"choices": [{"message": {"role": "assistant", "content": "I used the tool"}, "finish_reason": "stop"}]}
        with self.assertRaises(ProviderFault):
            await self.connection.save(self.body, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw)))
        self.assertFalse(self.connection.view()["configured"])
