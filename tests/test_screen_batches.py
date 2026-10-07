"""Batch identity, independent XLSX coverage, failures and revision protection."""
from copy import deepcopy
import io
import json
import shutil
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from PIL import Image
from openpyxl import load_workbook

from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches, validate_policy
from ocr_workbench.store import Conflict, Store


TABLE = {'rows': 3, 'columns': 2, 'caption': '单位：元', 'cells': [
    {'row': r, 'column': c, 'row_span': 1, 'column_span': 1, 'text': text, 'is_header': r == 0}
    for r, row in enumerate([['编号', '金额'], ['00123456789012345678', '123.45'], ['00002', '=1+1']])
    for c, text in enumerate(row)]}


class Queue:
    def __init__(self, store):
        self.store, self.wake = store, threading.Event()

    def action(self, project, action, task_ids):
        status = {'pause': 'paused', 'resume': 'queued', 'cancel': 'cancelled'}[action]
        with self.store.transaction() as db:
            for key in task_ids:
                db.execute("UPDATE tasks SET status=? WHERE id=? AND project_id=? AND status NOT IN ('succeeded','failed','cancelled')", (status, key, project))


def prepare(source, folder, **kwargs):
    target = Path(folder) / 'primary.png'
    with Image.open(source) as image:
        image.save(target)
        w, h = image.size
    matrix = [1, 0, 0, 0, 1, 0, 0, 0, 1]
    return {'width': w, 'height': h, 'primary_id': 'primary',
            'quality': {'flags': [], 'unreadable': False}, 'business_regions': [],
            'prepared_images': [{'id': 'primary', 'path': str(target), 'kind': 'original',
                                 'width': w, 'height': h, 'prepared_to_source': matrix}]}


def select(candidates, *args, **kwargs):
    selected = candidates[0]
    return {'selected_id': selected['id'], 'selected_result': deepcopy(selected['raw']),
            'automatic_delivery': True, 'review_required': False,
            'assessments': [{'id': selected['id'], 'issues': []}], 'conflicts': [], 'modifications': [], 'upgrade_kind': None}


class ScreenBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='native-screen-test-', dir='F:/OCR/output/ocr-screen-benchmark-20261007/temp')
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / 'workspace')
        self.project = self.store.project('屏拍批次')['id']
        self.queue = Queue(self.store)
        self.manager = ScreenBatches(self.store, self.queue, self.root, prepare=prepare, select=select)

    def tearDown(self):
        self.manager.stop()
        self.temporary.cleanup()

    def images(self, count):
        versions = []
        for i in range(count):
            source = self.root / f'input-{i}.png'
            Image.new('RGB', (100, 70), (i % 255, 255, 255)).save(source)
            image = add_image(self.store, self.project, '同名.png', source)
            versions.append(image['active_version'])
        return versions

    def submit(self, versions, **policy):
        return self.manager.submit(self.project, {'version_ids': versions, 'request_id': 'request-screen-1',
            'policy': {'primary_engine': 'glm', 'max_upgrades': 0, **policy}})['batch_id']

    def finish(self, *, failure=False):
        while task := self.store.claim():
            if failure:
                with self.store.transaction() as db:
                    db.execute("UPDATE tasks SET status='failed',error='model unavailable' WHERE id=?", (task['id'],))
            else:
                self.store.complete(task['id'], {'status': 'success', 'text': '业务数据', 'tables': [deepcopy(TABLE)],
                    'blocks': [], 'engine': task['engine'], 'elapsed_seconds': .1, 'project_image_version': task['version_id']})

    def test_240_inputs_yield_240_independent_excel_files_and_stable_names(self):
        versions = self.images(240)
        batch = self.submit(versions)
        for _ in range(10):
            self.manager.step()
            self.finish()
        self.manager.step()
        state = self.manager.get(self.project, batch)
        self.assertEqual(state['counts'], {'total': 240, 'completed': 240, 'needs_review': 0, 'failed': 0, 'cancelled': 0, 'pending': 0})
        self.assertEqual(len(state['items']), 30)
        path, manifest = self.manager.export(self.project, batch)
        self.assertEqual(manifest['success_files'], 240)
        with zipfile.ZipFile(path) as archive:
            xlsx = [name for name in archive.namelist() if name.endswith('.xlsx')]
            self.assertEqual(len(xlsx), 240)
            book = load_workbook(io.BytesIO(archive.read(xlsx[0])))
            sheet = book.worksheets[0]
            self.assertEqual(sheet['A3'].value, '00123456789012345678')
            self.assertEqual(sheet['B4'].value, '=1+1')
            self.assertEqual(sheet['B4'].data_type, 's')
        self.assertEqual({x['input_version_id'] for x in manifest['items']}, set(versions))
        # Synthetic OCR payloads verify engineering/export only, never OCR accuracy.

    def test_submission_idempotency_and_conflicting_scope(self):
        versions = self.images(2)
        first = self.submit(versions)
        self.assertEqual(first, self.submit(versions))
        with self.assertRaises(Conflict):
            self.submit(versions[:1])
        self.assertEqual(self.manager.lookup(self.project, 'request-screen-1')['batch_id'], first)

    def test_lost_submission_recovers_frozen_defaults_after_policy_update(self):
        versions = self.images(1)
        self.manager.defaults = lambda: {'primary_engine': 'hunyuan', 'max_upgrades': 0, 'group_size': 32,
                                        'strategy_version': 'measured-first'}
        body = {'version_ids': versions, 'request_id': 'request-default-recovery', 'policy': {'primary_engine': 'auto'}}
        first = self.manager.submit(self.project, body)
        self.manager.defaults = lambda: {'primary_engine': 'glm', 'max_upgrades': 2, 'group_size': 16,
                                        'strategy_version': 'measured-next'}
        recovered = self.manager.submit(self.project, body)
        self.assertEqual(recovered['batch_id'], first['batch_id'])
        self.assertEqual(recovered['policy'], first['policy'])
        with self.assertRaises(Conflict):
            self.manager.submit(self.project, {**body, 'policy': {'primary_engine': 'glm'}})
        with self.assertRaisesRegex(ValueError, '下载时'):
            self.manager.submit(self.project, {**body, 'request_id': 'other-export-options', 'export_options': {'numeric': True}})

    def test_failed_only_retry_preserves_prior_failure_and_completed_result(self):
        versions = self.images(2)
        batch = self.submit(versions)
        self.manager.step()
        tasks = self.store.rows('SELECT * FROM tasks ORDER BY created,id')
        success, failed = tasks
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (success['id'],))
            db.execute("UPDATE tasks SET status='failed',error='unavailable' WHERE id=?", (failed['id'],))
        self.store.complete(success['id'], {'text': 'data', 'tables': [deepcopy(TABLE)], 'blocks': [], 'engine': 'glm'})
        self.manager.step()
        original = self.manager.detail(self.project, batch, success['version_id'])['result_id']
        self.manager.action(self.project, batch, {'action': 'retry_failed', 'request_id': 'retry-screen-1'})
        self.manager.step()
        self.finish()
        self.manager.step()
        self.assertEqual(self.manager.get(self.project, batch)['counts']['completed'], 2)
        self.assertEqual(self.store.one('tasks', failed['id'])['status'], 'failed')
        self.assertEqual(self.manager.detail(self.project, batch, success['version_id'])['result_id'], original)
        self.assertEqual(len(self.store.rows('SELECT * FROM screen_tasks')), 3)

    def test_pause_before_preparation_and_resume_has_no_duplicate_tasks(self):
        batch = self.submit(self.images(2))
        self.manager.action(self.project, batch, {'action': 'pause'})
        self.assertFalse(self.manager.step())
        self.manager.action(self.project, batch, {'action': 'resume'})
        self.manager.step()
        self.finish()
        self.manager.step()
        self.assertEqual(self.manager.get(self.project, batch)['status'], 'succeeded')
        self.assertEqual(len(self.store.rows('SELECT * FROM screen_tasks')), 2)

    def test_reopen_recovers_completed_tasks_without_new_recognition(self):
        batch = self.submit(self.images(1))
        self.manager.step()
        self.finish()
        reopened = ScreenBatches(Store(self.store.root), self.queue, self.root, prepare=prepare, select=select)
        reopened.step()
        self.assertEqual(reopened.get(self.project, batch)['status'], 'succeeded')
        self.assertEqual(len(self.store.rows('SELECT * FROM screen_tasks')), 1)
        reopened.stop()

    def test_stale_result_revision_cannot_be_exported_or_reviewed(self):
        version = self.images(1)[0]
        batch = self.submit([version])
        self.manager.step(); self.finish(); self.manager.step()
        detail = self.manager.detail(self.project, batch, version)
        value = deepcopy(detail['result']['edited'])
        value['tables'][0]['cells'][2]['text'] = 'changed'
        self.store.save(detail['result_id'], value, 0)
        with self.assertRaises(Conflict):
            self.manager.export(self.project, batch)
        with self.assertRaises(Conflict):
            self.manager.action(self.project, batch, {'action': 'review', 'version_ids': [version],
                'result_id': detail['result_id'], 'expected_revision': 0, 'state': 'accepted'})

    def test_cross_project_scope_and_file_traversal_rejected(self):
        versions = self.images(1)
        batch = self.submit(versions)
        other = self.store.project('其他项目')['id']
        with self.assertRaises(KeyError):
            self.manager.get(other, batch)
        with self.assertRaises(KeyError):
            self.manager.file(self.project, batch, versions[0], '../outside')

    def test_explicit_review_is_separate_from_automatic_delivery(self):
        version = self.images(1)[0]
        batch = self.submit([version])
        self.manager.step(); self.finish(); self.manager.step()
        detail = self.manager.detail(self.project, batch, version)
        self.assertFalse(detail['human_confirmed'])
        self.manager.action(self.project, batch, {'action': 'review', 'version_ids': [version],
            'result_id': detail['result_id'], 'expected_revision': 0, 'state': 'accepted'})
        self.assertTrue(self.manager.detail(self.project, batch, version)['human_confirmed'])

    def test_invalid_policy_and_no_table_never_make_blank_workbook(self):
        for policy in [{'max_upgrades': 3}, {'primary_engine': 'ppocr'}, {'budget_seconds': True}]:
            with self.assertRaises(ValueError):
                validate_policy(policy)
        batch = self.submit(self.images(1))
        with self.assertRaises(ValueError):
            self.manager.export(self.project, batch)

    def test_selected_precision_attempt_has_lineage_and_is_idempotent(self):
        versions = self.images(2)
        batch = self.submit(versions)
        self.manager.step(); self.finish(); self.manager.step()
        command = {'action': 'upgrade', 'version_ids': [versions[1]], 'request_id': 'precision-only-one'}
        improved = self.manager.action(self.project, batch, command)
        self.assertEqual(improved['parent_batch_id'], batch)
        self.assertEqual(improved['counts']['total'], 1)
        self.assertEqual(improved['policy']['mode'], 'accuracy')
        self.assertNotEqual(improved['batch_id'], batch)
        self.assertEqual(self.manager.action(self.project, batch, command)['batch_id'], improved['batch_id'])
        self.assertEqual(self.manager.get(self.project, batch)['counts']['completed'], 2)

    def test_pending_filter_and_collection_preserve_all_inputs(self):
        versions = self.images(3)
        batch = self.submit(versions)
        self.manager.step()
        self.assertEqual(self.manager.get(self.project, batch, status='pending')['total_items'], 3)
        self.assertEqual(self.manager.list(self.project)['batches'][0]['batch_id'], batch)
        with self.assertRaises(Conflict):
            self.manager.action(self.project, batch, {'action': 'review', 'version_ids': [versions[0]], 'state': 'accepted'})

    def test_exact_content_cache_reuses_inference_but_delivers_each_input(self):
        version = self.images(1)[0]
        batch = self.submit([version])
        self.manager.step(); self.finish(); self.manager.step()
        duplicate = self.root / 'duplicate.png'
        shutil.copyfile(self.store.file(self.store.one('versions', version)['path']), duplicate)
        image = add_image(self.store, self.project, '重复.png', duplicate)
        second = self.manager.submit(self.project, {'version_ids': [image['active_version']], 'request_id': 'duplicate-content',
            'policy': {'primary_engine': 'glm', 'max_upgrades': 0}})['batch_id']
        self.manager.step()
        self.assertEqual(self.manager.get(self.project, second)['counts']['completed'], 1)
        task = self.store.rows("SELECT * FROM tasks WHERE kind='screen_cached'")[0]
        self.assertEqual(task['input_version_id'], image['active_version'])
        self.assertNotEqual(self.manager.detail(self.project, second, image['active_version'])['result_id'],
                            self.manager.detail(self.project, batch, version)['result_id'])

    def test_numeric_export_is_per_image_and_preserves_identifiers(self):
        version = self.images(1)[0]
        batch = self.submit([version])
        self.manager.step(); self.finish(); self.manager.step()
        detail = self.manager.detail(self.project, batch, version)
        self.manager.action(self.project, batch, {'action': 'review', 'version_ids': [version], 'result_id': detail['result_id'],
            'expected_revision': detail['result_revision'], 'state': 'accepted'})
        path, _ = self.manager.export(self.project, batch, {'numeric_columns': {version: {'0': [0, 1]}}})
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.endswith('.xlsx'))
            book = load_workbook(io.BytesIO(archive.read(name)))
            self.assertEqual(book.worksheets[0]['A3'].value, '00123456789012345678')
            self.assertEqual(book.worksheets[0]['B3'].value, 123.45)
            self.assertEqual(book.worksheets[0]['B4'].data_type, 's')
        with self.assertRaises(ValueError):
            self.manager.export(self.project, batch, {'numeric_columns': {'0': [1]}})

    def test_context_stage_finishes_before_any_primary_task_and_recovers(self):
        versions = self.images(2)
        batch = self.submit(versions, layout_evidence=True)
        self.manager.step()
        tasks = self.store.rows('SELECT * FROM tasks')
        self.assertEqual({t['kind'] for t in tasks}, {'screen_context'})
        self.assertEqual(len(tasks), 2)
        raw = {'status': 'success', 'component': 'shared-geometry-context', 'image': {'width': 100, 'height': 70},
               'layout_blocks': [], 'regions': [], 'ocr_blocks': [], 'ocr_source': 'PP-OCRv6-auxiliary'}
        with self.store.transaction() as db:
            for task in tasks:
                db.execute("UPDATE tasks SET status='succeeded' WHERE id=?", (task['id'],))
                db.execute('UPDATE screen_contexts SET raw=? WHERE task_id=?', (json.dumps(raw), task['id']))
        self.manager.step(); self.finish(); self.manager.step()
        self.assertEqual(self.manager.get(self.project, batch)['counts']['completed'], 2)
        self.assertEqual(len(self.store.rows('SELECT * FROM screen_contexts')), 2)

    def test_expired_batch_budget_preserves_completed_and_stops_pending(self):
        versions = self.images(2)
        batch = self.submit(versions)
        self.manager.step()
        task = self.store.claim()
        self.store.complete(task['id'], {'text': 'data', 'tables': [deepcopy(TABLE)], 'blocks': [], 'engine': 'glm'})
        self.manager.step()
        with self.store.transaction() as db:
            db.execute('UPDATE screen_batches SET deadline=0.1 WHERE id=?', (batch,))
        self.manager.step()
        state = self.manager.get(self.project, batch)
        self.assertEqual(state['counts']['completed'], 1)
        self.assertEqual(state['counts']['failed'], 1)
        self.assertEqual(state['counts']['pending'], 0)

    def test_v18_cannot_be_opened_by_v17_without_modifying_database(self):
        import hashlib
        from unittest.mock import patch
        self.manager.stop()
        database = self.store.root / 'workbench.sqlite3'
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        with patch('ocr_workbench.store.SCHEMA_VERSION', 17), self.assertRaises(ValueError):
            Store(self.store.root)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_project_cleanup_covers_batch_exports_and_cascades_state(self):
        from ocr_workbench.maintenance import ProjectMaintenance
        version = self.images(1)[0]
        batch = self.submit([version])
        self.manager.step(); self.finish(); self.manager.step()
        self.manager.export(self.project, batch)
        maintenance = ProjectMaintenance(self.store, self.queue)
        self.assertIn(self.manager.root / batch, maintenance.paths(self.project))
        self.assertEqual(maintenance.safe(str((self.manager.root / batch).relative_to(self.store.root)), self.project), self.manager.root / batch)
        self.manager.stop()
        self.queue.status = lambda: {'task_id': None}
        maintenance.delete(self.project, '屏拍批次')
        self.assertFalse((self.manager.root / batch).exists())
        self.assertFalse(self.store.rows('SELECT * FROM screen_batches'))


if __name__ == '__main__':
    unittest.main()
