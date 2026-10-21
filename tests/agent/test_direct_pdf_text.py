"""Deterministic PDF action, explicit pages, durable acceptance and fixed outputs."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.agent.store import AgentStore, Conflict
from workbench_platform.application_services.pdf_text import PdfTextRequest, submit, drive
from workbench_platform.application_services.operations import pdf


class DirectPdfTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=r'F:\OCR\output\architecture-revision-20261017\temp')
        self.root = Path(self.tmp.name)
        self.store = AgentStore(self.root / 'agent')
        self.session = self.store.create_session('workspace', 'PDF', ['pdf'], 'session')
        self.asset = SimpleNamespace(media_type='application/pdf', revision=1, blob_hash='a' * 64)
        self.task = {'scope_revision': 1, 'manual': False}
        self.scheduled = []
        self.services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda identifier: SimpleNamespace(name='project')),
            asset_library=(SimpleNamespace(get=lambda *args: self.asset),
            SimpleNamespace(create=lambda workspace, items, asset_set_id: SimpleNamespace(model_dump=lambda **kwargs: {'asset_set_id': asset_set_id, 'revision': 1})), None))
        self.runtime = SimpleNamespace(store=self.store, services=self.services,
            tasks=SimpleNamespace(get=lambda sid: self.task, record_goal=lambda *args: None),
            schedule=lambda run, state: self.scheduled.append(run), activity=lambda *args, **kwargs: None)
        self.services.agent = self.runtime

    def tearDown(self):
        self.tmp.cleanup()

    async def make(self, **kwargs):
        return await submit(self.runtime, self.session['id'], PdfTextRequest(request_id='request', page_numbers=[2, 5], **kwargs))

    async def test_page_set_is_fixed_and_replay_ignores_later_scope_without_new_submission(self):
        run = await self.make(mode='native', expected_scope_revision=1)
        self.task['scope_revision'] = 2
        same = await self.make(mode='native', expected_scope_revision=1)
        self.assertEqual(run['id'], same['id'])
        self.assertEqual(self.scheduled, [run['id']])
        self.assertEqual(run['driver'], 'pdf-text-v1')
        self.assertEqual(json.loads(run['driver_state'])['request']['page_numbers'], [2, 5])
        with self.assertRaises(Conflict):
            await self.make(mode='ocr', expected_scope_revision=1)
        with self.assertRaises(ValueError):
            PdfTextRequest(request_id='invalid', page_numbers=[2, 2])
        with self.assertRaises(ValueError):
            PdfTextRequest(request_id='oversized', page_numbers=list(range(1, 102)))
        self.assertEqual(self.store.rows('SELECT * FROM model_requests'), [])

    async def test_multiple_files_and_non_pdf_are_rejected_before_any_run(self):
        self.asset.media_type = 'image/png'
        with self.assertRaises(ValueError):
            await self.make()
        self.asset.media_type = 'application/pdf'
        self.store.create_session('workspace', 'two', ['pdf', 'other'], 'two')
        with self.assertRaises(ValueError):
            await submit(self.runtime, 'two', PdfTextRequest(request_id='two', page_numbers=[2]))
        self.assertEqual(self.store.rows('SELECT * FROM runs'), [])

    async def exercise(self, mode, *, page_count=5, lost_reply=False, native_stopped=False):
        run = await self.make(mode=mode)
        submitted, accepted = [], {}
        job = {'kind': 'job', 'workspace_id': 'workspace', 'object_id': 'native-request', 'native_id': 'doc',
               'application': 'ocr', 'parent_id': 'handoff', 'revision': 1, 'coverage': {'kind': 'pdf_pages'}}
        def process(args, ctx):
            submitted.append(args.model_dump())
            value = AgentToolResult(status='pending', summary='accepted', job_refs=[job]).model_dump(mode='json')
            accepted[ctx.operation_id] = value
            if lost_reply:
                raise OSError('native acceptance response lost')
            return value
        def read(args, ctx):
            return AgentToolResult(status='success', summary='page', result_refs=[{
                'kind': 'result', 'workspace_id': 'workspace', 'object_id': 'result' + str(args.page_number),
                'application': 'ocr', 'parent_id': 'handoff', 'revision': 'fixed-v1', 'coverage': {'page_number': args.page_number},
            }]).model_dump(mode='json')
        stages = [{'page_number': page, 'status': 'succeeded', 'error': None} for page in (2, 5)]
        restored = []
        def prepare_observation(reference, context):
            context.current()
            self.assertEqual(reference, AgentRef.model_validate(job).model_dump(mode='json'))
            restored.append(reference)
        def progress_for(reference, context):
            if native_stopped and not restored:
                return {'status': 'unknown', 'observation': 'unavailable', 'reason': 'not_ready'}
            return {'status': 'succeeded', 'stage': '已完成', 'observation': 'native'}
        async def settle(result, state, *, context):
            self.assertTrue(not native_stopped or restored, 'Original native service was never restored')
            return pdf.settle(job, context)
        self.runtime.settle = settle
        def publish(ctx, reference):
            return {'artifact_ref': {'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': 'txt' + reference['object_id'], 'revision': 1},
                    'readable_text': {'reference': reference, 'text': 'PDF 页 ' + str(reference['coverage']['page_number']), 'download_url': '/fixed.txt'},
                    'delivery': {'bundle_id': 'txt' + reference['object_id'], 'title': 'PDF 原文', 'files': [{'name': 'ocr-text.txt', 'download_url': '/fixed.txt'}]}}
        with patch('workbench_platform.application_services.native.prepare', return_value={'link_id': 'link'}), \
             patch.object(pdf, 'inspect', return_value=AgentToolResult(status='success', summary='pages', data={
                 'handoff_id': 'handoff', 'documents': [{'asset_id': 'pdf', 'document_id': 'doc', 'page_count': page_count}]}).model_dump()), \
             patch.object(pdf, 'process', side_effect=process), \
             patch.object(pdf, 'lookup', side_effect=lambda args, ctx, **kwargs: accepted.get(ctx.operation_id)), \
             patch.object(pdf, 'status', return_value={'submitted': True, 'stages': stages, 'parameters': {'page_numbers': [2, 5]}}), \
             patch.object(pdf, 'read', side_effect=read), \
             patch('workbench_platform.application_services.job_observations.progress_for', side_effect=progress_for), \
             patch('workbench_platform.application_services.job_lifecycle.prepare_observation', side_effect=prepare_observation), \
             patch('workbench_platform.artifacts.text_outputs.publish_text_result', side_effect=publish):
            if page_count < 5:
                with self.assertRaisesRegex(ValueError, '超出真实'):
                    await drive(self.runtime, run['id'])
                self.assertEqual(submitted, [])
                return
            await drive(self.runtime, run['id'])
        self.assertEqual(len(submitted), 1)
        self.assertEqual((submitted[0]['page_numbers'], submitted[0]['mode']), ([2, 5], mode))
        self.assertEqual(self.store.run(run['id'])['status'], 'succeeded')
        events = self.store.rows('SELECT * FROM events WHERE run_id=?', (run['id'],))
        payloads = [json.loads(row['payload']) for row in events if row['kind'] == 'delivery']
        self.assertEqual(len(payloads), 1)
        self.assertEqual([ref['coverage']['page_number'] for ref in payloads[0]['delivery']['result_refs']], [2, 5])
        self.assertEqual(payloads[0]['delivery']['data']['mode'], mode)
        self.assertEqual(len(payloads[0]['delivery']['data']['readable_text']), 2)
        self.assertTrue(any(row['kind'] == 'job_progress' and json.loads(row['payload'])['status'] == 'succeeded' for row in events))
        self.assertEqual(self.store.rows('SELECT * FROM model_requests'), [])
        self.assertEqual(len(restored), int(native_stopped))

    async def test_native_text_uses_only_selected_pages_without_ocr_mode(self):
        await self.exercise('native')

    async def test_auto_preserves_native_selection_and_lost_acceptance_uses_lookup(self):
        await self.exercise('auto', lost_reply=True)

    async def test_unavailable_native_service_is_restored_for_original_accepted_pdf_job(self):
        await self.exercise('ocr', lost_reply=True, native_stopped=True)

    async def test_out_of_range_page_is_rejected_before_native_processing(self):
        await self.exercise('auto', page_count=4)

    async def test_more_than_fifty_selected_pages_keep_all_fixed_result_references(self):
        ref = {'kind': 'job', 'workspace_id': 'workspace', 'object_id': 'request', 'revision': 1, 'application': 'ocr', 'parent_id': 'handoff', 'native_id': 'doc'}
        stages = [{'page_number': page, 'status': 'succeeded', 'error': None} for page in range(1, 61)]
        def read(args, ctx):
            return {'result_refs': [{'kind': 'result', 'workspace_id': 'workspace', 'object_id': 'p' + str(args.page_number), 'revision': 1}], 'warnings': []}
        with patch.object(pdf, 'status', return_value={'submitted': True, 'stages': stages, 'parameters': {'page_numbers': list(range(1, 61))}}), patch.object(pdf, 'read', side_effect=read):
            result = pdf.settle(ref, SimpleNamespace())
        self.assertEqual(len(result['result_refs']), 60)
        self.assertEqual(len(result['data']['pages']), 50)
        self.assertEqual(result['data']['pages_total'], 60)
        self.assertTrue(result['data']['pages_truncated'])
