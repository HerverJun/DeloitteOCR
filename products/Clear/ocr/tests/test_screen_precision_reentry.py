"""Delayed native precision candidates preserve intervening human work.

Real Store/ScreenBatches and registered resolve routing are used. Recognition
and layout observations are controlled CPU fixtures, not accuracy evidence.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_screen_batches as fixtures


class PrecisionReentryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ScreenBatchTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager
        self.store = self.fixture.store
        self.project = self.fixture.project
        self.version = self.fixture.images(1)[0]
        self.parent = self.fixture.submit([self.version], primary_engine='hunyuan',
                                          require_human_review=True)
        self.manager.step(); self.fixture.finish(); self.manager.step()
        item = self.manager._items(self.parent)[0]
        self.image_id = item['image_id']
        self.before = self.store.result(item['result_id'])
        preparation = json.loads(item['preparation'])
        preparation['business_regions'] = [{'id': 'controlled-grid', 'table_box': [0, 0, 100, 70],
            'sources': ['pixel_grid'], 'grid_reliable': True,
            'observed_grid_rows': fixtures.TABLE['rows'],
            'observed_grid_columns': fixtures.TABLE['columns'] + 1}]
        self.manager._update(self.parent, self.version, preparation=json.dumps(preparation))
        with patch('ocr_workbench.screen_resolution.precision_capability',
                   return_value={'available': True, 'engine': 'glm', 'basis': 'controlled-test-only'}):
            receipt = self.manager.action(self.project, self.parent, {
                'action': 'upgrade', 'version_ids': [self.version], 'request_id': 'delayed-precision'})
        self.assertEqual(receipt['status'], 'submitted')
        self.child = receipt['batch_id']
        self.assertFalse(receipt['selection_applied'])
        self.assertFalse(receipt['values_modified'])

    def finish_delayed_candidate(self):
        self.manager.step(); self.fixture.finish(); self.manager.step()
        child = self.manager.get(self.project, self.child)
        self.assertEqual(child['counts']['needs_review'], 1)
        candidate = self.store.result(child['items'][0]['result_id'])
        self.assertNotEqual(candidate['id'], self.before['id'])
        return candidate

    def selected(self):
        return self.store.rows('SELECT result_id FROM selections WHERE image_id=?',
                               (self.image_id,))[0]['result_id']

    def test_edit_while_precision_runs_preserves_new_revision_and_value(self):
        edited = deepcopy(self.before['edited'])
        edited['tables'][0]['cells'][3]['text'] = '-9876.54'
        saved = self.store.save(self.before['id'], edited, self.before['revision'])
        self.finish_delayed_candidate()
        self.assertEqual(self.selected(), saved['id'])
        self.assertEqual(self.store.result(saved['id']), saved)
        detail = self.manager.detail(self.project, self.parent, self.version)
        self.assertEqual(detail['current_result_revision'], saved['revision'])
        self.assertFalse(detail['human_confirmed'])

    def test_reconfirmation_while_precision_runs_preserves_review_epoch(self):
        body = {'action': 'review', 'version_ids': [self.version], 'state': 'accepted',
                'expected_revision': self.before['revision']}
        self.manager.action(self.project, self.parent, body)
        self.manager.action(self.project, self.parent, body)
        review = self.store.rows('SELECT * FROM reviews WHERE image_id=?', (self.image_id,))[0]
        self.finish_delayed_candidate()
        self.assertEqual(self.selected(), self.before['id'])
        self.assertEqual(self.store.rows('SELECT * FROM reviews WHERE image_id=?', (self.image_id,))[0], review)
        self.assertTrue(self.manager.detail(self.project, self.parent, self.version)['human_confirmed'])

    def test_selected_other_native_candidate_is_not_replaced_by_late_precision(self):
        task = self.store.rows("SELECT result_id FROM tasks WHERE image_id=? AND kind='ocr'",
                               (self.image_id,))[0]
        target = self.store.result_snapshot(task['result_id'])
        body = {key: target[key] for key in ('revision', 'task_id', 'project_id',
                                            'image_id', 'version_id', 'original_sha256')}
        body.update(result_id=target['id'], instance_id='controlledNative', actor={
            'id': 'reviewer', 'display_name': 'CPU review fixture', 'source': 'local'}, note='choose existing candidate')
        self.store.adopt_result('manual-selection-reentry', body)
        self.finish_delayed_candidate()
        self.assertEqual(self.selected(), target['id'])
        self.assertFalse(self.manager.detail(self.project, self.parent, self.version)['human_confirmed'])

    def test_new_original_version_is_kept_after_old_precision_returns(self):
        from ocr_workbench.imaging import transform
        changed = transform(self.store, self.version, {'kind': 'rotate', 'degrees': 90})
        self.finish_delayed_candidate()
        self.assertEqual(self.store.one('images', self.image_id)['active_version'], changed['id'])
        self.assertEqual(self.selected(), self.before['id'])
        self.assertFalse(self.manager.delivery_snapshot(self.project, self.parent)['fresh'])
