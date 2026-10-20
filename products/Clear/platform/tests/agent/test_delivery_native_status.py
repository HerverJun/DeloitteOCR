import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workbench_platform.agent.delivery_jobs import NativeDeliveryBlocked, verify_native_jobs


class NativeDeliveryTests(unittest.TestCase):
    def test_pdf_terminal_stage_normalization_is_shared_by_delivery_reader(self):
        from workbench_platform.application_services.operations.jobs import read_reference
        reference = {'kind': 'result', 'workspace_id': 'workspace', 'application': 'ocr',
                     'object_id': 'page-result', 'parent_id': 'handoff', 'revision': 0}
        job = {'kind': 'job', 'workspace_id': 'workspace', 'application': 'ocr', 'object_id': 'request',
               'parent_id': 'handoff', 'native_id': 'document', 'revision': 1, 'coverage': {'kind': 'pdf_pages'}}
        ctx = SimpleNamespace(session_id='session', workspace_id='workspace', current=lambda: None,
            store=SimpleNamespace(rows=lambda *args: [{'result': json.dumps({'result_refs': [reference], 'job_refs': [job]})}]))
        native = {'submitted': True, 'stages': [{'page_number': 2, 'status': 'succeeded'},
                                              {'page_number': 5, 'status': 'succeeded'}]}
        with patch('workbench_platform.application_services.operations.pdf.status', return_value=native):
            assert read_reference(job, ctx)['status'] == 'succeeded'
            checks, limitations = verify_native_jobs([reference], ctx)
        self.assertEqual(checks[0]['status'], 'passed')
        self.assertEqual(limitations, [])

    def context(self):
        artifact = {'kind': 'artifact', 'workspace_id': 'workspace', 'application': 'data',
                    'object_id': 'bundle', 'parent_id': 'native-session', 'revision': 1}
        job = {'kind': 'job', 'workspace_id': 'workspace', 'application': 'data',
               'object_id': 'export', 'parent_id': 'native-session', 'revision': 4}
        unrelated = {**artifact, 'object_id': 'unrelated'}
        rows = [{'result': json.dumps({'status': 'success', 'artifact_refs': [artifact]})},
                {'result': json.dumps({'status': 'success', 'artifact_refs': [artifact], 'job_refs': [job]})},
                {'result': json.dumps({'status': 'success', 'artifact_refs': [unrelated],
                                      'job_refs': [{**job, 'object_id': 'other-export'}]})}]
        return artifact, SimpleNamespace(session_id='session', workspace_id='workspace', current=lambda: None,
            store=SimpleNamespace(rows=lambda *args: rows))

    def test_saved_success_and_available_artifact_do_not_override_current_native_state(self):
        artifact, ctx = self.context()
        with patch('workbench_platform.application_services.operations.jobs.read_reference', return_value={'status': 'running'}) as reader:
            with self.assertRaises(NativeDeliveryBlocked) as caught:
                verify_native_jobs([artifact], ctx)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.args[0]['object_id'], 'export')
        self.assertEqual(caught.exception.checks[0]['evidence']['native_status'], 'running')

    def test_unreadable_native_state_blocks_but_terminal_partial_retains_limitation(self):
        artifact, ctx = self.context()
        with patch('workbench_platform.application_services.operations.jobs.read_reference', side_effect=OSError('offline')):
            with self.assertRaises(NativeDeliveryBlocked):
                verify_native_jobs([artifact], ctx)
        with patch('workbench_platform.application_services.operations.jobs.read_reference', return_value={'status': 'partial'}):
            checks, limitations = verify_native_jobs([artifact], ctx)
        self.assertEqual(checks[0]['status'], 'failed')
        self.assertTrue(limitations)
        with patch('workbench_platform.application_services.operations.jobs.read_reference', return_value={'status': 'succeeded'}):
            checks, limitations = verify_native_jobs([artifact], ctx)
        self.assertEqual(checks[0]['status'], 'passed')
        self.assertEqual(limitations, [])

    def test_export_without_job_refs_uses_verified_bundle_origin_not_user_parent(self):
        artifact, ctx = self.context()
        ctx.store.rows = lambda *args: []
        artifact['parent_id'] = 'untrusted-parent'
        bundle = {'kind': 'ocr_result', 'platform_job_id': 'actual-ocr-job', 'handoff_id': 'verified-handoff'}
        with patch('workbench_platform.application_services.operations.jobs.read_reference', return_value={'status': 'running'}) as reader:
            with self.assertRaises(NativeDeliveryBlocked):
                verify_native_jobs([artifact], ctx, verified_objects=[bundle])
        self.assertEqual(reader.call_args.args[0]['object_id'], 'actual-ocr-job')
        self.assertEqual(reader.call_args.args[0]['parent_id'], 'verified-handoff')
