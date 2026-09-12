"""Direct FastAPI route regressions; no server, browser or OCR models."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from ocr_workbench.imaging import add_image
from ocr_workbench.service import create_app
from ocr_workbench.store import Conflict


class ServiceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        (self.bundle / 'config').mkdir(parents=True)
        (self.bundle / 'config/engines.json').write_text(json.dumps({
            'glm': {'name': 'Synthetic', 'models': [], 'capabilities': {'tables': True}}
        }), 'utf-8')
        self.app = create_app(self.bundle, self.root / 'data', 't' * 32, start_queue=False)
        self.store = self.app.state.store
        self.project = self.store.project('服务回归')
        source = self.root / 'image.png'
        Image.new('RGB', (3, 3), 'white').save(source)
        self.photo = add_image(self.store, self.project['id'], 'image.png', source)
        task = self.store.enqueue(self.project['id'], [self.photo['active_version']], ['glm'])[0]
        self.store.claim()
        self.store.complete(task, {'text': '0001', 'tables': [], 'blocks': [], 'engine': 'glm'})
        self.result_id = self.store.one('tasks', task)['result_id']

    def route(self, path, method='GET', app=None):
        return next(route.endpoint for route in (app or self.app).routes
                    if route.path == path and method in route.methods)

    def test_health_reports_worker_failure_and_review_mode_readiness(self):
        health = self.route('/api/health')
        with patch.object(self.app.state.queue, 'status', return_value={
                'healthy': False, 'alive': False, 'state': 'failed', 'last_error': {'message': 'locked'}}):
            response = health()
        self.assertEqual(response['status'], 'degraded')
        self.assertEqual(response['queue']['last_error']['message'], 'locked')
        review = create_app(self.bundle, self.root / 'review', 't' * 32,
                            start_queue=False, review_only=True)
        response = self.route('/api/health', app=review)()
        self.assertEqual(response['status'], 'ready')
        self.assertTrue(response['review_only'])

    def test_review_mode_rejects_recognition_and_activation_routes(self):
        app = create_app(self.bundle, self.root / 'review', 't' * 32,
                         start_queue=False, review_only=True)
        cases = [
            ('/api/projects/{key}/tasks', ('missing', {})),
            ('/api/versions/{key}/transform', ('missing', {'kind': 'dewarp'})),
            ('/api/projects/{key}/queue/{action}', ('missing', 'resume', {})),
            ('/api/projects/{key}/queue/{action}', ('missing', 'retry', {})),
            ('/api/engine-packages/activate', ({},)),
            ('/api/engine-packages/switch', ({},)),
            ('/api/queue/recover', ()),
        ]
        for path, args in cases:
            with self.subTest(path=path, args=args), self.assertRaisesRegex(ValueError, '仅校对'):
                self.route(path, 'POST', app)(*args)

    def test_review_mode_keeps_existing_result_edit_and_export_available(self):
        app = create_app(self.bundle, self.store.root, 't' * 32,
                         start_queue=False, review_only=True)
        saved = self.route('/api/results/{key}', 'PUT', app)(self.result_id,
            {'revision': 0, 'edited': {'text': '校对 0001', 'tables': []}})
        self.assertEqual(saved['revision'], 1)
        response = self.route('/api/export', 'POST', app)(
            {'result_ids': [self.result_id], 'format': 'txt'})
        self.assertEqual(Path(response.path).read_text('utf-8'), '校对 0001')
        asyncio.run(response.background())

    def test_recovery_route_requests_recovery_and_restarts_only_dead_worker(self):
        app = create_app(self.bundle, self.root / 'recover', 't' * 32, start_queue=True)
        queue = app.state.queue
        recover = self.route('/api/queue/recover', 'POST', app)
        with patch.object(queue, 'status', return_value={'alive': False}), \
                patch.object(queue, 'start') as start, \
                patch.object(queue, 'recover_worker', return_value={'state': 'running'}) as retry:
            self.assertEqual(recover(), {'state': 'running'})
            start.assert_called_once()
            retry.assert_called_once()
        with patch.object(queue, 'status', return_value={'alive': True}), \
                patch.object(queue, 'start') as start, \
                patch.object(queue, 'recover_worker') as retry:
            recover()
            start.assert_not_called()
            retry.assert_called_once()
        with self.assertRaisesRegex(ValueError, '未启用'):
            self.route('/api/queue/recover', 'POST')()

    def test_project_revision_shortcut_and_review_invalidation(self):
        project = self.route('/api/projects/{key}')
        snapshot = project(self.project['id'])
        self.assertEqual(len(snapshot['images']), 1)
        unchanged = project(self.project['id'], since_revision=snapshot['revision'])
        self.assertTrue(unchanged['unchanged'])
        self.assertNotIn('images', unchanged)
        review = self.route('/api/images/{key}/review', 'PUT')
        review(self.photo['id'], {'status': 'confirmed', 'result_id': self.result_id,
               'revision': 0, 'version_id': self.photo['active_version']})
        confirmed = project(self.project['id'], since_revision=snapshot['revision'])
        self.assertGreater(confirmed['revision'], snapshot['revision'])
        self.assertEqual(confirmed['images'][0]['review_status'], 'confirmed')
        self.route('/api/results/{key}', 'PUT')(self.result_id,
            {'revision': 0, 'edited': {'text': 'changed externally', 'tables': []}})
        latest = project(self.project['id'], since_revision=confirmed['revision'])
        self.assertGreater(latest['revision'], confirmed['revision'])
        self.assertEqual(latest['images'][0]['review_status'], 'pending')
        with self.assertRaises(Conflict):
            review(self.photo['id'], {'status': 'confirmed', 'result_id': self.result_id,
                   'revision': 0, 'version_id': self.photo['active_version']})

    def test_confirmed_export_route_delivers_checked_content_and_cleans_up(self):
        export = self.route('/api/export', 'POST')
        body = {'result_ids': [self.result_id], 'format': 'json', 'confirmed_only': True}
        with self.assertRaises(Conflict):
            export(body)
        self.route('/api/images/{key}/review', 'PUT')(self.photo['id'], {
            'status': 'confirmed', 'result_id': self.result_id,
            'revision': 0, 'version_id': self.photo['active_version']})
        response = export(body)
        path = Path(response.path)
        result = json.loads(path.read_text('utf-8'))
        self.assertEqual(result['id'], self.result_id)
        self.assertEqual(result['revision'], 0)
        self.assertEqual(result['edited']['text'], '0001')
        asyncio.run(response.background())
        self.assertFalse(path.parent.exists())

    def test_project_snapshot_keeps_revision_review_and_names_in_one_database_view(self):
        from ocr_workbench.store import Store

        self.store.set_review(self.photo['id'], 'confirmed', self.result_id, 0,
                              self.photo['active_version'])
        before = self.store.project_revision(self.project['id'])
        other = Store(self.store.root)
        review_states = self.store._review_states
        changed = False

        def change_after_reading_review(db, project_id):
            nonlocal changed
            reviews = review_states(db, project_id)
            if not changed:
                changed = True
                other.save(self.result_id, {'text': 'unreviewed external edit', 'tables': []}, 0)
                with other.transaction() as writer:
                    writer.execute('UPDATE images SET name=? WHERE id=?',
                                   ('renamed-after-edit.png', self.photo['id']))
            return reviews

        with patch.object(self.store, '_review_states', change_after_reading_review):
            snapshot = self.route('/api/projects/{key}')(self.project['id'])
        self.assertTrue(changed)
        self.assertEqual(snapshot['revision'], before)
        self.assertEqual(snapshot['images'][0]['name'], 'image.png')
        self.assertEqual(snapshot['images'][0]['review_status'], 'confirmed')
        refreshed = self.route('/api/projects/{key}')(self.project['id'], since_revision=before)
        self.assertGreater(refreshed['revision'], before)
        self.assertEqual(refreshed['images'][0]['name'], 'renamed-after-edit.png')
        self.assertEqual(refreshed['images'][0]['review_status'], 'pending')


if __name__ == '__main__':
    unittest.main()
