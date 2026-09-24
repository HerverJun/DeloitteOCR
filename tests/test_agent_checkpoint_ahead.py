"""E68: real saver ahead of resume UI projection after an abrupt process exit."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import unittest

import httpx
import test_agent_recovery
from ocr_workbench.service import create_app


class CheckpointAheadTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_recovery.RecoveryTests.setUp
    runtime = test_agent_recovery.RecoveryTests.runtime
    wait_status = test_agent_recovery.RecoveryTests.wait_status
    config_revision = test_agent_recovery.RecoveryTests.config_revision
    reopen = test_agent_recovery.RecoveryTests.reopen

    async def test_crash_after_saver_update_before_resume_event_reprojects_once(self):
        first = await self.runtime()
        self.config_revision()
        try:
            first.schedule(self.run)
            await self.wait_status('waiting_jobs')
            if self.run['id'] in first.tasks:
                await first.tasks[self.run['id']]
            initial_requests = len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (self.run['id'],)))
            initial_stages = len(self.store.rows('SELECT * FROM document_stages'))
            self.assertEqual((initial_requests, initial_stages), (1, 1))
        finally:
            await first.close()
        current = self.agent.run(self.project, self.run['id'])
        self.assertEqual(current['status'], 'interrupted')
        worker = Path(__file__).parent / 'agent_fixtures' / 'checkpoint_ahead_worker.py'
        source = str(Path(__file__).parents[1] / 'src')
        process = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-c', 'import runpy,sys; sys.path.insert(0,sys.argv[1]); '
             'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
             source, str(worker), self.temp.name, self.project, current['id']],
            env={**os.environ, 'PYTHONPATH': source}, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 93, process.stdout + process.stderr)

        second = await self.reopen(first)
        try:
            current = self.agent.run(self.project, self.run['id'])
            config = {'configurable': {'thread_id': current['graph_thread_id']}}
            snapshot = await second.graph.aget_state(config)
            self.assertEqual(snapshot.values['generation'], current['generation'])
            self.assertEqual(current['status'], 'interrupted')
            self.assertFalse(snapshot.interrupts)
            self.assertIn('await_jobs', snapshot.next)
            event_key = f"{current['id']}:{current['generation']}:resume"
            self.assertFalse(self.store.rows('SELECT * FROM agent_events WHERE event_key=?', (event_key,)))
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (current['id'],))), initial_requests)
            self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), initial_stages)

            await second.resume_interrupted(self.project, current['id'], current['generation'])
            await self.wait_status('waiting_jobs')
            if current['id'] in second.tasks:
                await second.tasks[current['id']]
            events = self.agent.events(self.project, self.session['id'])
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_events WHERE event_key=?', (event_key,))), 1)
            self.assertEqual([e['seq'] for e in events], sorted({e['seq'] for e in events}))
            self.assertTrue(any(e['type'] == 'run_state' and e['payload']['status'] == 'waiting_jobs'
                                and e['generation'] == current['generation'] for e in events))
            self.assertEqual(self.agent.run(self.project, current['id'])['status'], 'waiting_jobs')
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests WHERE run_id=?', (current['id'],))), initial_requests)
            self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), initial_stages)
            with self.assertRaisesRegex(Exception, '只有重启中断'):
                await second.resume_interrupted(self.project, current['id'], current['generation'])
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_events WHERE event_key=?', (event_key,))), 1)
            # Query the authenticated UI endpoints against this reopened runtime.
            # ASGITransport does not enter a second lifespan or interrupt the run.
            bundle = Path(self.temp.name) / 'bundle'
            (bundle / 'config').mkdir(parents=True, exist_ok=True)
            (bundle / 'config' / 'engines.json').write_text('{}', encoding='utf-8')
            app = create_app(bundle, self.store.root, 'isolated-token',
                             start_queue=False, agent_enabled=True)
            app.state.agent_runtime = second
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                         headers={'Authorization': 'Bearer isolated-token'}) as client:
                snapshot_response = await client.get(f'/api/agent/sessions/{self.session["id"]}')
                self.assertEqual(snapshot_response.status_code, 200)
                visible = next(run for run in snapshot_response.json()['runs'] if run['id'] == current['id'])
                self.assertEqual(visible['status'], 'waiting_jobs')
                event_response = await client.get(f'/api/agent/sessions/{self.session["id"]}/events')
                self.assertEqual(event_response.status_code, 200)
                projected = event_response.json()['events']
                self.assertEqual(len([e for e in projected if e['type'] == 'run_state' and
                    e['generation'] == current['generation'] and e['payload']['status'] == 'queued']), 1)
                self.assertTrue(any(e['type'] == 'run_state' and e['payload']['status'] == 'waiting_jobs'
                                    and e['generation'] == current['generation'] for e in projected))
        finally:
            await second.close()
