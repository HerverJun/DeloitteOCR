"""Cross-feature queue identity and current structure evidence regressions."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from PIL import Image

import test_multimodal_store as multimodal_fixtures
import test_structure_workflow as structure_fixtures
from ocr_workbench.document_review import document_review_queue, refresh_document_review
from ocr_workbench.store import Conflict, Store, encoded
from ocr_workbench.structure_store import decide_structure, record_candidates, refresh_proposals, structure_view
from ocr_workbench.table_tool import tool_identity


class CurrentStructureQueueTests(unittest.TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.StructureStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store, self.result_id = self.fixture.store, self.fixture.result_id
        self.page = self.store.page_for_version(self.fixture.version['id'])
        self.document_id = self.page['document_id']
        self.tool = tool_identity()
        raw = self.store.result(self.result_id)['original']
        raw.update(origin='document', document={'page_id': self.page['id'], 'mode': 'native',
            'table_tool': {'state': 'ready', 'retry_allowed': True, 'tool_key': self.tool['key'],
                          'run_id': 'first-tool-run', 'message': 'Current fixture candidates'}})
        self.prediction = deepcopy(self.fixture.pred)
        self.prediction.update(candidate_provider_key='pdfplumber/fixture', table_tool_run_id='first-tool-run')
        with self.store.transaction() as db:
            db.execute('DELETE FROM structure_candidates WHERE result_id=?', (self.result_id,))
            db.execute('UPDATE documents SET kind=? WHERE id=?', ('pdf', self.document_id))
            db.execute('UPDATE results SET original=? WHERE id=?', (encoded(raw), self.result_id))
            record_candidates(db, self.result_id, self.fixture.version, self.prediction, self.fixture.blocks)

    def queue(self, **kwargs):
        return document_review_queue(self.store, self.document_id, **kwargs)

    def check(self):
        return refresh_proposals(self.store, self.result_id, self.store.result(self.result_id)['revision'])

    def proposal(self):
        return next(p for p in self.check()['proposals'] if p['kind'] == 'replace_table' and p['state'] == 'pending')

    def assert_no_current_structure(self):
        panel = structure_view(self.store, self.result_id)
        self.assertEqual(panel['candidates'], [])
        self.assertFalse(any(p['state'] in ('pending', 'deferred') for p in panel['proposals']))
        for state in ('open', 'deferred', 'all'):
            queue = self.queue(state=state)
            self.assertFalse(any(t['kind'] == 'structure' and t['state'] != 'resolved' for t in queue['tasks']))
            self.assertEqual(queue['summary']['candidate_pages'], 0)
            self.assertEqual(queue['summary']['checked_pages'], 0)

    def test_current_candidates_and_check_agree_with_panel(self):
        queue = self.queue()
        self.assertEqual(queue['summary']['candidate_pages'], 1)
        self.assertEqual(queue['summary']['checked_pages'], 0)
        self.assertIn('structure_check', [t['kind'] for t in queue['tasks']])
        panel = self.check()
        queue = self.queue()
        alternatives = [p for t in queue['tasks'] if t['kind'] == 'structure' for p in t['alternatives']]
        self.assertEqual({p['id'] for p in panel['proposals']}, {p['id'] for p in alternatives})
        self.assertTrue(all(p['can_apply'] for p in alternatives))
        self.assertEqual(queue['summary']['checked_pages'], 1)
        self.assertNotIn('structure_check', [t['kind'] for t in queue['tasks']])

    def test_tool_upgrade_hides_pending_and_deferred_even_after_recheck(self):
        proposal = self.proposal()
        decide_structure(self.store, self.result_id, proposal['id'], self.fixture.body(proposal, action='defer'))
        with patch('ocr_workbench.table_tool.tool_identity', return_value={**self.tool, 'key': 'upgraded-tool-key'}):
            self.assert_no_current_structure()
            self.assertIn('table_tool', [t['kind'] for t in self.queue()['tasks']])
            refresh_document_review(self.store, self.document_id)
            self.assert_no_current_structure()
            with self.assertRaises(Conflict):
                decide_structure(self.store, self.result_id, proposal['id'], self.fixture.body(proposal, request='late-accept'))

    def test_new_tool_run_hides_previous_candidates_until_new_check(self):
        self.check()
        raw = self.store.result(self.result_id)['original']
        raw['document']['table_tool']['run_id'] = 'second-tool-run'
        with self.store.transaction() as db:
            db.execute('UPDATE results SET original=? WHERE id=?', (encoded(raw), self.result_id))
        self.assert_no_current_structure()
        newer = {**self.prediction, 'table_tool_run_id': 'second-tool-run'}
        with self.store.transaction() as db:
            current_id = record_candidates(db, self.result_id, self.fixture.version, newer, self.fixture.blocks)
        self.assertEqual(self.queue()['summary']['checked_pages'], 0)
        panel = self.check()
        self.assertEqual([c['id'] for c in panel['candidates']], [current_id])
        self.assertTrue(all(p['candidate_set_id'] == current_id for p in panel['proposals']))
        self.assertEqual(self.queue()['summary']['checked_pages'], 1)

    def test_completed_decisions_remain_in_all_but_cannot_apply_after_upgrade(self):
        proposal = self.proposal()
        decide_structure(self.store, self.result_id, proposal['id'], self.fixture.body(proposal))
        with patch('ocr_workbench.table_tool.tool_identity', return_value={**self.tool, 'key': 'upgraded-tool-key'}):
            self.assert_no_current_structure()
            rows = [t for t in self.queue(state='all')['tasks'] if t['kind'] == 'structure']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['state'], 'resolved')
            self.assertEqual(rows[0]['alternatives'][0]['state'], 'accepted')
            self.assertFalse(rows[0]['alternatives'][0]['can_apply'])
            self.assertFalse(structure_view(self.store, self.result_id)['proposals'][0]['can_apply'])

    def test_image_hash_change_hides_pending_and_invalidates_check(self):
        self.check()
        with self.store.transaction() as db:
            db.execute("UPDATE versions SET sha256='different-content' WHERE id=?", (self.fixture.version['id'],))
        self.assert_no_current_structure()

    def test_revision_change_requires_a_new_check(self):
        self.check()
        result = self.store.result(self.result_id)
        result['edited']['text'] += '\nreview note'
        self.store.save(self.result_id, result['edited'], result['revision'])
        self.assertFalse(any(t['kind'] == 'structure' for t in self.queue()['tasks']))
        self.assertEqual(self.queue()['summary']['checked_pages'], 0)
        self.assertEqual(self.queue()['summary']['candidate_pages'], 1)
        self.assertFalse(structure_view(self.store, self.result_id)['proposals'])
        self.check()
        self.assertEqual(self.queue()['summary']['checked_pages'], 1)

    def test_kept_and_rejected_history_survives_unrelated_edit_and_tool_upgrade(self):
        proposals = self.check()['proposals']
        self.assertGreaterEqual(len(proposals), 2)
        decide_structure(self.store, self.result_id, proposals[0]['id'],
                         self.fixture.body(proposals[0], action='reject'))
        decide_structure(self.store, self.result_id, proposals[1]['id'],
                         self.fixture.body(proposals[1], action='keep', request='keep-other'))
        result = self.store.result(self.result_id)
        result['edited']['text'] += '\nunrelated note'
        self.store.save(self.result_id, result['edited'], result['revision'])
        with patch('ocr_workbench.table_tool.tool_identity', return_value={**self.tool, 'key': 'upgraded-tool-key'}):
            self.assert_no_current_structure()
            alternatives = [p for t in self.queue(state='all')['tasks'] if t['kind'] == 'structure' for p in t['alternatives']]
            self.assertEqual({p['state'] for p in alternatives}, {'kept', 'rejected'})
            self.assertTrue(all(not p['can_apply'] for p in alternatives))

    def test_changed_candidate_fingerprint_requires_recheck(self):
        self.check()
        with self.store.transaction() as db:
            db.execute("UPDATE structure_checks SET candidates_sha256='old-candidate-set' WHERE result_id=?", (self.result_id,))
        self.assertEqual(self.queue()['summary']['checked_pages'], 0)
        self.assertIn('structure_check', [t['kind'] for t in self.queue()['tasks']])
        refresh_document_review(self.store, self.document_id)
        self.assertEqual(self.queue()['summary']['checked_pages'], 1)

    def test_new_active_image_version_has_no_current_candidates_or_check(self):
        from ocr_workbench.imaging import save_version
        self.check()
        save_version(self.store, self.fixture.version, Image.new('RGB', (120, 200), 'white'),
                     {'kind': 'rotate', 'degrees': 90})
        queue = self.queue(state='all')
        self.assertEqual([t['kind'] for t in queue['tasks']], ['page_processing'])
        self.assertEqual(queue['summary']['unprocessed_pages'], 1)
        self.assertEqual(queue['summary']['candidate_pages'], 0)
        self.assertEqual(queue['summary']['checked_pages'], 0)
        with self.assertRaises(Conflict):
            structure_view(self.store, self.result_id)

    def test_panel_pins_revision_and_candidates_during_another_store_edit(self):
        from ocr_workbench.table_tool import view as original_tool_view
        self.check()
        other = Store(self.store.root)
        def edit_after_result_read(db, result):
            saved = other.result(self.result_id)
            saved['edited']['text'] += '\nconcurrent note'
            other.save(self.result_id, saved['edited'], saved['revision'])
            return original_tool_view(db, result)
        with patch('ocr_workbench.table_tool.view', side_effect=edit_after_result_read):
            panel = structure_view(self.store, self.result_id)
        self.assertEqual(other.result(self.result_id)['revision'], 1)
        self.assertEqual(panel['revision'], 0)
        self.assertTrue(panel['proposals'])
        self.assertTrue(all(p['revision'] == 0 and p['can_apply'] for p in panel['proposals']))
        self.assertEqual(structure_view(self.store, self.result_id)['proposals'], [])


class MultimodalQueueIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = multimodal_fixtures.MultimodalStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.document_id = self.store.page_for_version(self.fixture.version['id'])['document_id']

    def queue(self, **kwargs):
        return document_review_queue(self.store, self.document_id, **kwargs)

    def test_repeated_targets_and_pending_tasks_have_unique_stable_paginated_ids(self):
        self.fixture.generated(request='review-one')
        self.fixture.generated(request='review-two')
        self.fixture.enqueue(request='pending-one')
        self.fixture.enqueue(request='pending-two')
        first = self.queue(limit=100)
        self.assertEqual(first['total'], 8)
        self.assertEqual(len({t['id'] for t in first['tasks']}), 8)
        self.assertEqual(first['tasks'], self.queue(limit=100)['tasks'])
        paginated = []
        for offset in range(0, first['total'], 2):
            page = self.queue(offset=offset, limit=2)
            self.assertEqual(page['total'], first['total'])
            paginated.extend(page['tasks'])
        self.assertEqual(paginated, first['tasks'])
        ids = {t.get('proposal_id') or t.get('task_id'): t['id'] for t in first['tasks']}
        pending = next(t for t in first['tasks'] if t['kind'] == 'multimodal_task')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='failed',error='synthetic failure' WHERE id=?", (pending['task_id'],))
        self.assertEqual(ids, {t.get('proposal_id') or t.get('task_id'): t['id'] for t in self.queue(limit=100)['tasks']})

    def test_proposal_identity_survives_target_rebase(self):
        _, _, proposals = self.fixture.generated({'编号 00001': '编号 000000001', '金额 -0.01': '金额 -0.10'})
        before = {t['proposal_id']: t for t in self.queue()['tasks'] if t['kind'] == 'multimodal'}
        first = next(p for p in proposals if p['before'] == '编号 00001')
        sibling = next(p for p in proposals if p['before'] == '金额 -0.01')
        self.fixture.decide(first)
        after = {t['proposal_id']: t for t in self.queue(state='all')['tasks'] if t['kind'] == 'multimodal'}
        self.assertNotEqual(before[sibling['id']]['target'], after[sibling['id']]['target'])
        self.assertEqual({k: t['id'] for k, t in before.items()}, {k: t['id'] for k, t in after.items()})


if __name__ == '__main__':
    unittest.main()
