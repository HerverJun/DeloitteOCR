"""New multimodal records admit supported readers before any DATA writes."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.product_compatibility import DATA_COMPATIBILITY, check
from workbench_platform.resources.reader_features import require_features
from workbench_platform.agent.store import AgentStore


ROOT = Path('F:/OCR/output/clear-platform-tools-20261016/tests/reader-gate')
FEATURE = 'agent-multimodal-v1'


class PlatformReaderGateTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_feature_marker_rejects_old_reader_without_touching_data(self):
        require_features(self.root, 'agent-harness-v1')
        require_features(self.root, FEATURE)
        marker = self.root / 'product-reader-requirements.json'
        original = marker.read_bytes()
        old_reader = deepcopy(DATA_COMPATIBILITY)
        old_reader['reader_features'].remove(FEATURE)
        with self.assertRaisesRegex(ValueError, '不能直接降级'):
            check(self.root, old_reader)
        self.assertEqual(marker.read_bytes(), original)
        self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(set(json.loads(original)['reader_features']),
                         {'agent-harness-v1', FEATURE})

    def test_untouched_data_remains_usable_by_previous_reader(self):
        old_reader = deepcopy(DATA_COMPATIBILITY)
        old_reader['reader_features'].remove(FEATURE)
        self.assertTrue(check(self.root, old_reader)['compatible'])
        self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])
        self.assertFalse((self.root / 'product-reader-requirements.json').exists())

    def test_marker_updates_keep_legacy_requirements_and_are_idempotent(self):
        require_features(self.root, 'manual-multimodal-v1', 'agent-harness-v1')
        require_features(self.root, FEATURE)
        before = (self.root / 'product-reader-requirements.json').read_bytes()
        require_features(self.root, FEATURE)
        self.assertEqual((self.root / 'product-reader-requirements.json').read_bytes(), before)
        self.assertFalse(list(self.root.glob('*.new')))

    def test_financial_amount_marker_rejects_previous_reader_without_data_changes(self):
        feature = 'data-financial-amount-v1'
        previous_reader = deepcopy(DATA_COMPATIBILITY)
        previous_reader['reader_features'].remove(feature)
        self.assertTrue(check(self.root, previous_reader)['compatible'])
        require_features(self.root, feature)
        marker = self.root / 'product-reader-requirements.json'
        before = marker.read_bytes()
        with self.assertRaisesRegex(ValueError, '不能直接降级'):
            check(self.root, previous_reader)
        self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])
        self.assertEqual(marker.read_bytes(), before)

    def test_nonempty_new_ledger_rejects_missing_feature_without_marker(self):
        old_reader = deepcopy(DATA_COMPATIBILITY)
        old_reader['reader_features'].remove(FEATURE)
        path = self.root / 'jobs.sqlite3'
        for table in ('manual_operation', 'manual_result_snapshot'):
            with self.subTest(table=table):
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('CREATE TABLE "'+table+'"(body TEXT)')
                self.assertTrue(check(self.root, old_reader)['compatible'])
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('INSERT INTO "'+table+'" VALUES(?)', ('{}',))
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, '固定结果与恢复'):
                    check(self.root, old_reader)
                self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])
                self.assertEqual(path.read_bytes(), before)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('DROP TABLE "'+table+'"')

    def test_local_cpu_validation_blocks_old_reader_without_rewriting_receipt(self):
        receipt = self.root / 'multimodal-cpu-validation.json'
        receipt.write_text('{"format":"clear-local-cpu-validation-v1","items":{}}', encoding='utf-8')
        before = receipt.read_bytes()
        old_reader = deepcopy(DATA_COMPATIBILITY)
        old_reader['reader_features'].remove(FEATURE)
        with self.assertRaisesRegex(ValueError, '本机准备'):
            check(self.root, old_reader)
        self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])
        self.assertEqual(receipt.read_bytes(), before)

    def test_unmarked_policy_and_authorized_scope_reject_old_reader_but_defaults_do_not(self):
        store = AgentStore(self.root / 'agent')
        with store.transaction() as db:
            db.execute('CREATE TABLE audit_tasks(session_id TEXT,body TEXT)')
        old_reader = deepcopy(DATA_COMPATIBILITY)
        old_reader['reader_features'].remove(FEATURE)
        for body, rejected in [({}, False), ({'delivery_policy': 'auto'}, False),
            ({'delivery_policy': None, 'delivery_policy_inference': None}, False),
            ({'delivery_policy': 'draft'}, True), ({'delivery_policy': 'reviewed'}, True),
            ({'delivery_policy_inference': {'policy': 'draft'}}, True),
            ({'authorized_reference_ids': ['fAllowed']}, True)]:
            with self.subTest(body=body):
                with store.transaction() as db:
                    db.execute('DELETE FROM audit_tasks')
                    db.execute('INSERT INTO audit_tasks VALUES(?,?)', ('sTest', json.dumps(body)))
                if rejected:
                    with self.assertRaisesRegex(ValueError, '交付策略'):
                        check(self.root, old_reader)
                else:
                    self.assertTrue(check(self.root, old_reader)['compatible'])
                self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])


if __name__ == '__main__':
    unittest.main()
