"""Agent PDF output uses actual native PDF runtime and existing preflight."""
import json
import subprocess
import time
from types import SimpleNamespace
import unittest

import test_agent_policy
import test_document_processing
from ocr_workbench.agent.artifacts import Artifacts
from ocr_workbench.agent.contracts import ExportResults
from ocr_workbench.agent.operations import Operations
from ocr_workbench.agent.policy import PolicyDenied
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.documents import Documents


@unittest.skipUnless((test_document_processing.BUNDLE / 'runtimes/pdf/python.exe').exists(), 'Real isolated PDF runtime required')
class PdfArtifactTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def prepare_pdf(self):
        documents = Documents(self.store, test_document_processing.BUNDLE)
        self.addCleanup(documents.stop)
        doc = documents.import_document(self.project, 'native.pdf', test_document_processing.FIXTURES / 'native.pdf', 72)
        page = self.store.document_pages(doc['id'])[0]
        stage = documents.process(page['id'], 'native')
        documents.step()
        finished = self.store.one('document_stages', stage)
        self.assertEqual(finished['status'], 'succeeded', finished['error'])
        result_id = json.loads(finished['output'])['result_id']
        result = self.store.result(result_id)
        version_id = result['original']['project_image_version']
        args = ExportResults(source='explicit_results', format='pdf', results=[{'result_id': result_id, 'revision': 0, 'version_id': version_id}])
        self.policy.grant(self.project, 'scoped_new_artifact', {'document_ids': [doc['id']], 'page_ids': [page['id']],
            'version_ids': [version_id], 'result_ids': [result_id], 'format': 'pdf', 'partial_policy': 'ask'},
            source='user_request', expires=time.time() + 60, run_id=self.run['id'])
        self.agent.record_calls(self.project, self.run['id'], 1, 0, [{'call_id': 'pdf', 'tool': 'export_results', 'arguments': args.model_dump(exclude_none=True)}])
        artifacts = Artifacts(self.agent, ApplicationServices(self.store, documents, SimpleNamespace(guard=self.store.file_lock)), Operations(self.agent, self.policy))
        return artifacts, args, {'run': self.run, 'generation': 1, 'step': 0, 'call_id': 'pdf'}, page

    def test_native_pdf_artifact_preserves_text_with_real_preflight(self):
        artifacts, args, context, page = self.prepare_pdf()
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        exported = artifacts.verify(self.project, key)
        self.assertEqual(exported.suffix, '.pdf')
        code = "import pypdfium2 as pdfium,sys; p=pdfium.PdfDocument(sys.argv[1]); print('\\n'.join(p[i].get_textpage().get_text_range() for i in range(len(p)))); p.close()"
        completed = subprocess.run([str(test_document_processing.BUNDLE / 'runtimes/pdf/python.exe'), '-B', '-c', code, str(exported)], check=True, capture_output=True)
        self.assertIn(b'00123456789012345678', completed.stdout)
        manifest = json.loads(artifacts.get(self.project, key)['manifest'])
        self.assertEqual(manifest['sources'][0]['page_id'], page['id'])
        self.assertTrue(manifest['sources'][0]['is_adopted'])
        self.assertEqual(manifest['sources'][0]['engine'], 'pdf-native')

    def test_missing_text_geometry_fails_preflight_without_ready_artifact(self):
        artifacts, args, context, _ = self.prepare_pdf()
        result_id = args.results[0].result_id
        self.store.save(result_id, {'text': 'Entirely new unmapped content 918273', 'tables': []}, 0)
        revised = args.model_copy(update={'results': [args.results[0].model_copy(update={'revision': 1})]})
        self.agent.record_calls(self.project, self.run['id'], 1, 1, [{'call_id': 'pdf-unmapped', 'tool': 'export_results', 'arguments': revised.model_dump(exclude_none=True)}])
        with self.assertRaises(PolicyDenied) as error:
            artifacts.export(revised, {**context, 'step': 1, 'call_id': 'pdf-unmapped'})
        self.assertEqual(error.exception.code, 'business_failed')
        self.assertIn('预检', str(error.exception))
        self.assertFalse(self.store.rows("SELECT id FROM agent_artifacts WHERE state='ready'"))
        self.assertEqual(self.store.rows('SELECT state FROM agent_artifacts')[0]['state'], 'failed')
