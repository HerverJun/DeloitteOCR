"""Clipboard text stays literal even when downloads carry provenance archives."""

import asyncio
from pathlib import Path
import unittest
import zipfile

from ocr_workbench.service import create_app
from ocr_workbench.structure_store import decide_structure
import test_fusion_store as fusion_cases
import test_multimodal_store as multimodal_cases
import test_service_audit as service_cases
import test_structure_workflow as structure_cases


class ResultTextTests(unittest.TestCase):
    def fixture(self, case_type):
        case = case_type()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        return case

    def app(self, store):
        return create_app(Path(__file__).resolve().parents[1], store.root,
                          'clipboard-regression-token', start_queue=False, review_only=True)

    def route(self, app, path, method='GET'):
        return next(route.endpoint for route in app.routes
                    if route.path == path and method in route.methods)

    def assert_text(self, app, result_id, expected):
        before = app.state.store.result(result_id)
        response = self.route(app, '/api/results/{key}/text')(result_id)
        self.assertEqual(response.headers['content-type'], 'text/plain; charset=utf-8')
        self.assertEqual(response.body.decode('utf-8'), expected)
        self.assertEqual(app.state.store.result(result_id), before)

    def assert_archive(self, app, result_id, expected):
        response = self.route(app, '/api/export', 'POST')(
            {'result_ids': [result_id], 'format': 'txt'})
        try:
            self.assertTrue(zipfile.is_zipfile(response.path))
            with zipfile.ZipFile(response.path) as archive:
                text_files = [name for name in archive.namelist() if name.endswith('.txt')]
                self.assertEqual(len(text_files), 1)
                self.assertEqual(archive.read(text_files[0]).decode('utf-8').replace('\r\n', '\n'), expected)
                self.assertTrue(any(name.endswith('.json') for name in archive.namelist()))
        finally:
            asyncio.run(response.background())

    def test_saved_plain_text_preserves_unicode_tabs_lines_zeros_and_formulas(self):
        case = self.fixture(service_cases.ServiceAuditTests)
        app = self.app(case.store)
        expected = '甲方\t00001\nélodie\t-0.01\n=SUM(A1:A2)\nC:\\财务\\报表'
        case.store.save(case.result_id, {'text': expected, 'tables': []}, 0)
        self.assert_text(app, case.result_id, expected)
        self.assertEqual(case.store.result(case.result_id)['original']['text'], '0001')

    def test_fusion_preview_has_plain_text_and_retains_download_provenance(self):
        case = self.fixture(fusion_cases.FusionStoreTests)
        result_id, originals = case.create()
        app = self.app(case.store)
        expected = '项目\t金额\n项目甲\t00123'
        self.assert_text(app, result_id, expected)
        self.assert_archive(app, result_id, expected)
        self.assertIn(case.store.project_snapshot(case.project['id'])['images'][0]['selected_result'], originals)

    def test_adopted_structure_copies_current_table_without_source_json(self):
        case = self.fixture(structure_cases.StructureStoreTests)
        proposal = case.proposal()
        decide_structure(case.store, case.result_id, proposal['id'], case.body(proposal))
        app = self.app(case.store)
        expected = '项目\t金额\n收入\t001.00\n税额\t-0.01'
        self.assert_text(app, case.result_id, expected)
        self.assert_archive(app, case.result_id, expected)
        self.assertEqual(case.store.result(case.result_id)['original']['tables'][0]['rows'], 2)

    def test_multimodal_adoption_copies_literal_revision_without_review_report(self):
        case = self.fixture(multimodal_cases.MultimodalStoreTests)
        _, _, proposals = case.generated({'编号 00001': '编号 000001'})
        case.decide(next(p for p in proposals if p['status'] == 'replace'))
        app = self.app(case.store)
        expected = '编号 000001\n金额 -0.01\n名称 张三'
        self.assert_text(app, case.result_id, expected)
        self.assert_archive(app, case.result_id, expected)
        self.assertEqual(case.store.result(case.result_id)['original']['text'], case.raw['text'])


if __name__ == '__main__':
    unittest.main()
