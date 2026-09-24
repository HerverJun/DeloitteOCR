import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from ocr_workbench.agent.providers import ProviderFault, normalize_response, payload_for, request, logged_request
from ocr_workbench.agent.store import AgentStore
from ocr_workbench.store import Store, Conflict


def reply(protocol, calls=True):
    if protocol == "openai_chat_completions":
        message = {"role": "assistant", "content": "查看上下文" if calls else "总额 42"}
        if calls:
            message["tool_calls"] = [{"id": "call-1", "type": "function", "function": {"name": "get_workspace_context", "arguments": "{}"}}]
            message["reasoning_content"] = "opaque"
        return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    blocks = [{"type": "thinking", "thinking": "opaque", "signature": "signature-token"}, {"type": "text", "text": "查看上下文" if calls else "总额 42"}]
    if calls:
        blocks.append({"type": "tool_use", "id": "call-1", "name": "get_workspace_context", "input": {}})
    return {"type": "message", "role": "assistant", "content": blocks, "stop_reason": "tool_use" if calls else "end_turn", "usage": {"input_tokens": 10, "output_tokens": 4}}


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def config(self, protocol="openai_chat_completions"):
        return {"protocol": protocol, "base_url": "https://controller.invalid/v1", "model": "synthetic", "revision": 1}

    async def test_two_native_tool_rounds_both_protocols(self):
        for protocol in ("openai_chat_completions", "anthropic_messages"):
            requests = []
            def handler(req):
                body = json.loads(req.content)
                requests.append(body)
                if len(requests) > 1:
                    self.assertIn("42", json.dumps(body["messages"]))
                    if protocol == "anthropic_messages":
                        self.assertEqual(body["messages"][1]["content"][0]["signature"], "signature-token")
                    else:
                        self.assertEqual(body["messages"][2]["reasoning_content"], "opaque")
                raw = reply(protocol, calls=len(requests) <= 2)
                if protocol == "openai_chat_completions" and len(requests) == 2:
                    raw["choices"][0]["message"]["tool_calls"][0]["id"] = "call-2"
                if protocol == "anthropic_messages" and len(requests) == 2:
                    raw["content"][-1]["id"] = "call-2"
                return httpx.Response(200, json=raw)
            messages = [{"role": "user", "content": "查询"}]
            for step in range(3):
                response = await request(self.config(protocol), "synthetic-key", payload_for(self.config(protocol), messages), str(step), transport=httpx.MockTransport(handler))
                messages.append(response)
                if response["calls"]:
                    messages.append({"role": "tool", "call_id": response["calls"][0]["call_id"], "result": {"status": "success", "summary": "42"}})
            self.assertEqual(response["content"], "总额 42")
            self.assertEqual(len(requests), 3)
            self.assertEqual(response["usage"], {"input_tokens": 10, "output_tokens": 4})

    async def test_multiple_calls_pair_in_order_and_preserve_blocks(self):
        for protocol in ("openai_chat_completions", "anthropic_messages"):
            raw = reply(protocol)
            if protocol == "openai_chat_completions":
                calls = raw["choices"][0]["message"]["tool_calls"]
                calls.append({**deepcopy(calls[0]), "id": "call-2"})
            else:
                raw["content"].append({**deepcopy(raw["content"][-1]), "id": "call-2"})
                raw["content"].insert(1, {"type": "redacted_thinking", "data": "opaque-data"})
            normalized = normalize_response(protocol, raw, "m")
            messages = [{"role": "user", "content": "查询"}, normalized]
            with self.assertRaises(ProviderFault):
                payload_for(self.config(protocol), messages)
            results = [{"role": "tool", "call_id": name, "result": {"status": "success", "summary": "ok"}} for name in ("call-1", "call-2")]
            with self.assertRaises(ProviderFault):
                payload_for(self.config(protocol), messages + list(reversed(results)))
            body = payload_for(self.config(protocol), messages + results)
            if protocol == "anthropic_messages":
                self.assertEqual(body["messages"][1]["content"], raw["content"])
                self.assertEqual(len(body["messages"][-1]["content"]), 2)

    async def test_invalid_batches_and_truncation_never_normalize(self):
        protocol = "openai_chat_completions"
        for arguments in ('{"page_id":', '{"page_id":true}', '{"page_id":"x","page_id":"y"}', '[]', '{"page_id":NaN}'):
            raw = reply(protocol)
            raw["choices"][0]["message"]["tool_calls"].append({"id": "call-2", "type": "function", "function": {"name": "read_page_result", "arguments": arguments}})
            with self.subTest(arguments=arguments), self.assertRaises(ProviderFault):
                normalize_response(protocol, raw, "m")
        for stop, code in (("length", "truncated_response"), ("content_filter", "refusal"), ("unexpected", "unsupported_capability")):
            raw = reply(protocol)
            raw["choices"][0]["finish_reason"] = stop
            with self.assertRaises(ProviderFault) as caught:
                normalize_response(protocol, raw, "m")
            self.assertEqual(caught.exception.code, code)
        raw = reply(protocol)
        raw["choices"][0]["message"]["tool_calls"] *= 2
        with self.assertRaises(ProviderFault):
            normalize_response(protocol, raw, "m")
        raw = reply(protocol, False)
        raw["choices"][0]["message"]["content"] = '{"tool":"shell","arguments":{"cmd":"bad"}}'
        self.assertEqual(normalize_response(protocol, raw, "m")["calls"], [])

    async def test_http_errors_redaction_and_no_retries(self):
        for status, code in ((401, "authentication"), (403, "authentication"), (429, "rate_limited"), (500, "provider_unavailable"), (302, "provider_unavailable")):
            seen = []
            def handler(req):
                seen.append(req)
                return httpx.Response(status, text="secret-provider-body", headers={"location": "https://evil.invalid"})
            with self.assertRaises(ProviderFault) as caught:
                await request(self.config(), "synthetic-key", {}, "m", transport=httpx.MockTransport(handler))
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(len(seen), 1)
        def echo(req):
            raw = reply("openai_chat_completions", False)
            raw["choices"][0]["message"]["content"] = "leak synthetic-key"
            return httpx.Response(200, json=raw)
        result = await request(self.config(), "synthetic-key", {}, "m", transport=httpx.MockTransport(echo))
        self.assertNotIn("synthetic-key", json.dumps(result))

    async def test_timeout_cancel_and_reply_size(self):
        async def timeout(req):
            raise httpx.ReadTimeout("synthetic-key must not appear")
        with self.assertRaises(ProviderFault) as caught:
            await request(self.config(), "synthetic-key", {}, "m", transport=httpx.MockTransport(timeout))
        self.assertEqual(caught.exception.code, "response_unknown")
        async def cancel(req):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await request(self.config(), "key", {}, "m", transport=httpx.MockTransport(cancel))
        with self.assertRaises(ProviderFault):
            await request(self.config(), "key", {}, "m", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))))

    async def test_logged_response_replay_and_unknown_post(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Store(directory))
            project = store.business.project("test")["id"]
            session = store.create_session(project, {"client_request_id": "s", "title": "test"})
            run = store.create_run(project, session["id"], {"client_request_id": "r", "content": "查询"}, context={}, config={}, limits={})
            calls = []
            def handler(req):
                calls.append(req)
                return httpx.Response(200, json=reply("openai_chat_completions", False))
            args = (store, run, 1, 0, self.config(), "secret-key", [{"role": "user", "content": "查询"}])
            first = await logged_request(*args, transport=httpx.MockTransport(handler))
            self.assertEqual(await logged_request(*args, transport=httpx.MockTransport(handler)), first)
            self.assertEqual(len(calls), 1)
            async def lost(req):
                calls.append(req)
                raise httpx.ReadError("lost")
            args = (store, run, 1, 1, self.config(), "secret-key", [{"role": "user", "content": "下一步"}])
            for _ in range(2):
                with self.assertRaises(ProviderFault) as caught:
                    await logged_request(*args, transport=httpx.MockTransport(lost))
                self.assertEqual(caught.exception.code, "response_unknown")
            self.assertEqual(len(calls), 2)

    async def test_logged_http_cancel_before_and_during_post_never_replays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Store(directory))
            project = store.business.project('cancel HTTP')['id']
            session = store.create_session(project, {'client_request_id': 's', 'title': 'cancel HTTP'})
            run = store.create_run(project, session['id'], {'client_request_id': 'r', 'content': '查询'},
                                   context={}, config={}, limits={})
            args = (store, run, 1, 0, self.config(), 'synthetic-key', [{'role': 'user', 'content': '查询'}])
            entered = asyncio.Event()
            posts = []
            async def pending(request):
                posts.append(request)
                entered.set()
                await asyncio.Event().wait()
            transport = httpx.MockTransport(pending)
            task = asyncio.create_task(logged_request(*args, transport=transport))
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(store.business.rows('SELECT state FROM agent_model_requests')[0]['state'], 'sent')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for _ in range(2):
                with self.assertRaises(ProviderFault) as caught:
                    await logged_request(*args, transport=transport)
                self.assertEqual(caught.exception.code, 'response_unknown')
            self.assertEqual(len(posts), 1)
            store.interrupt_on_startup()
            self.assertEqual(store.business.rows('SELECT state FROM agent_model_requests')[0]['state'], 'unknown')
            with self.assertRaises(Conflict):
                await logged_request(*args, transport=transport)
            self.assertEqual(len(posts), 1)

            # Fencing a cancelled generation before request reservation avoids
            # even an attempted outbound request.
            next_session = store.create_session(project, {'client_request_id': 's2', 'title': 'second session'})
            fresh = store.create_run(project, next_session['id'], {'client_request_id': 'r2', 'content': '查询'},
                                     context={}, config={}, limits={})
            store.interrupt_on_startup()
            with self.assertRaises(Conflict):
                await logged_request(store, fresh, 1, 0, self.config(), 'synthetic-key',
                                     [{'role': 'user', 'content': '查询'}], transport=transport)
            self.assertEqual(len(posts), 1)
            self.assertEqual(len(store.business.rows('SELECT * FROM agent_model_requests')), 1)

    async def test_local_http_post_cancel_after_server_receives_body(self):
        entered, release = asyncio.Event(), asyncio.Event()
        posts = []
        async def handler(reader, writer):
            try:
                headers = await reader.readuntil(b'\r\n\r\n')
                size = next(int(line.split(b':', 1)[1].strip()) for line in headers.split(b'\r\n')
                            if line.lower().startswith(b'content-length:'))
                body = await reader.readexactly(size)
                posts.append(body)
                entered.set()
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        try:
            config = {**self.config(), 'base_url': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1'}
            with tempfile.TemporaryDirectory() as directory:
                store = AgentStore(Store(directory))
                project = store.business.project('local HTTP')['id']
                session = store.create_session(project, {'client_request_id': 's', 'title': 'local HTTP'})
                run = store.create_run(project, session['id'], {'client_request_id': 'r', 'content': '查询'},
                                       context={}, config={}, limits={})
                args = (store, run, 1, 0, config, 'synthetic-key', [{'role': 'user', 'content': '查询'}])
                task = asyncio.create_task(logged_request(*args))
                await asyncio.wait_for(entered.wait(), 5)
                self.assertEqual(len(posts), 1)
                self.assertEqual(store.business.rows('SELECT state FROM agent_model_requests')[0]['state'], 'sent')
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProviderFault) as caught:
                    await logged_request(*args)
                self.assertEqual(caught.exception.code, 'response_unknown')
                self.assertEqual(len(posts), 1)
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    async def test_rejected_response_replays_error_without_new_post(self):
        with tempfile.TemporaryDirectory() as root:
            agent = AgentStore(Store(root))
            project = agent.business.project('Rejection')['id']
            session = agent.create_session(project, {'client_request_id': 's', 'title': 'Rejection'})
            run = agent.create_run(project, session['id'], {'client_request_id': 'r', 'content': 'query'}, context={}, config={}, limits={})
            requests = []
            def handler(request):
                requests.append(True)
                raw = reply('openai_chat_completions')
                raw['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{"unknown_field":true}'
                return httpx.Response(200, json=raw)
            for _ in range(2):
                with self.assertRaises(ProviderFault) as caught:
                    await logged_request(agent, run, 1, 0, self.config(), 'synthetic-key', [{'role': 'user', 'content': 'query'}], transport=httpx.MockTransport(handler))
                self.assertEqual(caught.exception.code, 'invalid_arguments')
            self.assertEqual(len(requests), 1)
            self.assertEqual(agent.business.rows('SELECT state FROM agent_model_requests')[0]['state'], 'rejected')
