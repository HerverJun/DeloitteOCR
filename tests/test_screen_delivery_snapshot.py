"""Exports bind changes that preserve file counts and result revisions."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from ocr_workbench.store import Conflict
import test_screen_batches as fixture


class DeliverySnapshotTests(unittest.TestCase):
    setUp = fixture.ScreenBatchTests.setUp
    tearDown = fixture.ScreenBatchTests.tearDown
    images = fixture.ScreenBatchTests.images
    submit = fixture.ScreenBatchTests.submit
    finish = fixture.ScreenBatchTests.finish

    def ready(self, count=1):
        versions = self.images(count)
        batch = self.submit(versions)
        for _ in range(4):
            self.manager.step()
            self.finish()
        return batch, versions

    def test_unchanged_read_and_reexport_keep_semantic_snapshot(self):
        batch, _ = self.ready()
        first = self.manager.delivery_snapshot(self.project, batch)
        self.manager.get(self.project, batch)
        self.assertEqual(first, self.manager.delivery_snapshot(self.project, batch))
        _, export = self.manager.export(self.project, batch)
        self.assertEqual(export['snapshot_id'], first['snapshot_id'])
        self.assertEqual(export['export_options'], {'include_pending_review': False, 'numeric_columns': None})
        self.assertEqual(export['version_vector']['snapshot_id'], first['snapshot_id'])
        self.assertEqual(export['version_vector']['items'], first['items'])

    def test_reconfirming_same_result_invalidates_old_export_without_new_revision(self):
        batch, versions = self.ready()
        item = self.manager.get(self.project, batch)['items'][0]
        body = {'action': 'review', 'version_ids': versions, 'result_id': item['result_id'],
                'expected_revision': item['result_revision'], 'state': 'accepted'}
        self.manager.action(self.project, batch, body)
        before = self.manager.delivery_snapshot(self.project, batch)
        self.manager.action(self.project, batch, body)
        after = self.manager.delivery_snapshot(self.project, batch)
        self.assertTrue(after['fresh'])
        self.assertEqual(before['items'][0]['current_revision'], after['items'][0]['current_revision'])
        self.assertNotEqual(before['items'][0]['review_epoch'], after['items'][0]['review_epoch'])
        self.assertNotEqual(before['snapshot_id'], after['snapshot_id'])

    def test_edited_result_same_file_count_invalidates_old_snapshot(self):
        batch, _ = self.ready()
        before = self.manager.delivery_snapshot(self.project, batch)
        item = self.manager.get(self.project, batch)['items'][0]
        result = self.store.result(item['result_id'])
        edited = deepcopy(result['edited'])
        edited['tables'][0]['cells'][2]['text'] = 'new exact value'
        self.store.save(result['id'], edited, result['revision'])
        after = self.manager.delivery_snapshot(self.project, batch)
        self.assertNotEqual(before['snapshot_id'], after['snapshot_id'])
        self.assertFalse(after['fresh'])
        with self.assertRaises(Conflict):
            self.manager.export(self.project, batch)

    def test_review_transition_without_edit_changes_snapshot(self):
        batch, versions = self.ready()
        item = self.manager.get(self.project, batch)['items'][0]
        before = self.manager.delivery_snapshot(self.project, batch)
        self.manager.action(self.project, batch, {'action': 'review', 'version_ids': versions,
            'result_id': item['result_id'], 'expected_revision': item['result_revision'], 'state': 'accepted'})
        after = self.manager.delivery_snapshot(self.project, batch)
        self.assertTrue(after['fresh'])
        self.assertNotEqual(before['snapshot_id'], after['snapshot_id'])
        self.store.set_review(item['image_id'], 'question', item['result_id'], item['result_revision'], versions[0])
        self.assertFalse(self.manager.delivery_snapshot(self.project, batch)['fresh'])

    def test_adopted_candidate_change_with_equal_revision_is_stale(self):
        batch, versions = self.ready()
        item = self.manager.get(self.project, batch)['items'][0]
        tasks = self.store.enqueue(self.project, versions, ['glm'])
        self.finish()
        other = self.store.one('tasks', tasks[0])['result_id']
        with self.store.transaction() as db:
            db.execute('INSERT INTO selections VALUES(?,?) ON CONFLICT(image_id) DO UPDATE SET result_id=excluded.result_id',
                       (item['image_id'], other))
        snapshot = self.manager.delivery_snapshot(self.project, batch)
        self.assertFalse(snapshot['fresh'])
        self.assertIn('selected_result_changed', snapshot['stale_items'][0]['reasons'])

    def test_review_during_export_is_rejected_even_without_revision_change(self):
        from ocr_workbench.tables import export_xlsx
        batch, versions = self.ready()
        item = self.manager.get(self.project, batch)['items'][0]
        def export_then_review(*args, **kwargs):
            export_xlsx(*args, **kwargs)
            self.manager.action(self.project, batch, {'action': 'review', 'version_ids': versions,
                'result_id': item['result_id'], 'expected_revision': item['result_revision'], 'state': 'accepted'})
        with patch('ocr_workbench.tables.export_xlsx', export_then_review), self.assertRaises(Conflict):
            self.manager.export(self.project, batch)

    def test_partial_export_becomes_history_when_remaining_item_finishes(self):
        batch, versions = self.ready(2)
        self.manager._update(batch, versions[1], status='running', stage='upgrade')
        _, manifest = self.manager.export(self.project, batch)
        self.assertEqual(manifest['success_files'], 1)
        self.assertEqual(len(manifest['items']), 2)
        self.manager._update(batch, versions[1], status='completed', stage='completed')
        self.assertNotEqual(manifest['snapshot_id'], self.manager.delivery_snapshot(self.project, batch)['snapshot_id'])
