"""Real PDF runtime checks plus native/region merge and publication invariants."""
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image

from ocr_workbench.store import Store
from ocr_workbench.documents import Documents, WaitingUnlock, DocumentCancelled
from ocr_workbench.page_processing import (merge_page, complete_region_task, finalize_waiting_pages, map_region_result)
from ocr_workbench.coordinates import box_polygon

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'build/document-workflow/bundle'
FIXTURES = ROOT / 'build/document-workflow/fixtures'


class PageMergeTests(unittest.TestCase):
    def test_duplicate_agreement_and_ambiguous_overlap_are_distinguished(self):
        native = {'units': [{'text': '001234', 'polygon': box_polygon([10, 10, 50, 30]), 'native_index': 0}], 'flagged': []}
        context = ({'id': 'v', 'sha256': 'hash', 'width': 100, 'height': 100},
                   {'id': 'd', 'sha256': 'source'}, {'id': 'p', 'page_number': 1}, 'auto')
        data = {'blocks': [{'text': '001234', 'polygon': box_polygon([10, 10, 50, 30])}], 'engine': 'ppocr', 'tables': []}
        agreeing = merge_page(native, [data], *context)
        self.assertEqual(agreeing['text'], '001234')
        self.assertEqual(agreeing['document']['conflicts'], [])
        data['blocks'][0]['text'] = '991234'
        conflict = merge_page(native, [data], *context)
        self.assertIn('001234', conflict['text'])
        self.assertIn('991234', conflict['text'])
        self.assertEqual(len(conflict['document']['conflicts']), 1)

    def test_region_coordinates_map_without_modifying_input_or_fabricating_missing_boxes(self):
        raw = {'text': '001', 'blocks': [{'text': '001', 'polygon': box_polygon([1, 2, 20, 10])}],
               'tables': [{'cells': [{'polygon': None}]}]}
        mapped = map_region_result(raw, [100, 200, 400, 500], 'version')
        self.assertEqual(mapped['blocks'][0]['polygon'][0], [101, 202])
        self.assertEqual(raw['blocks'][0]['polygon'][0], [1, 2])
        self.assertIsNone(mapped['tables'][0]['cells'][0]['polygon'])


