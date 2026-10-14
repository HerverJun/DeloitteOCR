import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from PIL import Image
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image, transform, save_version
from ocr_workbench.coordinates import (pdf_transform, point, inverse, bounds, polygon,
                                       box_polygon, operation_transform)
from ocr_workbench.document_store import structure_fingerprint


class CoordinateTests(unittest.TestCase):
    def test_cropbox_rotation_round_trip_and_viewport_corners(self):
        crop = [30, 50, 230, 350]
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                matrix = pdf_transform(crop, rotation, 144)
                corners = polygon(matrix, box_polygon(crop))
                self.assertEqual(bounds(corners), [0, 0, 600, 400] if rotation % 180 else [0, 0, 400, 600])
                for original in ([30, 50], [123, 234], [230, 350]):
                    restored = point(inverse(matrix), point(matrix, original))
                    for a, b in zip(restored, original):
                        self.assertAlmostEqual(a, b)
        self.assertEqual(point(pdf_transform(crop, 90, 72), [30, 50]), [0, 0])

    def test_invalid_nonfinite_and_nonlinear_transforms(self):
        for dpi in (float('nan'), float('inf'), 0, 1201, True):
            with self.assertRaises(ValueError):
                pdf_transform([0, 0, 100, 100], dpi=dpi)
        with self.assertRaises(ValueError):
            inverse([0] * 9)
        self.assertIsNone(operation_transform({'kind': 'dewarp'}, 100, 100))
        self.assertEqual(point(operation_transform({'kind': 'crop', 'box': [3.4, 7.2, 20, 30]}, 100, 100), [3, 7]), [0, 0])

    def test_geometry_identity_ignores_text_but_detects_topology(self):
        edit = {'tables': [{'rows': 2, 'columns': 1, 'cells': [
            {'row': 0, 'column': 0, 'row_span': 1, 'column_span': 1, 'text': '001'},
            {'row': 1, 'column': 0, 'row_span': 1, 'column_span': 1, 'text': '002'}]}]}
        changed = copy.deepcopy(edit)
        changed['tables'][0]['cells'][0]['text'] = '修订'
        self.assertEqual(structure_fingerprint(edit), structure_fingerprint(changed))
        changed['tables'][0]['cells'][0]['row_span'] = 2
        self.assertNotEqual(structure_fingerprint(edit), structure_fingerprint(changed))


class DocumentFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / '项目')
        self.project = self.store.project('文档测试')

    def tearDown(self):
        self.temp.cleanup()

    def imported(self):
        path = self.root / 'source.png'
        Image.new('RGB', (100, 80), 'white').save(path)
        return add_image(self.store, self.project['id'], path.name, path)

    def test_single_image_links_and_derived_versions_keep_identity(self):
        photo = self.imported()
        original = self.store.file(photo['original_path']).read_bytes()
        page = self.store.page_for_version(photo['active_version'])
        self.assertEqual(page['document_id'], photo['id'])
        derived = transform(self.store, photo['active_version'], {'kind': 'rotate', 'degrees': 90})
        linked = self.store.page_for_version(derived['id'])
        self.assertEqual(linked['id'], page['id'])
        self.assertEqual(self.store.file(photo['original_path']).read_bytes(), original)
        documents = self.store.project_snapshot(self.project['id'])['documents']
        self.assertEqual(len(documents), 1)
        self.assertEqual(self.store.document_pages(documents[0]['id'])[0]['active_version'], derived['id'])

    def test_regions_bind_version_and_reject_cross_page_and_outside(self):
        one, two = self.imported(), self.imported()
        version = one['active_version']
        points = [[1, 1], [30, 1], [30, 25], [1, 25]]
        initial = self.store.project_revision(self.project['id'])
        key = self.store.add_region(one['id'], version, 'table', points, 'manual')
        self.assertGreater(self.store.project_revision(self.project['id']), initial)
        self.assertEqual(self.store.page_regions(one['id'])[0]['id'], key)
        for page_id, poly in [(two['id'], points), (one['id'], [[0, 0], [200, 0], [100, 10]])]:
            with self.assertRaises(ValueError):
                self.store.add_region(page_id, version, 'table', poly, 'manual')
        rotated = transform(self.store, version, {'kind': 'rotate', 'degrees': 90})
        self.assertEqual(self.store.page_regions(one['id']), [])
        self.assertEqual(len(self.store.page_regions(one['id'], version)), 1)
        self.assertEqual(self.store.page_for_version(rotated['id'])['id'], one['id'])

    def test_nonlinear_version_does_not_reuse_pdf_mapping(self):
        photo = self.imported()
        with self.store.transaction() as db:
            db.execute("UPDATE page_versions SET pdf_to_pixel=?,mapping_status='exact' WHERE version_id=?",
                       (json.dumps(pdf_transform([0, 0, 100, 80], dpi=72)), photo['active_version']))
        cropped = transform(self.store, photo['active_version'], {'kind': 'crop', 'box': [10, 10, 80, 60]})
        page = self.store.page_for_version(cropped['id'])
        self.assertEqual(point(json.loads(page['pdf_to_pixel']), [10, 70]), [0, 0])
        warped = save_version(self.store, cropped, Image.new('RGB', (70, 50)), {'kind': 'dewarp'})
        relation = self.store.page_for_version(warped['id'])
        self.assertEqual(relation['mapping_status'], 'nonlinear')
        self.assertIsNone(relation['pdf_to_pixel'])
        enhanced = transform(self.store, warped['id'], {'kind': 'contrast', 'factor': 1.2})
        self.assertEqual(self.store.page_for_version(enhanced['id'])['mapping_status'], 'nonlinear')

    def test_stages_resume_without_repeating_success_and_late_commit_is_rejected(self):
        photo = self.imported()
        stage = self.store.enqueue_document_stage(photo['id'], 'process', {'mode': 'auto'})
        self.assertEqual(self.store.claim_document_stage()['id'], stage)
        self.assertTrue(self.store.finish_document_stage(stage, {'ok': True}))
        self.assertEqual(self.store.enqueue_document_stage(photo['id'], 'process', {'mode': 'auto'}), stage)
        self.assertIsNone(self.store.claim_document_stage())
        other = self.store.enqueue_document_stage(photo['id'], 'process', {'mode': 'ocr'})
        self.store.claim_document_stage()
        self.store.recover_document_stages()
        self.assertEqual(self.store.one('document_stages', other)['status'], 'interrupted')
        self.assertFalse(self.store.finish_document_stage(other, {'late': True}))
        self.assertEqual(self.store.enqueue_document_stage(photo['id'], 'process', {'mode': 'ocr'}), other)
        self.assertEqual(self.store.claim_document_stage()['id'], other)
        with self.assertRaises(ValueError):
            self.store.enqueue_document_stage(photo['id'], 'process', {'password': 'never persist'})

    def test_schema8_migration_preserves_ids_paths_and_backs_up_before_write(self):
        legacy = self.root / 'legacy'
        with patch('ocr_workbench.store.SCHEMA_VERSION', 8):
            old = Store(legacy)
        with old.transaction() as db:
            db.execute("INSERT INTO projects VALUES('p','legacy','then','then')")
            db.execute("INSERT INTO images VALUES('i','p','original.png','unchanged/original.png','hash','v','then')")
            db.execute("INSERT INTO versions VALUES('v','i',NULL,'unchanged/v.png',100,100,'vh','{}','then')")
        migrated = Store(legacy)
        self.assertEqual(migrated.one('images', 'i')['original_path'], 'unchanged/original.png')
        self.assertEqual(migrated.document_pages('i')[0]['image_id'], 'i')
        self.assertEqual(migrated.page_for_version('v')['id'], 'i')
        with closing(sqlite3.connect(migrated.migration_backup)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 8)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='documents'").fetchone())

    def test_v9_failure_rolls_back_new_tables_and_rejects_future_schema(self):
        legacy = self.root / 'broken'
        with patch('ocr_workbench.store.SCHEMA_VERSION', 8):
            Store(legacy)
        from ocr_workbench.document_store import migrate_v9
        def fail(db):
            migrate_v9(db)
            raise OSError('disk fault after schema changes')
        with patch('ocr_workbench.store.migrate_v9', fail), self.assertRaises(OSError):
            Store(legacy)
        with closing(sqlite3.connect(legacy / 'workbench.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 8)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='documents'").fetchone())
        with patch('ocr_workbench.store.SCHEMA_VERSION', 8), self.assertRaises(ValueError):
            Store(self.store.root)


if __name__ == '__main__':
    unittest.main()
