"""Corrupt and foreign checkpoints must not replay tasks or disable local OCR."""
import asyncio
import hashlib
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import httpx
import test_agent_recovery
from ocr_workbench.service import create_app
from ocr_workbench.store import Conflict
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.store import digest


class CheckpointFaultTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_recovery.RecoveryTests.setUp
    runtime = test_agent_recovery.RecoveryTests.runtime
    wait_status = test_agent_recovery.RecoveryTests.wait_status
    config_revision = test_agent_recovery.RecoveryTests.config_revision
    reopen = test_agent_recovery.RecoveryTests.reopen

    async def test_resume_call_commit_before_graph_checkpoint_replays_exact_pair_once(self):
        # An actual child process consumes Command(resume), then exits inside
        # await_jobs after its business transaction, before the saver writes.
        with self.store.transaction() as db:
            db.execute("UPDATE agent_runs SET status='cancelled' WHERE id=?", (self.run['id'],))
        root = Path(self.temp.name)
        worker = Path(__file__).parent / 'agent_fixtures' / 'resume_fault_worker.py'
        source = str(Path(__file__).parents[1] / 'src')
        process = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-c', 'import runpy,sys; sys.path.insert(0,sys.argv[1]); '
             'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
             source, str(worker), str(root), self.project, self.session['id'], self.photo['id']],
            env={**os.environ, 'PYTHONPATH': source}, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 92, process.stdout + process.stderr)
        self.run = self.agent.run(self.project, self.store.rows(
            "SELECT id FROM agent_runs WHERE client_request_id='resume-fault-window'")[0]['id'])
        call = self.store.rows('SELECT * FROM agent_calls WHERE run_id=?', (self.run['id'],))[0]
        self.assertEqual(call['state'], 'finished')
        self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
        before_events = self.store.rows("SELECT * FROM agent_events WHERE event_key=?", (call['id'] + ':finished',))
        self.assertEqual(len(before_events), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (self.run['id'],))), 1)

        from ocr_workbench.application_services import ApplicationServices
        from ocr_workbench.documents import Documents
        seen = []
        async def model(state, run):
            seen.append(state['step'])
            self.assertEqual(state['step'], 1)
            message = {'role': 'assistant', 'content': '已核对后台任务状态。'}
            record, fresh = self.agent.begin_model_request(self.project, run['id'], state['generation'],
                                                             state['step'], digest(message))
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
            snapshot = await runtime.graph.aget_state({'configurable': {'thread_id': current['graph_thread_id']}})
            self.assertEqual((snapshot.values['step'], snapshot.values['cursor']), (0, 0))
            self.assertIn('await_jobs', snapshot.next)
            self.assertEqual(snapshot.interrupts[0].value['kind'], 'jobs')
            await runtime.resume_interrupted(self.project, current['id'], current['generation'])
            await self.wait_status('completed')
            if current['id'] in runtime.tasks:
                await runtime.tasks[current['id']]
            self.assertEqual(seen, [1])
            self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_calls WHERE run_id=?', (current['id'],))), 1)
            self.assertEqual(self.store.rows("SELECT * FROM agent_events WHERE event_key=?", (call['id'] + ':finished',)), before_events)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (current['id'],))), 2)
            self.assertEqual(len([event for event in self.agent.events(self.project, self.session['id'])
                                  if event['type'] == 'tool_finished']), 1)
        finally:
            await runtime.close()

    async def test_corrupt_pending_state_refuses_explicit_resume_without_replay(self):
        first = await self.runtime()
        self.config_revision()
        first.schedule(self.run)
        await self.wait_status('waiting_jobs')
        await first.close()
        second = await self.reopen(first)
        try:
            current = self.agent.run(self.project, self.run['id'])
            before_stages = self.store.rows('SELECT * FROM document_stages')
            before_requests = self.store.rows('SELECT * FROM agent_model_requests')
            await second.checkpoints.conn.execute('UPDATE checkpoints SET checkpoint=? WHERE thread_id=?', (b'\xc1', current['graph_thread_id']))
            await second.checkpoints.conn.commit()
            with self.assertRaisesRegex(Conflict, '检查点'):
                await second.resume_interrupted(self.project, current['id'], current['generation'])
            self.assertEqual(self.agent.run(self.project, current['id'])['status'], 'interrupted')
            self.assertEqual(self.store.rows('SELECT * FROM document_stages'), before_stages)
            self.assertEqual(self.store.rows('SELECT * FROM agent_model_requests'), before_requests)
            self.assertFalse(second.tasks)
        finally:
            await second.close()

    async def test_foreign_run_in_checkpoint_is_rejected_before_business_resume(self):
        first = await self.runtime()
        self.config_revision()
        first.schedule(self.run)
        await self.wait_status('waiting_jobs')
        await first.close()
        second = await self.reopen(first)
        try:
            current = self.agent.run(self.project, self.run['id'])
            config = {'configurable': {'thread_id': current['graph_thread_id']}}
            await second.graph.aupdate_state(config, {'run_id': 'foreign-run', 'project_id': self.other})
            before = self.store.rows('SELECT * FROM document_stages')
            with self.assertRaisesRegex(Conflict, '检查点'):
                await second.resume_interrupted(self.project, current['id'], current['generation'])
            self.assertEqual(self.store.rows('SELECT * FROM document_stages'), before)
            self.assertEqual(self.agent.run(self.project, current['id'])['status'], 'interrupted')
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), 1)
        finally:
            await second.close()

    async def test_corrupt_sqlite_disables_agent_but_retains_authenticated_local_project_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bundle'
            (bundle / 'config').mkdir(parents=True)
            (bundle / 'config/engines.json').write_text('{}', 'utf-8')
            app = create_app(bundle, root / 'workspace', 'isolated-checkpoint-test', start_queue=False, agent_enabled=True)
            project = app.state.store.project('Retained project')
            checkpoint = app.state.store.root / 'agent-checkpoints.sqlite3'
            checkpoint.write_bytes(b'Not a SQLite database\x00preserve forensic evidence')
            original = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            async with app.router.lifespan_context(app):
                self.assertIsNone(app.state.agent_runtime)
                self.assertIn('检查点', app.state.agent_startup_error)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1', headers={'Authorization': 'Bearer isolated-checkpoint-test'}) as client:
                    response = await client.get(f'/api/projects/{project["id"]}')
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(project['id'], response.text)
                    denied = await client.post(f'/api/projects/{project["id"]}/agent/sessions', json={'client_request_id': 'session', 'title': 'refuse'})
                    self.assertEqual(denied.status_code, 503)
            self.assertEqual(hashlib.sha256(checkpoint.read_bytes()).hexdigest(), original)