@unittest.skipUnless((BUNDLE / 'runtimes/pdf/python.exe').exists() and (FIXTURES / 'native.pdf').exists(), 'Run make_document_fixtures.py with the isolated PDF runtime first')
class PdfWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / '项目')
        self.project = self.store.project('PDF 测试')
        self.manager = Documents(self.store, BUNDLE)

    def tearDown(self):
        self.manager.stop()
        self.temporary.cleanup()

    def import_pdf(self, name='native.pdf', **kwargs):
        return self.manager.import_document(self.project['id'], name, FIXTURES / name, dpi=72, **kwargs)

    def test_lazy_thousand_page_import_materializes_no_images_and_pages_are_ordered(self):
        doc = self.import_pdf('thousand.pdf')
        self.assertEqual(doc['page_count'], 1000)
        self.assertEqual(self.store.rows('SELECT * FROM images'), [])
        pages = self.store.document_pages(doc['id'], offset=499, limit=2)
        self.assertEqual([p['page_number'] for p in pages], [500, 501])
        self.manager.ensure_rendered(pages[0]['id'])
        self.assertEqual(len(self.store.rows('SELECT * FROM images')), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM page_versions')), 1)

    def test_native_page_auto_processing_retains_ids_and_does_not_enqueue_gpu(self):
        doc = self.import_pdf()
        page = self.store.document_pages(doc['id'])[0]
        stage = self.manager.process(page['id'])
        self.manager.step()
        finished = self.store.one('document_stages', stage)
        self.assertEqual(finished['status'], 'succeeded', finished['error'])
        result_id = json.loads(finished['output'])['result_id']
        raw = self.store.result(result_id)['original']
        self.assertIn('00123456789012345678', raw['text'])
        self.assertEqual(raw['engine'], 'pdf-native')
        self.assertIsNone(raw['blocks'][0]['confidence'])
        self.assertEqual(self.store.rows('SELECT * FROM page_ocr_inputs'), [])
        self.assertEqual(self.store.one('pages', page['id'])['image_id'], self.store.one('tasks', self.store.result(result_id)['task_id'])['image_id'])

    def test_mixed_page_keeps_native_page_number_and_region_ocr_maps_to_full_page(self):
        doc = self.import_pdf('mixed.pdf')
        page = self.store.document_pages(doc['id'])[0]
        stage_id = self.manager.process(page['id'])
        self.manager.step()
        stage = self.store.one('document_stages', stage_id)
        self.assertEqual(stage['status'], 'waiting_gpu', stage['error'])
        image = self.store.one('images', self.store.one('pages', page['id'])['image_id'])
        version_id = image['active_version']
        while task := self.store.claim():
            complete_region_task(self.store, task, {'text': 'Scanned body 001234', 'engine': 'ppocr', 'tables': [],
                'blocks': [{'text': 'Scanned body 001234', 'polygon': box_polygon([5, 5, 100, 20]), 'confidence': .9}]})
        finalize_waiting_pages(self.manager)
        finished = self.store.one('document_stages', stage_id)
        raw = self.store.result(json.loads(finished['output'])['result_id'])['original']
        self.assertIn('Page 001', raw['text'])
        self.assertIn('Scanned body 001234', raw['text'])
        self.assertEqual(self.store.one('images', image['id'])['active_version'], version_id)
        self.assertEqual(len(self.store.rows('SELECT * FROM results')), 1)

    def test_encrypted_unlock_is_ephemeral_and_resume_keeps_completed_pages(self):
        doc = self.import_pdf('encrypted.pdf')
        self.assertEqual(doc['status'], 'waiting_unlock')
        self.assertEqual(doc['page_count'], 0)
        with self.assertRaises(WaitingUnlock):
            self.manager.unlock(doc['id'], 'wrong')
        password = 'temporary-fixture-secret'
        unlocked = self.manager.unlock(doc['id'], password)
        self.assertEqual(unlocked['page_count'], 1)
        page = self.store.document_pages(doc['id'])[0]
        self.manager = Documents(self.store, BUNDLE)
        key = self.manager.process(page['id'])
        self.manager.step()
        self.assertEqual(self.store.one('document_stages', key)['status'], 'waiting_unlock')
        self.manager.unlock(doc['id'], password)
        self.manager.action(doc['id'], 'resume')
        self.manager.step()
        self.assertEqual(self.store.one('document_stages', key)['status'], 'succeeded')
        for path in self.store.root.rglob('*'):
            if path.is_file() and path.suffix not in ('.pdf', '.png'):
                self.assertNotIn(password.encode(), path.read_bytes(), path.name)

    def test_rotated_cropboxes_native_geometry_matches_rendered_glyphs(self):
        for rotation in (0, 90, 180, 270):
            doc = self.import_pdf(f'crop-{rotation}.pdf')
            page = self.store.document_pages(doc['id'])[0]
            image = self.manager.ensure_rendered(page['id'])
            native = json.loads(self.store.file(self.store.one('pages', page['id'])['native_result']).read_text('utf-8'))['native']
            self.assertEqual(native['page_class'], 'native', (rotation, native['flagged']))
            with Image.open(self.store.file(self.store.one('versions', image['active_version'])['path'])) as rendered:
                for unit in native['units']:
                    from ocr_workbench.coordinates import bounds
                    crop = rendered.crop(bounds(unit['polygon'])).convert('L')
                    self.assertLess(crop.getextrema()[0], 200, (rotation, unit['text']))

    def test_old_invisible_ocr_is_flagged_and_blank_page_keeps_number(self):
        doc = self.import_pdf('old-ocr.pdf')
        page = self.store.document_pages(doc['id'])[0]
        self.manager.ensure_rendered(page['id'])
        native = json.loads(self.store.file(self.store.one('pages', page['id'])['native_result']).read_text('utf-8'))['native']
        self.assertTrue(any(u['reason'] == 'old_invisible_ocr' for u in native['flagged']))
        self.assertFalse(any('WRONG' in u['text'] for u in native['units']))
        blank = self.import_pdf('blank.pdf')
        blank_page = self.store.document_pages(blank['id'])[0]
        key = self.manager.process(blank_page['id'])
        self.manager.step()
        self.assertEqual(self.store.one('document_stages', key)['status'], 'succeeded')
        self.assertEqual(self.store.one('pages', blank_page['id'])['status'], 'blank')
        self.assertEqual(self.store.one('pages', blank_page['id'])['page_number'], 1)

    def test_tiff_is_lazy_and_corrupt_pdf_has_no_partial_document(self):
        doc = self.import_pdf('multipage.tiff')
        self.assertEqual(doc['page_count'], 2)
        self.assertEqual(self.store.rows('SELECT * FROM images'), [])
        second = self.store.document_pages(doc['id'])[1]
        image = self.manager.ensure_rendered(second['id'])
        version = self.store.one('versions', image['active_version'])
        self.assertEqual((version['width'], version['height']), (120, 80))
        with self.assertRaises(ValueError):
            self.import_pdf('broken.pdf')
        self.assertEqual(len(self.store.rows('SELECT * FROM documents')), 1)

    @unittest.skipUnless((FIXTURES/'chinese-two-column.pdf').exists(),'Additional PDF fixtures required')
    def test_chinese_two_column_text_and_damaged_unicode_are_distinguished(self):
        doc=self.import_pdf('chinese-two-column.pdf');page=self.store.document_pages(doc['id'])[0]
        self.manager.ensure_rendered(page['id'])
        native=json.loads(self.store.file(self.store.one('pages',page['id'])['native_result']).read_text('utf-8'))['native']
        text=''.join(u['text'] for u in native['units'])
        for value in ('中文合同','左栏金额','右栏日期','2026-09-13','00001234567890123456'):self.assertIn(value,text)
        doc=self.import_pdf('damaged-unicode.pdf');page=self.store.document_pages(doc['id'])[0]
        self.manager.ensure_rendered(page['id'])
        damaged=json.loads(self.store.file(self.store.one('pages',page['id'])['native_result']).read_text('utf-8'))['native']
        self.assertTrue(any(u['reason']=='unmapped_characters' for u in damaged['flagged']))
        self.assertTrue(damaged['coverage_regions'])


if __name__ == '__main__':
    unittest.main()
