import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest

import test_agent_policy
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.store import digest
from ocr_workbench.documents import Documents


class JobTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    async def runtime(self, *, existing=False):
        bundle = Path(self.temp.name) / "bundle"
        bundle.mkdir()
        documents = Documents(self.store, bundle, None)
        maintenance = SimpleNamespace(guard=self.store.file_lock)
        services = ApplicationServices(self.store, documents, maintenance)
        # Runtime startup interrupts existing unfinished runs; open before making the run used below.
        with self.store.transaction() as db:
            db.execute("UPDATE agent_runs SET status='cancelled' WHERE id=?", (self.run["id"],))
        async def model(state, run):
            if state["step"] == 0:
                message = {"role": "assistant", "tool_calls": [{"id": "pages", "type": "function", "function": {
                    "name": "process_pages", "arguments": json.dumps({"document_id": self.photo["id"], "page_numbers": [1], "mode": "native", "force": True})}}]}
            else:
                message = {"role": "assistant", "content": "已核对后台任务状态。"}
            record, fresh = self.agent.begin_model_request(self.project, run["id"], state["generation"], state["step"], digest(message))
            if not fresh:
                return json.loads(record["normalized_response"])
            response = normalize_response("openai_chat_completions", {"choices": [{"message": message, "finish_reason": "tool_calls" if state["step"] == 0 else "stop"}]}, record["request_id"])
            self.agent.finish_model_request(self.project, run["id"], state["generation"], record["request_id"], response)
            return response
        runtime = await AgentRuntime(services, policy={"limits": {}}, model_override=model).open()
        self.run = self.agent.create_run(self.project, self.session["id"], {"client_request_id": "work", "content": "处理第一页"}, context={}, config={}, limits={})
        runtime.policy.grant(self.project, "scoped_processing", {"document_ids": [self.photo["id"]], "page_ids": [self.photo["id"]], "mode": "native", "force": True}, source="user_request", expires=time.time() + 60, run_id=self.run["id"])
        if existing:
            documents.process_pages([self.photo["id"]], "native", force=True)
        return runtime

    async def wait_status(self, expected):
        for _ in range(150):
            run = self.agent.run(self.project, self.run["id"])
            if run["status"] == expected:
                return run
            if run["status"] == "failed":
                self.fail(str(self.agent.events(self.project, self.session["id"])))
            await asyncio.sleep(.02)
        self.fail(f"Expected {expected}: {run}")

    async def test_waiting_jobs_automatically_resumes_without_extra_model_poll(self):
        runtime = await self.runtime()
        try:
            runtime.schedule(self.run)
            await self.wait_status("waiting_jobs")
            self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests WHERE run_id=?", (self.run["id"],))), 1)
            stage = self.store.claim_document_stage()
            self.assertIsNotNone(stage)
            self.store.finish_document_stage(stage["id"], {"synthetic": True})
            await self.wait_status("completed")
            pending = runtime.tasks.get(self.run["id"])
            if pending:
                await pending
            self.assertEqual(len(self.store.rows("SELECT * FROM document_stages")), 1)
            self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests WHERE run_id=?", (self.run["id"],))), 2)
            intents = self.store.rows("SELECT * FROM agent_resume_intents")
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0]["state"], "applied")
            run = self.agent.run(self.project, self.run['id'])
            self.assertEqual(run['outcome'], 'partial')
            self.assertEqual(run['coverage']['uncovered_count'], 1)
        finally:
            await runtime.close()

    async def test_stalled_job_requires_decision_without_cancelling_or_model_polling(self):
        runtime = await self.runtime()
        try:
            runtime.schedule(self.run)
            await self.wait_status('waiting_jobs')
            if self.run['id'] in runtime.tasks:
                await runtime.tasks[self.run['id']]
            with self.store.transaction() as db:
                db.execute("UPDATE agent_operations SET updated='2020-01-01T00:00:00+00:00' WHERE run_id=?", (self.run['id'],))
            await runtime._observe_once()
            await self.wait_status('waiting_user')
            if self.run['id'] in runtime.tasks:
                await runtime.tasks[self.run['id']]
            decision = self.store.rows("SELECT * FROM agent_decisions WHERE run_id=? AND status='pending'", (self.run['id'],))[0]
            self.assertEqual(decision['kind'], 'no_job_progress')
            self.assertEqual(self.store.rows('SELECT status FROM document_stages')[0]['status'], 'queued')
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), 1)
            self.agent.reply_decision(self.project, self.run['id'], decision['id'], {'client_request_id': 'wait', 'payload_hash': decision['payload_hash'], 'option_id': 'continue'})
            await runtime._observe_once()
            await self.wait_status('waiting_jobs')
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
            stage = self.store.claim_document_stage()
            self.store.finish_document_stage(stage['id'], {'synthetic': True})
            await self.wait_status('completed')
        finally:
            await runtime.close()

    async def test_inbox_wakes_waiting_jobs_without_cancelling_submitted_stage(self):
        runtime = await self.runtime()
        try:
            runtime.schedule(self.run)
            await self.wait_status('waiting_jobs')
            runtime.inbox.enqueue(self.project, self.session['id'], {'client_request_id': 'append', 'content': '只报告进度，不等待完成'})
            run = await self.wait_status('completed')
            self.assertEqual(run['outcome'], 'partial')
            self.assertEqual(self.store.rows('SELECT status FROM document_stages')[0]['status'], 'queued')
            self.assertEqual(self.store.rows('SELECT state FROM agent_inbox')[0]['state'], 'applied')
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
        finally:
            await runtime.close()

    async def test_native_incomplete_is_uncovered_and_unchanged_poll_does_not_write(self):
        runtime = await self.runtime()
        try:
            runtime.schedule(self.run)
            await self.wait_status('waiting_jobs')
            stage = self.store.claim_document_stage()
            task = self.store.enqueue(self.project, [self.photo['active_version']], ['glm'])[0]
            self.store.claim()
            self.store.complete(task, {'text': 'partial native text', 'tables': [], 'blocks': [], 'engine': 'glm',
                                       'document': {'native_only_incomplete': True, 'unprocessed_regions': 1}})
            result_id = self.store.one('tasks', task)['result_id']
            self.store.finish_document_stage(stage['id'], {'result_id': result_id})
            operation = self.store.rows("SELECT operation_id FROM agent_job_links")[0]['operation_id']
            first = runtime.jobs.operation_result(self.project, self.run['id'], operation)
            self.assertEqual(first['status'], 'partial')
            self.assertEqual(first['data']['coverage']['uncovered_count'], 1)
            self.policy = runtime.policy
            self.policy.authorize(self.run, 1, 'read_page_result', {'page_id': self.photo['id'], 'source': 'run_result', 'result_id': result_id})
            events = len(self.agent.events(self.project, self.session['id']))
            self.assertEqual(first, runtime.jobs.operation_result(self.project, self.run['id'], operation))
            self.assertEqual(len(self.agent.events(self.project, self.session['id'])), events)
        finally:
            await runtime.close()

    async def test_cancel_created_and_preserve_reused_stages(self):
        for reused in (False, True):
            # Separate tests need independent workspace IDs; the second pass uses fresh setup.
            if reused:
                self.setUp()
            runtime = await self.runtime(existing=reused)
            try:
                runtime.schedule(self.run)
                await self.wait_status("waiting_jobs")
                link = self.store.rows("SELECT * FROM agent_job_links")[0]
                self.assertEqual(link["ownership"], "reused" if reused else "created")
                await runtime.cancel(self.project, self.run["id"], self.run["generation"], "cancel_owned_jobs")
                stage = self.store.one("document_stages", link["job_id"])
                self.assertEqual(stage["status"], "queued" if reused else "cancelled")
                self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests WHERE run_id=?", (self.run["id"],))), 1)
            finally:
                await runtime.close()
