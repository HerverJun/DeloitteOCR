import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace

import test_agent_jobs
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.store import Conflict


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_jobs.JobTests.setUp
    runtime = test_agent_jobs.JobTests.runtime
    wait_status = test_agent_jobs.JobTests.wait_status

    def config_revision(self):
        with self.store.transaction() as db:
            db.execute("UPDATE agent_runs SET config=? WHERE id=?", (json.dumps({"revision": 1}), self.run['id']))

    async def reopen(self, previous):
        return await AgentRuntime(previous.services, policy={"limits": {}},
            connection=SimpleNamespace(assert_current=lambda revision: None), model_override=previous.model_override).open()

    async def test_real_process_exits_after_business_commit_before_tool_reply(self):
        # The child uses the real graph/handler/SQLite saver and dies inside the
        # operation after_commit hook, before the tool or graph can get its result.
        with self.store.transaction() as db:
            db.execute("UPDATE agent_runs SET status='cancelled' WHERE id=?", (self.run['id'],))
        root = Path(self.temp.name)
        worker = Path(__file__).parent / 'agent_fixtures' / 'replay_fault_worker.py'
        source = str(Path(__file__).parents[1] / 'src')
        environment = {**os.environ, 'PYTHONPATH': source}
        process = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-c', 'import runpy,sys; sys.path.insert(0,sys.argv[1]); '
             'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
             source, str(worker), str(root), self.project, self.session['id'], self.photo['id']],
            env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 91, process.stdout + process.stderr)
        self.run = self.agent.run(self.project, self.store.rows(
            "SELECT id FROM agent_runs WHERE client_request_id='fault-window'")[0]['id'])
        self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_job_links')), 1)
        self.assertIsNone(self.store.rows('SELECT result_ref FROM agent_calls WHERE run_id=?', (self.run['id'],))[0]['result_ref'])
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (self.run['id'],))), 1)

        from ocr_workbench.application_services import ApplicationServices
        from ocr_workbench.documents import Documents
        calls = []
        async def model(state, run):
            calls.append(state['step'])
            self.assertEqual(state['step'], 1, 'the checkpoint must not re-POST the already received model step')
            message = {'role': 'assistant', 'content': '已核对后台任务状态。'}
            record, fresh = self.agent.begin_model_request(self.project, run['id'], state['generation'],
                                                             state['step'], test_agent_jobs.digest(message))
            self.assertTrue(fresh)
            response = normalize_response('openai_chat_completions', {'choices': [{'message': message,
                'finish_reason': 'stop'}]}, record['request_id'])
            self.agent.finish_model_request(self.project, run['id'], state['generation'], record['request_id'], response)
            return response
        services = ApplicationServices(self.store, Documents(self.store, root / 'bundle', None),
                                       SimpleNamespace(guard=self.store.file_lock))
        runtime = await AgentRuntime(services, policy={'limits': {}},
            connection=SimpleNamespace(assert_current=lambda revision: None), model_override=model).open()
        try:
            current = self.agent.run(self.project, self.run['id'])
            self.assertEqual(current['status'], 'interrupted')
            self.assertEqual(calls, [])
            await runtime.resume_interrupted(self.project, current['id'], current['generation'])
            await self.wait_status('waiting_jobs')
            self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (current['id'],))), 1)
            stage = self.store.claim_document_stage()
            self.store.finish_document_stage(stage['id'], {'synthetic': True})
            await asyncio.gather(runtime._observe_once(), runtime._observe_once())
            await self.wait_status('completed')
            self.assertEqual(calls, [1])
            self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (current['id'],))), 2)
            self.assertEqual(len([event for event in self.agent.events(self.project, self.session['id'])
                                  if event['type'] == 'tool_finished']), 1)
        finally:
            await runtime.close()

    async def test_job_finishes_before_graph_waits_and_duplicate_observation_is_noop(self):
        runtime = await self.runtime()
        try:
            original = runtime.jobs.operation_result
            finished = False
            def finish_before_wait(*args, **kwargs):
                nonlocal finished
                if not finished:
                    finished = True
                    stage = self.store.claim_document_stage()
                    self.assertIsNotNone(stage)
                    self.store.finish_document_stage(stage['id'], {'synthetic': True})
                return original(*args, **kwargs)
            runtime.jobs.operation_result = finish_before_wait
            runtime.schedule(self.run)
            await self.wait_status('completed')
            await asyncio.gather(runtime._observe_once(), runtime._observe_once())
            self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (self.run['id'],))), 2)
            self.assertEqual(self.store.rows('SELECT * FROM agent_resume_intents'), [])
            self.assertEqual(len([event for event in self.agent.events(self.project, self.session['id'])
                                  if event['type'] == 'tool_finished']), 1)
        finally:
            await runtime.close()

    async def test_restart_waiting_jobs_does_not_resume_before_completion(self):
        first = await self.runtime()
        self.config_revision()
        first.schedule(self.run)
        await self.wait_status('waiting_jobs')
        await first.close()
        current = self.agent.run(self.project, self.run['id'])
        self.assertEqual(current['status'], 'interrupted')
        second = await self.reopen(first)
        try:
            # Reproduce the business waiting state preceding the saver interrupt
            # receipt: update_state drops the marker but leaves next=await_jobs.
            config = {'configurable': {'thread_id': current['graph_thread_id']}}
            await second.graph.aupdate_state(config, {'generation': current['generation']})
            snapshot = await second.graph.aget_state(config)
            self.assertFalse(snapshot.interrupts)
            self.assertIn('await_jobs', snapshot.next)
            await second.resume_interrupted(self.project, current['id'], current['generation'])
            current = await self.wait_status('waiting_jobs')
            self.assertEqual(current['owner'], second.owner)
            self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests WHERE run_id=?", (current['id'],))), 1)
            stage = self.store.claim_document_stage()
            self.store.finish_document_stage(stage['id'], {"synthetic": True})
            await self.wait_status('completed')
            if current['id'] in second.tasks:
                await second.tasks[current['id']]
            self.assertEqual(len(self.store.rows("SELECT * FROM document_stages")), 1)
        finally:
            await second.close()

    async def test_resume_receipt_reconciles_without_replaying_unknown_wakeup(self):
        runtime = await self.runtime()
        try:
            runtime.schedule(self.run)
            await self.wait_status('waiting_jobs')
            if self.run['id'] in runtime.tasks:
                await runtime.tasks[self.run['id']]
            config = {'configurable': {'thread_id': self.run['graph_thread_id']}}
            snapshot = await runtime.graph.aget_state(config)
            item = snapshot.interrupts[0]
            with self.store.transaction() as db:
                db.execute("INSERT INTO agent_resume_intents VALUES(?,?,?,?,?,?,'claimed',?)", (
                    'lost-ack', self.run['id'], self.run['generation'], snapshot.config['configurable']['checkpoint_id'], item.id, 'jobs', '2026-09-21'))
            # A saver update clears the interrupt but does not mean the wakeup ran.
            await runtime.graph.aupdate_state(config, {'generation': self.run['generation']})
            await runtime._reconcile_resume_intents()
            self.assertEqual(self.store.rows("SELECT state FROM agent_resume_intents WHERE id='lost-ack'")[0]['state'], 'claimed')
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), 1)
            stage = self.store.claim_document_stage()
            self.store.finish_document_stage(stage['id'], {'synthetic': True})
            await self.wait_status('completed')
            if self.run['id'] in runtime.tasks:
                await runtime.tasks[self.run['id']]
            count = len(self.store.rows('SELECT * FROM agent_model_requests'))
            await runtime._reconcile_resume_intents()
            self.assertEqual(self.store.rows("SELECT state FROM agent_resume_intents WHERE id='lost-ack'")[0]['state'], 'applied')
            await runtime._reconcile_resume_intents()
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), count)
            with self.store.transaction() as db:
                db.execute("UPDATE agent_resume_intents SET state='pending',generation=0 WHERE id='lost-ack'")
            await runtime._reconcile_resume_intents()
            self.assertEqual(self.store.rows("SELECT state FROM agent_resume_intents WHERE id='lost-ack'")[0]['state'], 'obsolete')
        finally:
            await runtime.close()

    async def test_real_queue_recovery_resumes_created_but_preserves_reused(self):
        for reused in (False, True):
            if reused:
                self.setUp()
            first = await self.runtime(existing=reused)
            self.config_revision()
            first.schedule(self.run)
            await self.wait_status('waiting_jobs')
            await first.close()
            self.store.recover_document_stages()
            self.assertEqual(self.store.rows('SELECT status FROM document_stages')[0]['status'], 'paused')
            second = await self.reopen(first)
            try:
                current = self.agent.run(self.project, self.run['id'])
                await second.resume_interrupted(self.project, current['id'], current['generation'])
                stage = self.store.rows('SELECT * FROM document_stages')[0]
                self.assertEqual(stage['status'], 'paused' if reused else 'queued')
                if reused:
                    finished = await self.wait_status('completed')
                    self.assertEqual(finished['outcome'], 'partial')
                    self.assertEqual(self.store.one('document_stages', stage['id'])['status'], 'paused')
                else:
                    self.store.claim_document_stage()
                    self.store.finish_document_stage(stage['id'], {'synthetic': True})
                    await self.wait_status('completed')
            finally:
                await second.close()

    async def test_restart_rebinds_user_decision_without_implied_reply(self):
        first = await self.runtime()
        self.config_revision()
        calls = []
        async def ask(state, run):
            calls.append(state['step'])
            message = {"role": "assistant", "content": "完成"}
            if state['step'] == 0:
                message = {"role": "assistant", "tool_calls": [{"id": "ask", "type": "function", "function": {
                    "name": "ask_user", "arguments": json.dumps({"question": "选择哪类？", "options": ["文字", "表格"]})}}]}
            return normalize_response('openai_chat_completions', {"choices": [{"message": message, "finish_reason": 'tool_calls' if state['step'] == 0 else 'stop'}]}, f"msg-{state['step']}")
        # Open created the graph with its original override; rebuild with the test model.
        from ocr_workbench.agent.graph import GraphServices, build_graph
        first.model_override = ask
        first.graph = build_graph(GraphServices(first.agent, first.registry, ask, jobs=first.jobs), first.checkpoints.saver)
        first.schedule(self.run)
        await self.wait_status('waiting_user')
        old = self.store.rows("SELECT * FROM agent_decisions")[0]
        await first.close()
        second = await self.reopen(first)
        try:
            current = self.agent.run(self.project, self.run['id'])
            await second.resume_interrupted(self.project, current['id'], current['generation'])
            current = await self.wait_status('waiting_user')
            self.assertEqual(calls, [0])
            decisions = self.store.rows("SELECT * FROM agent_decisions WHERE status='pending'")
            self.assertEqual(len(decisions), 1)
            new = decisions[0]
            self.assertNotEqual(new['id'], old['id'])
            self.assertEqual(new['generation'], current['generation'])
            with self.assertRaises(Conflict):
                self.agent.reply_decision(self.project, current['id'], old['id'], {"client_request_id": "stale", "payload_hash": old['payload_hash'], "option_id": "option-0"})
            self.agent.reply_decision(self.project, current['id'], new['id'], {"client_request_id": "new", "payload_hash": new['payload_hash'], "option_id": "option-1"})
            second.schedule(current, resume=True)
            await self.wait_status('completed')
            self.assertEqual(calls, [0, 1])
        finally:
            await second.close()
