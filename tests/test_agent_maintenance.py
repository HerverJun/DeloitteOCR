"""Project maintenance with the real AgentRuntime, SQLite saver and HTTP API."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from ocr_workbench.agent.checkpoints import Checkpoints
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.store import digest
from ocr_workbench.external_review import CredentialVault
from ocr_workbench.imaging import add_image
from ocr_workbench.service import create_app
from ocr_workbench.store import Conflict, now, uid


class AgentMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bundle = self.root / 'bundle'
        (bundle / 'config').mkdir(parents=True)
        (bundle / 'config/engines.json').write_text('{}', encoding='utf-8')
        self.entered, self.cancelled = set(), set()
        self.release_model = asyncio.Event()
        owner = self

        async def model(runtime, state, run):
            record, fresh = runtime.agent.begin_model_request(run['project_id'], run['id'], state['generation'], state['step'], digest({'goal': run['goal'], 'step': state['step']}))
            if not fresh:
                return json.loads(record['normalized_response'])
            owner.entered.add(run['id'])
            if run['goal'] == 'hold':
                try:
                    await owner.release_model.wait()
                except asyncio.CancelledError:
                    owner.cancelled.add(run['id'])
                    raise
            message = {'role': 'assistant', 'content': '合成维护测试'}
            if run['goal'] == 'ask' and state['step'] == 0:
                message = {'role': 'assistant', 'tool_calls': [{'id': 'clarification', 'type': 'function', 'function': {
                    'name': 'ask_user', 'arguments': json.dumps({'question': '请选择', 'options': ['继续', '停止']})}}]}
            elif run['goal'] == 'export' and state['step'] == 0:
                message = {'role': 'assistant', 'tool_calls': [{'id': 'export', 'type': 'function', 'function': {
                    'name': 'export_results', 'arguments': json.dumps(run['context']['export_args'])}}]}
            response = normalize_response('openai_chat_completions', {'choices': [{'message': message, 'finish_reason': 'tool_calls' if 'tool_calls' in message else 'stop'}]}, record['request_id'])
            runtime.agent.finish_model_request(run['project_id'], run['id'], state['generation'], record['request_id'], response)
            return response

        runtime_open = AgentRuntime.open

        async def open_with_fixture(runtime):
            runtime.model_override = model.__get__(runtime, AgentRuntime)
            return await runtime_open(runtime)

        self.model_patch = patch.object(AgentRuntime, 'open', open_with_fixture)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.app = create_app(bundle, self.root / 'workspace', 'synthetic-maintenance-token', start_queue=False, agent_enabled=True)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.runtime = self.app.state.agent_runtime
        self.assertIsNotNone(self.runtime)
        self.store, self.agent = self.app.state.store, self.runtime.agent
        self.maintenance = self.app.state.application_services.maintenance
        self.project = self.store.project('待清理项目')['id']
        self.other = self.store.project('保留项目')['id']
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver', headers={'Authorization': 'Bearer synthetic-maintenance-token'})
        self.addAsyncCleanup(self.client.aclose)

    async def new_run(self, project=None, goal='ask'):
        project = project or self.project
        session = self.agent.create_session(project, {'client_request_id': uid(), 'title': '会话'})
        run = self.agent.create_run(project, session['id'], {'client_request_id': uid(), 'content': goal}, context={}, config={}, limits={})
        self.runtime.schedule(run)
        for _ in range(150):
            current = self.agent.run(project, run['id'])
            if (goal == 'hold' and run['id'] in self.entered) or (goal == 'ask' and current['status'] == 'waiting_user'):
                return current
            self.assertNotEqual(current['status'], 'failed', self.agent.events(project, session['id']))
            await asyncio.sleep(.02)
        self.fail('Synthetic graph did not reach its required state')

    def photo(self, project):
        source = self.root / (uid() + '.png')
        Image.new('RGB', (3, 3), 'white').save(source)
        return add_image(self.store, project, 'synthetic.png', source)

    def artifact(self, run, *, key=None):
        key, operation = key or uid(), uid()
        folder = self.runtime.artifacts.directory(run['project_id'], key)
        folder.mkdir(parents=True, exist_ok=True)
        content = '财务测试产物'.encode('utf-8')
        (folder / 'export.txt').write_bytes(content)
        manifest = {'results': [], 'coverage': {'failed_pages': []}, 'format': 'txt'}
        checksum = hashlib.sha256(content).hexdigest()
        (folder / 'manifest.json').write_text(json.dumps({'artifact_id': key, 'project_id': run['project_id'], 'file': 'export.txt', 'bytes': len(content), 'sha256': checksum, 'mime': 'text/plain', 'manifest': manifest}), encoding='utf-8')
        with self.store.transaction() as db:
            db.execute("INSERT INTO agent_operations VALUES(?,?,?,?,?,?,'finished',?,NULL,?,?)", (operation, run['project_id'], run['id'], operation, 'synthetic-hash', run['generation'], json.dumps({'artifact_id': key}), now(), now()))
            db.execute("INSERT INTO agent_artifacts VALUES(?,?,?,?,?,?,?,?,?,'ready',?,0,?)", (key, run['project_id'], run['id'], operation, (folder / 'export.txt').relative_to(self.store.root).as_posix(), checksum, len(content), 'text/plain', json.dumps(manifest), time.time() + 3600, now()))
        return key, folder, operation

    async def delete(self, confirmation='待清理项目'):
        return await self.client.request('DELETE', f'/api/projects/{self.project}', json={'confirmation': confirmation})

    async def test_usage_counts_project_agent_payload_and_artifacts_without_other_project(self):
        run = await self.new_run()
        other = await self.new_run(self.other)
        self.artifact(run)
        _, _, operation = self.artifact(other)
        before = (await self.client.get(f'/api/projects/{self.project}/storage')).json()
        self.assertEqual(before['agent_sessions'], 1)
        self.assertEqual(before['agent_artifacts'], 1)
        self.assertGreater(before['agent_tables']['agent_events']['bytes'], 0)
        self.assertEqual(before['agent_tables']['agent_decisions']['rows'], 1)
        self.assertEqual(before['agent_database_payload_bytes'], sum(row['bytes'] for row in before['agent_tables'].values()))
        self.assertEqual(before['bytes'], before['file_bytes'] + before['database_payload_bytes'])
        with self.store.transaction() as db:
            db.execute('UPDATE agent_operations SET result=? WHERE id=?', ('其他项目大文本' * 10000, operation))
        after = self.maintenance.usage(self.project)
        self.assertEqual(before['agent_database_payload_bytes'], after['agent_database_payload_bytes'])
        with self.store.transaction() as db:
            own = db.execute('SELECT id,result FROM agent_operations WHERE project_id=?', (self.project,)).fetchone()
            replacement = '本项目新增文本' * 100
            delta = len(replacement.encode('utf-8')) - len(own['result'].encode('utf-8'))
            db.execute('UPDATE agent_operations SET result=? WHERE id=?', (replacement, own['id']))
        self.assertEqual(self.maintenance.usage(self.project)['agent_database_payload_bytes'], before['agent_database_payload_bytes'] + delta)

    async def test_live_project_delete_cancels_only_its_graph_and_cleans_saver(self):
        run, other = await self.new_run(goal='hold'), await self.new_run(self.other, 'hold')
        self.photo(self.project)
        other_photo = self.photo(self.other)
        _, own_folder, _ = self.artifact(run)
        _, other_folder, _ = self.artifact(other)
        vault = CredentialVault(self.root / 'shared-synthetic-credentials')
        reference = vault.save('synthetic-maintenance-key')
        with self.store.transaction() as db:
            db.execute("INSERT INTO settings VALUES('agent_controller_connection',?)", (json.dumps({'credential_ref': reference, 'revision': 1}),))
        # Other project's model is intentionally still waiting and holds its saver
        # activity scope; per-project cleanup must not await global quiescence.
        response = await asyncio.wait_for(self.delete(), 3)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['checkpoint_cleanup_pending'])
        self.assertIn(run['id'], self.cancelled)
        self.assertNotIn(other['id'], self.cancelled)
        self.assertEqual(self.agent.run(self.other, other['id'])['status'], 'running')
        self.assertFalse(own_folder.exists())
        self.assertTrue(other_folder.exists())
        self.assertTrue(self.store.file(other_photo['original_path']).exists())
        self.assertEqual(vault.read(reference), 'synthetic-maintenance-key')
        self.assertEqual(len(self.store.rows("SELECT * FROM settings WHERE key='agent_controller_connection'")), 1)
        self.assertFalse(self.store.rows('SELECT * FROM agent_checkpoint_cleanup'))
        snapshot = await self.runtime.graph.aget_state({'configurable': {'thread_id': run['graph_thread_id']}})
        self.assertFalse(snapshot.values)
        self.assertTrue((await self.runtime.graph.aget_state({'configurable': {'thread_id': other['graph_thread_id']}})).values)
        await self.runtime._observe_once()
        self.assertFalse(self.runtime.observer.done())

    async def test_wrong_confirmation_does_not_stop_run(self):
        run = await self.new_run(goal='hold')
        response = await self.delete('错名')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(run['id'], self.cancelled)
        self.assertEqual(self.agent.run(self.project, run['id'])['status'], 'running')
        self.assertNotIn(self.project, self.runtime.maintenance_projects)

    async def test_queue_protection_stops_agent_but_preserves_reused_and_other_tasks(self):
        run = await self.new_run()
        photo, other_photo = self.photo(self.project), self.photo(self.other)
        task = self.store.enqueue(self.project, [photo['active_version']], ['glm'])[0]
        other_task = self.store.enqueue(self.other, [other_photo['active_version']], ['glm'])[0]
        _, folder, operation = self.artifact(run)
        with self.store.transaction() as db:
            db.execute("INSERT INTO agent_job_links VALUES(?,'ocr',?,'reused',NULL,'queued')", (operation, task))
        response = await self.delete()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('等待任务', response.text)
        self.assertEqual(self.agent.run(self.project, run['id'])['status'], 'cancelled')
        self.assertEqual(self.store.one('tasks', task)['status'], 'queued')
        self.assertEqual(self.store.one('tasks', other_task)['status'], 'queued')
        self.assertTrue(folder.exists())
        self.assertTrue(self.store.one('projects', self.project))
        self.assertFalse(self.store.rows('SELECT * FROM agent_checkpoint_cleanup'))
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task,))
        self.assertEqual((await self.delete()).status_code, 200)
        self.assertEqual(self.store.one('tasks', other_task)['status'], 'queued')

    async def test_document_stage_and_active_worker_still_protect_files(self):
        run = await self.new_run()
        photo = self.photo(self.project)
        self.app.state.documents.process_pages([photo['id']], 'native')
        stages = self.store.rows('SELECT * FROM document_stages')
        response = await self.delete()
        self.assertEqual(response.status_code, 400)
        self.assertIn('文档处理任务', response.text)
        self.assertEqual(self.agent.run(self.project, run['id'])['status'], 'cancelled')
        self.assertEqual(self.store.rows('SELECT * FROM document_stages')[0]['status'], stages[0]['status'])
        with self.store.transaction() as db:
            db.execute("UPDATE document_stages SET status='cancelled'")
        task = self.store.enqueue(self.project, [photo['active_version']], ['glm'])[0]
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task,))
        with patch.object(self.app.state.queue, 'status', return_value={'task_id': task}):
            response = await self.delete()
        self.assertEqual(response.status_code, 400)
        self.assertIn('引擎退出', response.text)
        self.assertTrue(self.store.file(photo['original_path']).exists())

    async def test_active_download_preserves_artifact_until_lease_released(self):
        run = await self.new_run()
        key, folder, _ = self.artifact(run)
        lease, _ = self.runtime.artifacts.lease(self.project, key)
        response = await self.delete()
        self.assertEqual(response.status_code, 400)
        self.assertIn('正在下载', response.text)
        self.assertTrue(folder.exists())
        self.runtime.artifacts.release(lease)
        self.assertEqual((await self.delete()).status_code, 200)
        self.assertFalse(folder.exists())

    async def test_delete_fences_real_export_worker_and_waits_for_its_file_guard(self):
        photo = self.photo(self.project)
        task = self.store.enqueue(self.project, [photo['active_version']], ['glm'])[0]
        self.store.claim()
        self.store.complete(task, {'text': '实际导出与删除并发', 'tables': [], 'engine': 'glm'})
        result = self.store.one('tasks', task)['result_id']
        session = self.agent.create_session(self.project, {'client_request_id': uid(), 'title': '导出'})
        args = {'source': 'explicit_results', 'format': 'txt', 'results': [{'result_id': result, 'revision': 0, 'version_id': photo['active_version']}]}
        run = self.agent.create_run(self.project, session['id'], {'client_request_id': uid(), 'content': 'export'}, context={'export_args': args}, config={}, limits={})
        self.runtime.policy.grant(self.project, 'scoped_new_artifact', {'document_ids': [photo['id']], 'page_ids': [photo['id']],
            'version_ids': [photo['active_version']], 'result_ids': [result], 'format': 'txt', 'partial_policy': 'ask'},
            source='user_request', expires=time.time() + 60, run_id=run['id'])
        entered, release, rejected = threading.Event(), threading.Event(), threading.Event()
        original_export = self.runtime.services.export
        original_artifact = self.runtime.artifacts.export

        def held_export(body):
            generated = original_export(body)
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Synthetic export publication was not released')
            return generated

        def checked_export(*values, **kwargs):
            try:
                return original_artifact(*values, **kwargs)
            except Conflict:
                rejected.set()
                raise

        deletion = None
        with patch.object(self.runtime.services, 'export', side_effect=held_export), patch.object(self.runtime.artifacts, 'export', side_effect=checked_export):
            try:
                self.runtime.schedule(run)
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                deletion = asyncio.create_task(self.delete())
                for _ in range(100):
                    if self.agent.run(self.project, run['id'])['status'] == 'cancelled':
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(self.agent.run(self.project, run['id'])['status'], 'cancelled')
                self.assertFalse(deletion.done())
                release.set()
                response = await asyncio.wait_for(deletion, 3)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(rejected.is_set())
                self.assertFalse((self.store.root / 'agent-artifacts' / self.project).exists())
                self.assertFalse(self.store.rows('SELECT * FROM agent_artifacts WHERE project_id=?', (self.project,)))
            finally:
                release.set()
                if deletion is not None:
                    await asyncio.gather(deletion, return_exceptions=True)

    async def test_saver_failure_retains_tombstone_and_retries_without_undeleting_project(self):
        run = await self.new_run()
        with patch.object(self.runtime.checkpoints.saver, 'adelete_thread', side_effect=OSError('synthetic saver busy')):
            with self.assertLogs('ocr_workbench.agent.runtime', level='ERROR'):
                response = await self.delete()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['checkpoint_cleanup_pending'])
        self.assertFalse(self.store.rows('SELECT * FROM projects WHERE id=?', (self.project,)))
        self.assertEqual(self.store.rows('SELECT * FROM agent_checkpoint_cleanup')[0]['thread_id'], run['graph_thread_id'])
        await self.runtime.checkpoints.cleanup_project_threads(self.project)
        self.assertFalse(self.store.rows('SELECT * FROM agent_checkpoint_cleanup'))
        self.assertFalse((await self.runtime.graph.aget_state({'configurable': {'thread_id': run['graph_thread_id']}})).values)

    async def test_disabled_runtime_stops_interrupted_runs_and_restart_cleans_tombstone(self):
        run = await self.new_run()
        await self.runtime.close()
        self.app.state.agent_runtime = None
        self.assertEqual(self.agent.run(self.project, run['id'])['status'], 'interrupted')
        response = await self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['checkpoint_cleanup_pending'])
        checkpoints = await Checkpoints(self.store).open()
        try:
            self.assertFalse(self.store.rows('SELECT * FROM agent_checkpoint_cleanup'))
            self.assertIsNone(await checkpoints.saver.aget_tuple({'configurable': {'thread_id': run['graph_thread_id']}}))
        finally:
            await checkpoints.close()

    async def test_orphan_inventory_quarantine_preserves_registered_and_active_staging(self):
        run = await self.new_run()
        key, folder, _ = self.artifact(run)
        orphan = folder.parent / uid()
        orphan.mkdir()
        (orphan / 'export.txt').write_text('orphan', encoding='utf-8')
        staging = folder.parent / (key + '.staging-' + uid())
        staging.mkdir()
        (staging / 'export.txt').write_text('in progress', encoding='utf-8')
        report = self.maintenance.orphans()
        orphan_relative = orphan.relative_to(self.store.root).as_posix()
        self.assertEqual([entry['path'] for entry in report['files']], [orphan_relative])
        with self.assertRaises(ValueError):
            self.maintenance.quarantine_orphans([folder.relative_to(self.store.root).as_posix()])
        receipt = self.maintenance.quarantine_orphans([orphan_relative])
        self.assertEqual((Path(receipt['directory']) / '0/export.txt').read_text('utf-8'), 'orphan')
        self.assertTrue(folder.exists())
        self.assertTrue(staging.exists())
        await self.runtime.cancel(self.project, run['id'], run['generation'], 'stop_agent')
        self.assertEqual([entry['path'] for entry in self.maintenance.orphans()['files']], [staging.relative_to(self.store.root).as_posix()])

    async def test_maintenance_gate_rejects_new_graph_without_stopping_other_project(self):
        run, other = await self.new_run(goal='hold'), await self.new_run(self.other, 'hold')
        self.runtime.maintenance_projects.add(self.project)
        try:
            with self.assertRaises(Conflict):
                self.runtime.schedule(run)
            await self.runtime._observe_once()
            self.assertFalse(self.runtime.tasks[other['id']].done())
        finally:
            self.runtime.maintenance_projects.discard(self.project)


if __name__ == '__main__':
    unittest.main()
