"""Catalog roles, native identity, and unavailable model conditions."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from workbench_platform.application_services.native import model_projection


class ModelCatalogTests(TestCase):
    def context(self, classifier_models=None, recognition=True):
        links = [SimpleNamespace(app_id=family, link_id='link_' + family,
                                 module_project_id='project_' + family)
                 for family in ('ocr', 'classification')]
        classifier = SimpleNamespace(project=lambda: {'labels': [{'label_id': 'invoice'}]},
            models=lambda: tuple(classifier_models or []))
        engine = SimpleNamespace(
            capabilities=lambda: SimpleNamespace(recognition=recognition, reason=None if recognition else 'review_only'),
            _request=lambda *args: {'engines': {
                'ppocr': {'models': ['PP-OCRv6_medium_rec'], 'package_id': 'builtin',
                          'route': 'native CUDA', 'capabilities': {'text': True, 'tables': False}},
                'missing_engine': {'ready': False, 'reason': 'model missing', 'capabilities': {'text': True}},
            }})
        services = SimpleNamespace(workspace_store=SimpleNamespace(list_links=lambda _: links),
            job_service=SimpleNamespace(_context=lambda workspace, family, link: family,
                _adapter=lambda family: engine if family == 'ocr' else classifier))
        return SimpleNamespace(workspace_id='workspace', services=services)

    def test_native_versions_roles_and_missing_models_are_distinct(self):
        ctx = self.context([{'id': 'model_one', 'metadata': {'model_version_id': 'model_one',
            'task_type': 'single_label', 'provenance': {'model_onnx_sha256': 'a' * 64}},
            'package_path': 'must-not-enter-model-catalog'}])
        with patch('workbench_platform.application_services.native.availability', return_value={'state': 'ready', 'reason': ''}):
            rows = model_projection(ctx)
        indexed = {row['id']: row for row in rows if row['id']}
        self.assertEqual(indexed['ppocr']['role'], 'ocr_engine')
        self.assertEqual(indexed['ppocr']['supported_tasks'], ['text'])
        self.assertEqual(indexed['ppocr']['version_ref']['package_id'], 'builtin')
        self.assertFalse(indexed['missing_engine']['ready'])
        model = indexed['model_one']
        self.assertEqual(model['role'], 'classifier')
        self.assertEqual(model['version_ref']['model_onnx_sha256'], 'a' * 64)
        self.assertEqual(model['project_id'], 'project_classification')
        self.assertNotIn('package_path', model)
        self.assertFalse(next(row for row in rows if row['role'] == 'visual_review')['ready'])

    def test_empty_classifier_and_review_only_ocr_never_claim_ready(self):
        with patch('workbench_platform.application_services.native.availability', return_value={'state': 'ready', 'reason': ''}):
            rows = model_projection(self.context(recognition=False))
        self.assertTrue(rows)
        self.assertTrue(all(not row['ready'] for row in rows))
        self.assertIn('尚无训练模型', next(row for row in rows if row['role'] == 'classifier')['reason'])

    def test_unprepared_catalog_does_not_start_services_or_read_adapters(self):
        ctx = self.context()
        ctx.services.job_service._adapter = lambda _: self.fail('Unprepared catalog must not query adapters')
        with patch('workbench_platform.application_services.native.availability', return_value={'state': 'preparable', 'reason': 'not started'}):
            rows = model_projection(ctx)
        self.assertEqual({row['role'] for row in rows}, {'ocr_engine', 'classifier', 'visual_review'})
        self.assertTrue(all(not row['ready'] for row in rows))
