"""Exact queued duplicates share computation, never image/result identity."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import shutil
import unittest
import zipfile

from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches
from ocr_workbench.store import Store


class ScreenReuseTests(unittest.TestCase):
    def setUp(self):
        module = runpy.run_path(str(Path(__file__).resolve().with_name('test_screen_batches.py')))
        self.fixture = module['ScreenBatchTests']()
        self.fixture.setUp()
        self.store, self.manager = self.fixture.store, self.fixture.manager
        self.table = deepcopy(module['TABLE'])

    def tearDown(self):
        self.fixture.tearDown()

    def duplicates(self, count=2):
        versions = self.fixture.images(1)
        source = self.store.file(self.store.one('versions', versions[0])['path'])
        for index in range(1, count):
            copied = self.fixture.root / f'duplicate-{index}.png'
            shutil.copyfile(source, copied)
            image = add_image(self.store, self.fixture.project, '相同图片.png', copied)
            versions.append(image['active_version'])
        return versions

    def complete(self, task):
        raw = {'status': 'success', 'text': '业务数据', 'tables': [deepcopy(self.table)],
               'blocks': [], 'engine': task['engine'], 'elapsed_seconds': .1,
               'project_image_version': task['version_id'], 'screen_input': {'old_source_marker': task['id']}}
        self.assertTrue(self.store.complete(task['id'], raw))

    def context_success(self, task):
        raw = {'status': 'success', 'component': 'actual-mock-context',
               'image_version': task['version_id'], 'image': {'width': 100, 'height': 70},
               'inference_seconds': 2.5, 'load_seconds': 3,
               'layout_blocks': [{'label': 'table', 'box': [0, 0, 100, 70], 'confidence': .99}],
               'ocr_blocks': []}
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='succeeded',finished=datetime('now') WHERE id=?", (task['id'],))
            db.execute('UPDATE screen_contexts SET raw=? WHERE task_id=?', (json.dumps(raw), task['id']))
        return raw

    def test_queued_duplicate_runs_once_with_own_coordinate_map_and_two_excel_files(self):
        versions = self.duplicates()
        original_prepare = self.manager.prepare_override
        def transformed(source, folder, **kwargs):
            prepared = original_prepare(source, folder, **kwargs)
            if versions[1] in str(folder):
                prepared['prepared_images'][0]['prepared_to_source'][2] = 10
            return prepared
        self.manager.prepare_override = transformed
        self.table['cells'][0]['box'] = [0, 0, 20, 10]
        batch = self.fixture.submit(versions)
        self.manager.step()
        tasks = self.store.rows('SELECT * FROM tasks')
        self.assertEqual(sum(task['kind'] == 'screen_cached' for task in tasks), 1)
        source = self.store.claim()
        self.assertNotEqual(source['kind'], 'screen_cached')
        self.assertIsNone(self.store.claim())
        self.complete(source)
        self.manager.step()
        results = [self.manager.detail(self.fixture.project, batch, version)['result'] for version in versions]
        self.assertNotEqual(results[0]['id'], results[1]['id'])
        self.assertEqual([result['original']['project_image_version'] for result in results], versions)
        self.assertEqual(results[0]['edited']['tables'][0]['cells'][0]['box'], [0, 0, 20, 10])
        self.assertEqual(results[1]['edited']['tables'][0]['cells'][0]['box'], [10, 0, 30, 10])
        cached_task = self.store.rows("SELECT * FROM tasks WHERE kind='screen_cached'")[0]
        cached = self.store.result(cached_task['result_id'])['original']
        self.assertEqual(cached['screen_input']['input_version_id'], versions[1])
        self.assertEqual(cached['cache_hit']['source_task_id'], source['id'])
        self.assertEqual(results[1]['original']['screen']['sources'][0]['cache_hit']['source_task_id'], source['id'])
        self.assertNotIn('old_source_marker', cached['screen_input'])
        path, manifest = self.manager.export(self.fixture.project, batch)
        self.assertEqual(manifest['success_files'], 2)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(len([name for name in archive.namelist() if name.endswith('.xlsx')]), 2)

    def test_cancelled_source_promotes_one_waiter_and_other_waiter_retargets(self):
        versions = self.duplicates(3)
        batch = self.fixture.submit(versions)
        self.manager.step()
        self.manager.action(self.fixture.project, batch, {'action': 'cancel', 'version_ids': [versions[0]]})
        self.manager.step()
        task = self.store.claim()
        self.assertIn(task['version_id'], versions[1:])
        self.assertNotEqual(task['kind'], 'screen_cached')
        self.assertIsNone(self.store.claim())
        self.complete(task)
        self.manager.step()
        counts = self.manager.get(self.fixture.project, batch)['counts']
        self.assertEqual((counts['completed'], counts['cancelled'], counts['pending']), (2, 1, 0))
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks WHERE kind='screen_cached' AND status='succeeded'")), 1)

    def test_pause_reopen_resume_preserves_waiter_and_does_not_dispatch_duplicate(self):
        versions = self.duplicates()
        batch = self.fixture.submit(versions)
        self.manager.step()
        original_tasks = {task['id'] for task in self.store.rows('SELECT * FROM tasks')}
        self.manager.action(self.fixture.project, batch, {'action': 'pause'})
        self.manager.stop()
        reopened = ScreenBatches(Store(self.store.root), self.fixture.queue, self.fixture.root,
                                 prepare=self.manager.prepare_override, select=self.manager.select_override)
        try:
            reopened.action(self.fixture.project, batch, {'action': 'resume'})
            task = self.store.claim()
            self.assertNotEqual(task['kind'], 'screen_cached')
            self.assertIsNone(self.store.claim())
            self.complete(task)
            reopened.step()
            self.assertEqual(reopened.get(self.fixture.project, batch)['counts']['completed'], 2)
            self.assertEqual({task['task_id'] for task in self.store.rows('SELECT * FROM screen_tasks')}, original_tasks)
        finally:
            reopened.stop()

    def test_context_and_primary_both_reuse_exact_input_with_current_version_receipt(self):
        versions = self.duplicates()
        batch = self.fixture.submit(versions, layout_evidence=True)
        self.manager.step()
        context = self.store.claim()
        self.assertEqual(context['kind'], 'screen_context')
        self.assertIsNone(self.store.claim())
        original_raw = self.context_success(context)
        self.manager.step()
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks WHERE kind='screen_context'")), 1)
        reused = self.store.rows('''SELECT c.*,t.status FROM screen_contexts c JOIN tasks t ON t.id=c.task_id
            WHERE t.kind='screen_context_cached' ''')[0]
        raw = json.loads(reused['raw'])
        self.assertEqual(reused['status'], 'succeeded')
        self.assertEqual(raw['image_version'], versions[1])
        self.assertEqual(raw['screen_context_input']['image_version'], versions[1])
        self.assertEqual(raw['cache_hit']['source_task_id'], context['id'])
        self.assertEqual(raw['layout_blocks'], original_raw['layout_blocks'])
        self.assertEqual(raw['inference_seconds'], 0)
        self.assertEqual(json.loads(self.store.file(reused['artifact']).read_text('utf-8')), raw)
        evidence = json.loads(self.store.rows('SELECT evidence FROM screen_items WHERE input_version_id=?', (versions[1],))[0]['evidence'])
        self.assertEqual(evidence['provenance']['image_version'], versions[1])
        self.assertEqual(evidence['provenance']['cache_hit']['source_task_id'], context['id'])
        ocr = self.store.claim()
        self.assertNotEqual(ocr['kind'], 'screen_cached')
        self.assertIsNone(self.store.claim())
        self.complete(ocr)
        self.manager.step()
        self.assertEqual(self.manager.get(self.fixture.project, batch)['counts']['completed'], 2)
        detail = self.manager.detail(self.fixture.project,batch,versions[1])
        self.assertEqual(detail['result']['original']['screen']['context_provenance']['image_version'], versions[1])
        self.assertEqual(detail['result']['original']['screen']['context_provenance']['cache_hit']['source_task_id'], context['id'])

    def test_failed_context_source_is_not_successfully_cloned(self):
        versions = self.duplicates()
        batch = self.fixture.submit(versions, layout_evidence=True)
        self.manager.step()
        failed = self.store.claim()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='failed',error='actual context failure',finished=datetime('now') WHERE id=?", (failed['id'],))
        self.manager.step()
        promoted = self.store.claim()
        self.assertEqual(promoted['kind'], 'screen_context')
        self.assertEqual(promoted['version_id'], versions[1])
        self.assertEqual(self.store.one('tasks', failed['id'])['status'], 'failed')
        self.context_success(promoted)
        self.manager.step()
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks WHERE kind='screen_context_cached' AND status='succeeded'")), 0)
        evidence = json.loads(self.store.rows('SELECT evidence FROM screen_items WHERE input_version_id=?', (versions[0],))[0]['evidence'])
        self.assertIn('context_failed', {warning['code'] for warning in evidence['warnings']})


if __name__ == '__main__':
    unittest.main()
