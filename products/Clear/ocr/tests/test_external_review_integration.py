"""External review persistence, queue isolation, migration, and adopted exports."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from test_external_review import ProviderFixture, KEY
from ocr_workbench import external_review as ext
from ocr_workbench.imaging import add_image
from ocr_workbench.multimodal_store import enqueue_review, view_review, prepare_review, decide_review
from ocr_workbench.multimodal_export import capture_report, build_review_report
from ocr_workbench.service import create_app
from ocr_workbench.store import Store, Conflict, history_decoded
from ocr_workbench.task_queue import TaskQueue, FusionQueue, ExternalReviewQueue


class ExternalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = ProviderFixture()

    @classmethod
    def tearDownClass(cls):
        cls.provider.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'; (self.bundle / 'config').mkdir(parents=True)
        (self.bundle / 'config/engines.json').write_text('{"ppocr":{"name":"OCR","models":[]}}', 'utf-8')
        self.store = Store(self.root / 'data')
        self.connection = ext.ExternalConnection(self.store, ext.CredentialVault(self.root / 'secrets'))
        self.provider.requests.clear(); self.provider.mode = 'normal'
        self.provider.entered.clear(); self.provider.release.clear()
        image = Image.new('RGB', (640, 240), 'white')
        stream = io.BytesIO(); image.save(stream, format='PNG')
        probe = patch.object(ext, 'make_probe', return_value=(stream.getvalue(), self.provider.probe))
        probe.start(); self.addCleanup(probe.stop)
        self.body = {'protocol': 'openai', 'base_url': self.provider.url, 'api_key': KEY, 'model': 'vision-a'}
        self.saved = self.connection.save(self.body)
        self.project = self.store.project('外部审校测试')
        path = self.root / 'page.png'; image.save(path)
        self.image = add_image(self.store, self.project['id'], 'page.png', path)
        self.version = self.image['active_version']
        task = self.store.enqueue(self.project['id'], [self.version], ['ppocr'])[0]
        self.store.claim()
        self.original = {'engine': 'ppocr', 'text': 'Amount 001.00', 'tables': [], 'blocks': [
            {'text': 'Amount 001.00', 'polygon': [[10, 10], [500, 10], [500, 80], [10, 80]], 'kind': 'text', 'confidence': .9}]}
        self.store.complete(task, self.original)
        self.result = self.store.one('tasks', task)['result_id']
        self.queue = ExternalReviewQueue(self.store, self.bundle, self.connection)
        self.local = TaskQueue(self.store, self.bundle)
        self.addCleanup(self.queue.stop); self.addCleanup(self.local.stop)

    def request(self, name='request-a'):
        return {'model_id': self.saved['model_id'], 'revision': self.store.result(self.result)['revision'],
                'version_id': self.version, 'scope': 'page', 'request_id': name}

    def enqueue(self, name='request-a'):
        config, _ = self.connection.resolve(self.saved['model_id'])
        return enqueue_review(self.store, self.result, self.request(name), config)

    def app(self, review_only=False, start_queue=False):
        app = create_app(self.bundle, self.store.root, 't' * 32, review_only=review_only, start_queue=start_queue)
        app.state.external_connection.vault = self.connection.vault
        return app

    def client(self, app):
        return TestClient(app, headers={'Authorization': 'Bearer ' + 't' * 32})

    def test_queue_claim_recovery_and_duplicate_submission_are_isolated(self):
        task = self.enqueue()
        self.assertEqual(task, self.enqueue())
        self.assertIsNone(self.store.claim())
        self.assertIsNone(self.store.claim(fusion=True))
        self.store.recover(fusion=False)
        self.assertEqual(self.store.one('tasks', task)['status'], 'queued')
        self.store.recover(external=True)
        self.assertEqual(self.store.one('tasks', task)['status'], 'paused')
        self.queue.action(self.project['id'], 'resume', [task])
        self.assertEqual(self.store.claim(external=True)['id'], task)
        self.store.recover(fusion=False)
        self.assertEqual(self.store.one('tasks', task)['status'], 'running')
        self.store.recover(external=True)
        self.assertEqual(self.store.one('tasks', task)['status'], 'interrupted')

    def test_review_while_local_gpu_lock_is_held_and_human_adoption_export_undo(self):
        task = self.enqueue()
        self.local.gpu_guard.acquire()
        self.local.unload = lambda: self.fail('remote review must not unload local models')
        try:
            thread = threading.Thread(target=self.queue.step); thread.start(); thread.join(8)
            self.assertFalse(thread.is_alive())
        finally:
            self.local.gpu_guard.release()
            self.local.unload = lambda: None
        self.assertEqual(self.store.one('tasks', task)['status'], 'succeeded')
        proposal = view_review(self.store, self.result)['proposals'][0]
        self.assertEqual(proposal['after'], 'Amount 001.05')
        self.assertEqual(self.store.result(self.result)['original']['text'], 'Amount 001.00')
        self.assertEqual(self.store.result(self.result)['edited']['text'], 'Amount 001.00')
        adopted = decide_review(self.store, self.result, proposal['id'], {
            'action': 'accept', 'revision': 0, 'version_id': self.version, 'request_id': 'accept-a'})
        self.assertEqual(adopted['edited']['text'], 'Amount 001.05')
        report = capture_report(self.store, self.result)
        self.assertEqual(report['proposals'][0]['state'], 'accepted')
        self.assertNotIn(KEY, json.dumps(report))
        for fmt in ['json', 'md', 'xlsx']:
            path = build_review_report(self.store, self.result, fmt)
            self.assertTrue(path.is_file())
        self.store.history(self.result, -1, adopted['revision'])
        self.assertEqual(self.store.result(self.result)['edited']['text'], 'Amount 001.00')
        for row in self.store.rows('SELECT snapshot,raw_response FROM multimodal_requests'):
            for data in row.values():
                self.assertNotIn(KEY, history_decoded(data))

    def test_connection_changed_before_claim_or_retry_sends_nothing(self):
        task = self.enqueue()
        self.connection.save({**self.body, 'model': 'vision-b'})
        self.provider.requests.clear()
        self.queue.step()
        self.assertEqual(self.store.one('tasks', task)['status'], 'failed')
        self.assertEqual(self.provider.requests, [])
        with self.assertRaisesRegex(ValueError, '连接已变化'):
            self.queue.action(self.project['id'], 'retry', [task])
        self.assertEqual(view_review(self.store, self.result)['proposals'], [])

    def test_inflight_response_keeps_original_connection_snapshot(self):
        task = self.enqueue()
        self.provider.requests.clear(); self.provider.entered.clear(); self.provider.mode = 'hold'
        worker = threading.Thread(target=self.queue.step); worker.start()
        self.assertTrue(self.provider.entered.wait(4))
        self.connection.clear()
        self.provider.release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.one('tasks', task)['status'], 'succeeded')
        snapshot = json.loads(history_decoded(self.store.rows('SELECT snapshot FROM multimodal_requests WHERE task_id=?', (task,))[0]['snapshot']))
        self.assertEqual(snapshot['config']['revision'], self.saved['revision'])
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(len(view_review(self.store, self.result)['proposals']), 1)

    def test_stale_result_and_cancelled_tasks_never_produce_proposals(self):
        task = self.enqueue()
        self.store.save(self.result, {'text': 'Changed', 'tables': []}, 0)
        self.provider.requests.clear()
        self.queue.step()
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(view_review(self.store, self.result)['proposals'], [])
        task2 = self.enqueue('second')
        self.queue.action(self.project['id'], 'cancel', [task2])
        self.assertFalse(self.queue.step())
        self.assertEqual(self.store.one('tasks', task2)['status'], 'cancelled')

    def test_bulk_queue_actions_cannot_cancel_another_workers_tasks(self):
        external = self.enqueue()
        local = self.store.enqueue(self.project['id'], [self.version], ['ppocr'])[0]
        self.assertEqual(self.queue.action(self.project['id'], 'cancel'), [external])
        self.assertEqual(self.store.one('tasks', local)['status'], 'queued')
        with self.assertRaises(ValueError):
            self.queue.action(self.project['id'], 'cancel', [local])
        self.assertEqual(self.local.action(self.project['id'], 'cancel'), [local])

    def test_review_only_api_catalog_save_clear_and_inference(self):
        app = self.app(review_only=True)
        client = self.client(app)
        self.provider.requests.clear()
        result = client.get('/api/multimodal/models')
        remote = next(m for m in result.json()['models'] if m.get('backend') == 'external')
        self.assertTrue(remote['available']); self.assertEqual(self.provider.requests, [])
        response = client.post(f'/api/results/{self.result}/multimodal', json=self.request())
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()['task_id']
        self.assertEqual(client.post(f'/api/results/{self.result}/multimodal', json=self.request()).json()['task_id'], task)
        app.state.external_queue.step()
        self.assertEqual(self.store.one('tasks', task)['status'], 'succeeded')
        self.assertEqual(client.get('/api/multimodal/external').json()['model'], 'vision-a')
        self.assertNotIn(KEY, client.get('/api/multimodal/external').text)
        self.assertEqual(client.delete('/api/multimodal/external').status_code, 200)
        self.assertFalse(client.get('/api/multimodal/external').json()['configured'])

    def test_api_models_error_and_failed_save_preserve_existing_configuration(self):
        client = self.client(self.app())
        models = client.post('/api/multimodal/external/models', json=self.body)
        self.assertEqual(models.status_code, 200)
        self.provider.mode = 'status:401'
        before = client.get('/api/multimodal/external').json()
        failed = client.put('/api/multimodal/external', json=self.body)
        self.assertEqual(failed.status_code, 400)
        self.assertNotIn(KEY, failed.text)
        self.assertEqual(client.get('/api/multimodal/external').json(), before)

    def test_project_queue_actions_in_review_only_and_cleanup_running_guard(self):
        app = self.app(review_only=True); client = self.client(app)
        task = self.enqueue()
        path = f'/api/projects/{self.project["id"]}/queue/'
        for action in ['pause', 'resume', 'cancel', 'retry']:
            response = client.post(path + action, json={'task_ids': [task]})
            self.assertEqual(response.status_code, 200, response.text)
        self.provider.mode = 'hold'; self.provider.entered.clear()
        worker = threading.Thread(target=app.state.external_queue.step); worker.start()
        self.assertTrue(self.provider.entered.wait(4))
        from ocr_workbench.maintenance import ProjectMaintenance
        maintenance = ProjectMaintenance(self.store, self.local, external_queue=app.state.external_queue)
        with self.assertRaisesRegex(ValueError, '任务仍在运行'):
            maintenance.delete(self.project['id'], self.project['name'])
        client.post(path + 'cancel', json={'task_ids': [task]})
        worker.join(3); self.assertFalse(worker.is_alive())
        self.provider.release.set()
        self.assertEqual(view_review(self.store, self.result)['proposals'], [])
        maintenance.delete(self.project['id'], self.project['name'])
        self.assertEqual(self.store.rows('SELECT * FROM projects'), [])

    def test_v11_migration_marks_old_review_local_and_backs_up(self):
        oldroot = self.root / 'legacy'
        with patch('ocr_workbench.store.SCHEMA_VERSION', 11):
            old = Store(oldroot)
        self.assertEqual(old.rows('PRAGMA user_version')[0]['user_version'], 11)
        upgraded = Store(oldroot)
        from ocr_workbench.store import SCHEMA_VERSION
        self.assertEqual(upgraded.rows('PRAGMA user_version')[0]['user_version'], SCHEMA_VERSION)
        self.assertTrue(upgraded.migration_backup.is_file())
        backend = next(c for c in upgraded.rows('PRAGMA table_info(multimodal_requests)') if c['name'] == 'backend')
        self.assertEqual(backend['dflt_value'], "'local'")

    def test_lifespan_starts_remote_queue_without_gpu_queue(self):
        app = self.app(review_only=True, start_queue=True)
        with self.client(app) as client:
            self.assertTrue(app.state.external_queue.status()['healthy'])
            self.assertFalse(app.state.queue.status()['alive'])
            response = client.post(f'/api/results/{self.result}/multimodal', json=self.request())
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()['task_id']
            import time
            deadline = time.monotonic() + 8
            while self.store.one('tasks', task)['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual(self.store.one('tasks', task)['status'], 'succeeded')
        self.assertFalse(app.state.external_queue.status()['alive'])


if __name__ == '__main__':
    unittest.main()
