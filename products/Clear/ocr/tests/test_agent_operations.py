import unittest
import time

import test_agent_policy
from ocr_workbench.agent.operations import Operations
from ocr_workbench.store import Conflict


class OperationTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def arguments(self):
        return {"version_ids": [self.photo["active_version"]], "engines": ["glm"]}

    def authorized(self):
        self.policy.grant(self.project, "scoped_processing", {
            "document_ids": [self.photo["id"]], "page_ids": [self.photo["id"]],
            "version_ids": [self.photo["active_version"]], "engines": ["glm"]},
            source="user_request", expires=time.time() + 60, run_id=self.run["id"])
        call = {"call_id": "first", "tool": "run_ocr", "arguments": self.arguments()}
        self.agent.record_calls(self.project, self.run["id"], 1, 0, [call])
        context = {"run": self.run, "generation": 1, "step": 0, "call_id": "first"}
        return Operations(self.agent, self.policy), context

    def effect(self, db, operation_id):
        ids = self.store._enqueue(db, self.project, self.arguments()["version_ids"], ["glm"], request_id=operation_id)
        return {"task_ids": ids}, [{"job_id": key, "kind": "ocr", "ownership": "created", "state": "queued"} for key in ids]

    def test_plain_ocr_request_id_and_conflicting_request(self):
        first = self.store.enqueue(self.project, self.arguments()["version_ids"], ["glm"], request_id="request-123")
        self.assertEqual(self.store.enqueue(self.project, self.arguments()["version_ids"], ["glm"], request_id="request-123"), first)
        with self.assertRaises(Conflict):
            self.store.enqueue(self.project, self.arguments()["version_ids"], ["ppocr"], request_id="request-123")
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks")), 1)

    def test_effect_and_link_rollback_together(self):
        operations, context = self.authorized()
        def crash(window):
            if window == "after_effect":
                raise RuntimeError("crash injection")
        with self.assertRaises(RuntimeError):
            operations.submit(context, "run_ocr", self.arguments(), self.effect, fault=crash)
        for table in ("tasks", "agent_operations", "agent_job_links", "ocr_submissions"):
            self.assertEqual(self.store.rows("SELECT * FROM " + table), [])

    def test_post_commit_crash_and_semantic_second_call_reuse(self):
        operations, context = self.authorized()
        def crash(window):
            if window == "after_commit":
                raise RuntimeError("lost response")
        with self.assertRaises(RuntimeError):
            operations.submit(context, "run_ocr", self.arguments(), self.effect, fault=crash)
        first = operations.submit(context, "run_ocr", self.arguments(), self.effect)
        self.agent.record_calls(self.project, self.run["id"], 1, 1, [{"call_id": "second", "tool": "run_ocr", "arguments": self.arguments()}])
        second = operations.submit({**context, "step": 1, "call_id": "second"}, "run_ocr", self.arguments(), self.effect)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_operations")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_job_links")), 1)
        self.assertEqual(len({r["operation_id"] for r in self.store.rows("SELECT * FROM agent_calls")}), 1)
        self.assertEqual(self.store.rows("SELECT state FROM agent_operations")[0]["state"], "submitted")

    def test_zero_job_effect_finishes_atomically_and_post_commit_replay_reuses_it(self):
        operations, context = self.authorized()
        with self.store.transaction() as db:
            db.execute("INSERT INTO agent_operations(id,project_id,run_id,operation_key,input_hash,generation,state,created,updated) "
                       "VALUES('unrelated-old',?,?, 'unrelated','old-hash',1,'submitted','now','now')",
                       (self.project, self.run['id']))
        effects = []
        def synchronous(db, operation_id):
            effects.append(operation_id)
            return {"done": True}, []
        def crash(window):
            if window == "after_commit":
                raise RuntimeError("lost response")
        with self.assertRaises(RuntimeError):
            operations.submit(context, "run_ocr", self.arguments(), synchronous, fault=crash)
        row = self.store.rows("SELECT id,state,result FROM agent_operations WHERE id<>'unrelated-old'")[0]
        self.assertEqual(row["state"], "finished")
        self.assertEqual(operations.submit(context, "run_ocr", self.arguments(), synchronous)["operation_id"], row["id"])
        self.assertEqual(effects, [row["id"]])
        self.assertEqual(self.store.rows("SELECT * FROM agent_job_links"), [])
        self.assertEqual(self.store.rows("SELECT state FROM agent_operations WHERE id='unrelated-old'")[0]['state'], 'submitted')

    def test_generation_fence_prevents_late_submit(self):
        operations, context = self.authorized()
        self.agent.interrupt_on_startup()
        with self.assertRaises(Conflict):
            operations.submit(context, "run_ocr", self.arguments(), self.effect)
        self.assertFalse(self.store.rows("SELECT * FROM tasks"))
