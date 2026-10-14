"""Synthetic PDF and lifecycle failures, independent of public document content."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ocr_workbench.atomic_files import read_json, write_json
from ocr_workbench.documents import Documents, DocumentCancelled, WaitingUnlock
from ocr_workbench.store import Store, Conflict
from ocr_workbench.document_review import document_review_queue
from ocr_workbench.page_processing import complete_region_task, finalize_waiting_pages
from ocr_workbench.structure_store import structure_view
from ocr_workbench.table_tool import enqueue, tool_identity

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = Path(os.environ.get('OCR_LIFECYCLE_BUNDLE', str(ROOT/'build/document-workflow/bundle')))
FIXTURES = ROOT/'build/document-workflow/fixtures'


@unittest.skipUnless((BUNDLE/'runtimes/pdf/python.exe').is_file() and (FIXTURES/'native.pdf').is_file(), 'Isolated PDF runtime and generated fixtures required')
class TableToolLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'workspace')
        self.manager = Documents(self.store, BUNDLE)
        self.project = self.store.project('Generic tool lifecycle')

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def import_page(self, name='native.pdf'):
        doc = self.manager.import_document(self.project['id'], name, FIXTURES/name, dpi=72)
        page = self.store.document_pages(doc['id'])[0]
        self.manager.ensure_rendered(page['id'])
        return doc, self.store.one('pages', page['id'])

    def result(self, stage):
        row = self.store.one('document_stages', stage)
        self.assertEqual(row['status'], 'succeeded', row['error'])
        return self.store.result(json.loads(row['output'])['result_id'])

    def empty_prediction(self):
        tool = tool_identity()
        return {'component': 'pdfplumber', 'tool_version': tool['version'], 'pdfplumber_tables': [],
                'settings': {}, 'settings_sha256': tool['config_sha256']}

    def test_base_render_never_runs_auxiliary_tool(self):
        with patch.object(self.manager.cpu, 'call', wraps=self.manager.cpu.call) as call:
            _, page = self.import_page()
        self.assertEqual([c.args[0]['operation'] for c in call.call_args_list], ['inspect', 'render'])
        native = read_json(self.store.file(page['native_result']))['native']
        self.assertTrue(native['units'])
        self.assertNotIn('table_structure', native)

    def test_auxiliary_timeout_and_crash_preserve_base_result_and_show_review_issue(self):
        for error in (TimeoutError('injected timeout'), ValueError('injected child process failure')):
            with self.subTest(error=str(error)):
                doc, page = self.import_page()
                stage = self.manager.process(page['id'], 'native')
                with patch.object(self.manager.cpu, 'call', side_effect=error):
                    self.manager.step()
                result = self.result(stage)
                self.assertIn('00123456789012345678', result['edited']['text'])
                self.assertTrue(self.store.file(page['native_result']).is_file())
                self.assertEqual(structure_view(self.store, result['id'])['table_tool']['state'], 'failed')
                self.assertIn('table_tool', [t['kind'] for t in document_review_queue(self.store, doc['id'])['tasks']])

    def test_service_stop_during_tool_remains_interrupted_without_publishing(self):
        _, page = self.import_page()
        stage = self.manager.process(page['id'], 'native')
        def stop(*args, **kwargs):
            self.manager.stopping.set()
            raise DocumentCancelled()
        with patch.object(self.manager.cpu, 'call', side_effect=stop):
            self.manager.step()
        self.assertEqual(self.store.one('document_stages', stage)['status'], 'interrupted')
        self.assertEqual(self.store.rows('SELECT id FROM results'), [])
        self.assertTrue(self.store.file(page['native_result']).is_file())

    def test_stop_after_tool_returns_also_prevents_publication(self):
        _, page = self.import_page()
        stage = self.manager.process(page['id'], 'native')
        def stop(*args, **kwargs):
            self.manager.stopping.set()
            return self.empty_prediction()
        with patch.object(self.manager.cpu, 'call', side_effect=stop):
            self.manager.step()
        self.assertEqual(self.store.one('document_stages', stage)['status'], 'interrupted')
        self.assertEqual(self.store.rows('SELECT id FROM results'), [])

    def test_waiting_unlock_propagates_without_failure_or_partial_result(self):
        _, page = self.import_page()
        stage = self.manager.process(page['id'], 'native')
        with patch.object(self.manager.cpu, 'call', side_effect=WaitingUnlock('password required')):
            self.manager.step()
        self.assertEqual(self.store.one('document_stages', stage)['status'], 'waiting_unlock')
        self.assertEqual(self.store.rows('SELECT id FROM results'), [])

    def test_encrypted_auxiliary_retry_waits_for_password_and_resumes(self):
        secret = 'temporary-fixture-secret'
        doc = self.manager.import_document(self.project['id'], 'encrypted.pdf', FIXTURES/'encrypted.pdf', dpi=72, password=secret)
        page = self.store.document_pages(doc['id'])[0]
        stage = self.manager.process(page['id'], 'native'); self.manager.step()
        result = self.result(stage)
        self.manager.passwords.clear()
        job = enqueue(self.manager, result['id'], result['revision'])['stage_id']
        self.manager.step()
        self.assertEqual(self.store.one('document_stages', job)['status'], 'waiting_unlock')
        self.assertEqual(structure_view(self.store, result['id'])['table_tool']['state'], 'waiting_unlock')
        self.manager.unlock(doc['id'], secret)
        self.manager.action(doc['id'], 'resume'); self.manager.step()
        self.assertEqual(self.store.one('document_stages', job)['status'], 'succeeded')
        self.assertEqual(self.store.result(result['id']), result)
        for file in self.store.root.rglob('*.json'):
            self.assertNotIn(secret.encode(), file.read_bytes())

    def test_real_subprocess_timeout_retains_published_native_page(self):
        _, page = self.import_page()
        marker = Path(self.temp.name)/'tool-entered.txt'
        script = Path(self.temp.name)/'slow-tool.py'
        script.write_text('import time\nfrom pathlib import Path\nfrom ocr_workbench import pdf_worker,pdf_tables\n'
            'def slow(*args,**kwargs):\n    Path('+repr(str(marker))+').write_text("entered")\n    time.sleep(15)\n'
            'pdf_tables.extract_tables=slow\npdf_worker.main()\n', encoding='utf-8')
        popen, call = subprocess.Popen, self.manager.cpu.call
        def injected(args, *other, **kwargs):
            args = list(args)
            if '-c' in args and 'pdf_worker' in args[args.index('-c')+1]:
                args[args.index('-c')+1] = 'import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[2],run_name="__main__")'
                args.append(str(script))
            return popen(args, *other, **kwargs)
        stage = self.manager.process(page['id'], 'native')
        with patch('ocr_workbench.documents.subprocess.Popen', side_effect=injected), patch.object(
                self.manager.cpu, 'call', side_effect=lambda request,cancelled:call(request,cancelled,timeout=3)):
            self.manager.step()
        self.assertTrue(marker.is_file())
        result = self.result(stage)
        self.assertIn('表格提取超时', result['original']['document']['table_structure_error'])
        self.assertTrue(self.store.file(page['native_result']).is_file())
        self.assertIn('00123456789012345678', result['edited']['text'])

    def test_cache_reuse_config_change_and_legacy_snapshot(self):
        _, page = self.import_page()
        path = self.store.file(page['native_result'])
        original = read_json(path)
        original['native']['table_structure'] = dict(self.empty_prediction(), settings_sha256='old')
        write_json(path, original)
        call_real = self.manager.cpu.call
        with patch.object(self.manager.cpu, 'call', wraps=call_real) as call:
            first = self.manager.process(page['id'], 'native'); self.manager.step()
            second = self.manager.process(page['id'], 'native', force=True); self.manager.step()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.result(first)['original']['document']['table_tool']['state'], 'empty')
        self.assertEqual(self.result(second)['original']['document']['table_structure']['settings_sha256'], tool_identity()['config_sha256'])
        changed = dict(tool_identity(), key='synthetic-new-tool-identity')
        with patch('ocr_workbench.table_tool.tool_identity', return_value=changed), patch.object(self.manager.cpu, 'call', wraps=call_real) as call:
            third = self.manager.process(page['id'], 'native'); self.manager.step()
            self.assertEqual(call.call_count, 1)
            self.assertEqual(self.result(third)['original']['document']['table_tool']['tool_key'], changed['key'])

    def test_broken_cache_recomputes_and_empty_detection_is_not_failure(self):
        doc, page = self.import_page()
        first = self.manager.process(page['id'], 'native'); self.manager.step()
        status = structure_view(self.store, self.result(first)['id'])['table_tool']
        self.assertEqual(status['state'], 'empty')
        self.assertNotIn('table_tool', [t['kind'] for t in document_review_queue(self.store, doc['id'])['tasks']])
        cache = list(self.store.root.rglob('table-cache/*.json'))
        self.assertEqual(len(cache), 1)
        cache[0].write_text('{bad', encoding='utf-8')
        with patch.object(self.manager.cpu, 'call', wraps=self.manager.cpu.call) as call:
            second = self.manager.process(page['id'], 'native', force=True); self.manager.step()
        self.assertEqual(call.call_count, 1)
        self.result(second)

    def test_resuming_regions_refreshes_auxiliary_snapshot_without_repeating_completed_ocr(self):
        doc, page = self.import_page('mixed.pdf')
        stage = self.manager.process(page['id'])
        with patch.object(self.manager.cpu, 'call', side_effect=TimeoutError('old tool failure')):
            self.manager.step()
        tasks = self.store.rows('SELECT * FROM tasks')
        self.assertTrue(tasks)
        while task := self.store.claim():
            complete_region_task(self.store, task, {'text': 'synthetic region text', 'engine': 'ppocr', 'tables': [], 'blocks': []})
        ids = [t['id'] for t in tasks]
        changed = dict(tool_identity(), key='synthetic-upgraded-tool')
        with patch('ocr_workbench.table_tool.tool_identity', return_value=changed):
            finalize_waiting_pages(self.manager)
            self.assertEqual(self.store.one('document_stages', stage)['status'], 'queued')
            self.manager.step()
            snapshot = json.loads(self.store.one('document_stages', stage)['output'])
            self.assertEqual(snapshot['table_tool']['tool_key'], changed['key'])
            self.assertIsNone(snapshot['table_structure_error'])
            self.assertTrue(all(self.store.one('tasks', key)['status'] == 'succeeded' for key in ids))
            finalize_waiting_pages(self.manager)
            self.assertEqual(self.result(stage)['original']['document']['table_tool']['tool_key'], changed['key'])
        self.assertEqual(len(self.store.rows('SELECT * FROM page_ocr_inputs')), len(ids))

    def test_retry_preserves_manual_edit_revision_history_selection_and_original(self):
        doc, page = self.import_page()
        stage = self.manager.process(page['id'], 'native')
        with patch.object(self.manager.cpu, 'call', side_effect=TimeoutError('initial tool failure')):
            self.manager.step()
        result = self.result(stage)
        edit = deepcopy(result['edited']); edit['text'] += '\nManual text 0007'
        result = self.store.save(result['id'], edit, result['revision'])
        before = self.store.result(result['id'])
        history = self.store.rows('SELECT * FROM edits')
        selected = self.store.rows('SELECT * FROM selections')
        with self.assertRaises(Conflict):
            enqueue(self.manager, result['id'], 0)
        retry = enqueue(self.manager, result['id'], result['revision'])
        self.assertEqual(structure_view(self.store, result['id'])['table_tool']['state'], 'queued')
        self.manager.step()
        self.assertEqual(self.store.one('document_stages', retry['stage_id'])['status'], 'succeeded')
        self.assertEqual(self.store.result(result['id']), before)
        self.assertEqual(self.store.rows('SELECT * FROM edits'), history)
        self.assertEqual(self.store.rows('SELECT * FROM selections'), selected)
        self.assertEqual(structure_view(self.store, result['id'])['table_tool']['state'], 'empty')
        self.assertNotIn('table_tool', [t['kind'] for t in document_review_queue(self.store, doc['id'])['tasks']])

    def test_retry_failure_and_cancellation_remain_actionable(self):
        _, page = self.import_page()
        stage = self.manager.process(page['id'], 'native'); self.manager.step()
        result = self.result(stage)
        job = enqueue(self.manager, result['id'], 0)
        with patch.object(self.manager.cpu, 'call', side_effect=ValueError('tool process failed')):
            self.manager.step()
        self.assertEqual(self.store.one('document_stages', job['stage_id'])['status'], 'failed')
        self.assertEqual(structure_view(self.store, result['id'])['table_tool']['state'], 'failed')
        job = enqueue(self.manager, result['id'], 0)
        with patch.object(self.manager.cpu, 'call', side_effect=DocumentCancelled()):
            self.manager.step()
        self.assertEqual(self.store.one('document_stages', job['stage_id'])['status'], 'interrupted')
        self.assertTrue(structure_view(self.store, result['id'])['table_tool']['retry_allowed'])


if __name__ == '__main__':
    unittest.main()
