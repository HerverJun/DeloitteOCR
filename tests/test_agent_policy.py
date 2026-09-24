import asyncio
from pathlib import Path
import tempfile
import time
import unittest

from PIL import Image
from ocr_workbench.imaging import add_image
from ocr_workbench.store import Store, Conflict
from ocr_workbench.agent.store import AgentStore
from ocr_workbench.agent.policy import AgentPolicy, PolicyDenied
from ocr_workbench.agent.registry import ToolRegistry


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = AgentStore(self.store)
        self.project = self.store.project("本项目")["id"]
        self.other = self.store.project("另一项目")["id"]
        path = Path(self.temp.name) / "tiny.png"
        Image.new("RGB", (3, 3), "white").save(path)
        self.photo = add_image(self.store, self.project, "one.png", path)
        Image.new("RGB", (3, 3), "white").save(path)
        self.other_photo = add_image(self.store, self.other, "two.png", path)
        self.session = self.agent.create_session(self.project, {"client_request_id": "s", "title": "test"})
        self.run = self.agent.create_run(self.project, self.session["id"], {"client_request_id": "r", "content": "读取"}, context={}, config={}, limits={})
        self.policy = AgentPolicy(self.agent)

    def test_cross_project_ids_and_unsupported_tools(self):
        bad = self.other_photo
        for name, args in (("search_document", {"document_id": bad["id"], "query": "q"}),
                           ("read_page_result", {"page_id": bad["id"]}),
                           ("run_ocr", {"version_ids": [bad["active_version"]], "engines": ["glm"]}),
                           ("process_pages", {"document_id": bad["id"], "page_numbers": [1]})):
            with self.subTest(name=name), self.assertRaises(PolicyDenied):
                self.policy.authorize(self.run, 1, name, args)
        with self.assertRaises(ValueError):
            self.policy.authorize(self.run, 1, "shell", {"command": "whoami"})
        with self.assertRaises(PolicyDenied):
            self.policy.authorize(self.run, 1, "get_job_status", {"jobs": [{"kind": "ocr", "job_id": "forged"}]})
        with self.assertRaises(PolicyDenied):
            self.policy.authorize(self.run, 1, "get_workspace_context", {"selection_token": "forged"})

    def test_scoped_grant_reuse_no_implicit_expansion(self):
        args = {"document_id": self.photo["id"], "page_numbers": [1], "mode": "native"}
        with self.assertRaises(PolicyDenied):
            self.policy.authorize(self.run, 1, "process_pages", args)
        scope = {"document_ids": [self.photo["id"]], "page_ids": [self.photo["id"]], "mode": "native", "force": False}
        self.policy.grant(self.project, "scoped_processing", scope, source="user_request", expires=time.time() + 60, run_id=self.run["id"])
        for _ in range(2):
            self.policy.authorize(self.run, 1, "process_pages", args)
        with self.assertRaises(PolicyDenied):
            self.policy.authorize(self.run, 1, "process_pages", {**args, "force": True})
        with self.assertRaises(ValueError):
            self.policy.grant(self.project, "scoped_processing", scope, source="model_claim", expires=time.time() + 60)
        review = AgentPolicy(self.agent, review_only=True)
        review.authorize(self.run, 1, "process_pages", args)
        with self.assertRaises(PolicyDenied):
            review.authorize(self.run, 1, "process_pages", {**args, "mode": "ocr", "engine": "glm"})

    def test_outbound_binding_cannot_reuse_visual_for_controller(self):
        scope = {"role": "controller", "endpoint": "https://test.invalid/v1", "config_revision": 1,
                 "document_ids": [self.photo["id"]], "page_ids": [self.photo["id"]], "data_kinds": ["text"]}
        self.policy.grant(self.project, "controller_content", scope, source="bound_ui_scope", expires=time.time() + 60)
        self.policy.authorize_outbound(self.run, **scope)
        for change in ({"role": "visual"}, {"endpoint": "https://different.invalid"}, {"config_revision": 2}, {"data_kinds": ["image"]}, {"page_ids": [self.other_photo["id"]]}):
            with self.assertRaises(PolicyDenied):
                self.policy.authorize_outbound(self.run, **{**scope, **change})

    def test_entire_batch_checked_before_any_handler(self):
        registry = ToolRegistry(self.policy)
        called = []
        async def handler(args, context):
            called.append(True)
            return {"status": "success", "summary": "ok"}
        registry.register("read_page_result", handler)
        calls = [{"call_id": "first", "tool": "read_page_result", "arguments": {"page_id": self.photo["id"]}},
                 {"call_id": "second", "tool": "read_page_result", "arguments": {"page_id": self.other_photo["id"]}}]
        with self.assertRaises(PolicyDenied):
            registry.validate_batch(self.run, 1, calls)
        self.assertEqual(called, [])
        valid = registry.validate_batch(self.run, 1, calls[:1])
        self.agent.interrupt_on_startup()
        with self.assertRaises(Conflict):
            asyncio.run(registry.execute(self.run, 1, valid[0], {}))
        self.assertEqual(called, [])

    def test_decision_hash_expiry_and_reply_dedup(self):
        payload = {"question": "哪一页？", "options": [{"id": "one", "label": "第一页"}, {"id": "two", "label": "第二页"}]}
        decision = self.agent.create_decision(self.project, self.run["id"], 1, payload)
        duplicate = self.agent.create_decision(self.project, self.run["id"], 1, payload)
        self.assertEqual(decision["id"], duplicate["id"])
        reply = {"client_request_id": "answer", "option_id": "one", "payload_hash": decision["payload_hash"]}
        with self.assertRaises(Conflict):
            self.agent.reply_decision(self.project, self.run["id"], decision["id"], {**reply, "payload_hash": "0" * 64})
        self.agent.reply_decision(self.project, self.run["id"], decision["id"], reply)
        self.agent.reply_decision(self.project, self.run["id"], decision["id"], reply)
        self.assertEqual(len([e for e in self.agent.events(self.project, self.session["id"]) if e["type"] == "decision_resolved"]), 1)
        with self.assertRaises(Conflict):
            self.agent.reply_decision(self.project, self.run["id"], decision["id"], {**reply, "option_id": "two"})
        self.assertFalse(self.store.rows("SELECT * FROM agent_grants"), "Clarification never self-authorizes a write")
