import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from ocr_workbench.service import create_app
from ocr_workbench.external_review import CredentialVault
from ocr_workbench.agent.providers import request as provider_request
from test_agent_connection import echo_probe
from test_agent_provider import reply


class AgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_api_session_events_and_controller_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            (bundle / "config").mkdir(parents=True)
            (bundle / "config/engines.json").write_text("{}", encoding="utf-8")
            app = create_app(bundle, root / "workspace", "synthetic-local-token", start_queue=False, agent_enabled=True)
            project_id = app.state.store.project("API 合成项目")["id"]
            app.state.agent_connection.vault = CredentialVault(root / "credentials")
            await app.state.agent_connection.save({"protocol": "openai_chat_completions", "base_url": "https://synthetic.invalid/v1", "model": "synthetic", "api_key": "synthetic-key"}, transport=httpx.MockTransport(echo_probe))
            counts = []
            def model(req):
                counts.append(True)
                return httpx.Response(200, json=reply("openai_chat_completions", calls=len(counts) == 1))
            async def call(config, key, payload, message_id, **kwargs):
                return await provider_request(config, key, payload, message_id, transport=httpx.MockTransport(model))
            with patch("ocr_workbench.agent.providers.request", side_effect=call):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                        self.assertEqual((await client.get("/api/agent/status")).status_code, 401)
                        client.headers["Authorization"] = "Bearer synthetic-local-token"
                        status = (await client.get("/api/agent/status")).json()
                        self.assertTrue(status["available"])
                        saved = (await client.post(f"/api/projects/{project_id}/agent/sessions", json={"client_request_id": "new-session", "title": "测试"})).json()
                        session_id = saved["id"]
                        self.assertNotIn("graph_thread_id", saved)
                        message = {"client_request_id": "new-message", "content": "查看上下文"}
                        denied = await client.post(f"/api/agent/sessions/{session_id}/messages", json=message)
                        self.assertEqual(denied.status_code, 400)
                        before = await client.get(f'/api/projects/{project_id}/agent/controller-authorization')
                        self.assertFalse(before.json()['authorized'])
                        consent = await client.put(f"/api/projects/{project_id}/agent/controller-authorization", json={"revision": 1, "allow": True})
                        self.assertEqual(consent.status_code, 200)
                        after = await client.get(f'/api/projects/{project_id}/agent/controller-authorization')
                        self.assertTrue(after.json()['authorized'])
                        self.assertEqual(after.json()['revision'], 1)
                        response = await client.post(f"/api/agent/sessions/{session_id}/messages", json=message)
                        self.assertEqual(response.status_code, 200, response.text)
                        run_id = response.json()["run"]["id"]
                        for _ in range(100):
                            run = (await client.get(f"/api/agent/runs/{run_id}")).json()
                            if run["status"] in {"completed", "failed"}:
                                break
                            await asyncio.sleep(.02)
                        self.assertEqual(run["status"], "completed", run)
                        self.assertEqual(len(counts), 2)
                        replay = await client.post(f"/api/agent/sessions/{session_id}/messages", json=message)
                        self.assertEqual(replay.json()["run"]["id"], run_id)
                        self.assertEqual(len(counts), 2)
                        events = (await client.get(f"/api/agent/sessions/{session_id}/events?limit=2")).json()
                        self.assertEqual([e["seq"] for e in events["events"]], [1, 2])
                        following = (await client.get(f"/api/agent/sessions/{session_id}/events?after_seq=2")).json()
                        self.assertTrue(all(e["seq"] > 2 for e in following["events"]))
                        forged = await client.post(f"/api/agent/runs/{run_id}/resume", json={"client_request_id": "bad", "generation": 1, "thread_id": "arbitrary"})
                        self.assertEqual(forged.status_code, 422)
                        self.assertNotIn("synthetic-key", json.dumps(following))
