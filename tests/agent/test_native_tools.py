"""Actual managed native services, algorithms and typed bridges on isolated F data.

Controller is deterministic; this evidence never contributes to PA16 API scores.
"""
import asyncio
import csv
import io
import json
import os
from pathlib import Path
import time
import traceback
import unittest
import uuid
from contextlib import nullcontext
from dataclasses import replace
from unittest import mock

from fastapi.testclient import TestClient
from scripts.agent_native_preview import configure
from workbench_platform.app import app
from workbench_platform.agent.registry import ToolContext
from workbench_platform.agent.registry import invoke
from workbench_platform.agent.tools import data, ocr, classification, forensics, pdf

SOURCE = Path(__file__).resolve().parents[2]
# The development checkout is independent of the sealed native installations.
# Native frontends/runtimes must come from one verified release, not siblings
# that happen to exist next to this Git worktree.
NATIVE_RELEASE = Path('F:/OCR/release/Clear/versions/0.10.0')
PROGRAM = NATIVE_RELEASE / 'program'
RUN = Path('F:/OCR/output/platform-agent-dev-20261004')


class NativeToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = RUN / 'evidence/native' / ('run-' + uuid.uuid4().hex[:12])
        cls.previous = dict(os.environ)
        configure(NATIVE_RELEASE / 'platform', PROGRAM, cls.root)
        os.environ['WORKBENCH_AGENT_ENABLED'] = '1'
        cls.client = TestClient(app, base_url='http://127.0.0.1:8867')
        cls.client.__enter__()
        cls.services = app.state
        cls.services.asset_library[0].roots['fixtures'] = RUN / 'fixtures'
        cls.agent = app.state.agent
        (RUN / 'evidence/latest-native-run.json').write_text(json.dumps({'root': str(cls.root),
            'is_real_controller': False, 'uses_real_native_services': True}, indent=2), encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        os.environ.clear()
        os.environ.update(cls.previous)

    def setUp(self):
        self.workspace = 'w' + uuid.uuid4().hex[:20]
        self.services.workspace_store.create_workspace('Agent native engineering', workspace_id=self.workspace)
        self.session = self.agent.store.create_session(self.workspace, 'Native tests', [], 's' + uuid.uuid4().hex)
        self.run, _ = self.agent.store.start_run(self.session['id'], 'native integration', 'r' + uuid.uuid4().hex, 0)
        self.ctx = ToolContext(self.workspace, self.session['id'], self.run['id'], 1, 0, '', (), self.services, self.agent.store)
        self.counter = 0

    def asset(self, name):
        media = 'application/pdf' if name.endswith('.pdf') else 'image/jpeg' if name.endswith('.jpg') else 'image/png'
        return self.services.asset_library[0].register(self.workspace, 'fixtures', name, media_type=media, storage_mode='managed')

    def call(self, tool, arguments, timeout=180, call_id=None):
        self.counter += 1
        call = {'call_id': call_id or 'call_' + str(self.counter), 'tool': tool, 'arguments': arguments}
        self.agent.registry.validate_batch([call], self.agent.registry.tools)
        spec = self.agent.registry.tools[tool]
        failures = []
        http_failures = []
        from workbench_platform.jobs import native_classification
        original_connection = native_classification.http.client.HTTPConnection
        class DiagnosticConnection(original_connection):
            def request(self, method, url, *args, **kwargs):
                self.diagnostic_path = url
                return super().request(method, url, *args, **kwargs)
            def getresponse(self):
                try:
                    response = super().getresponse()
                except Exception as caught:
                    http_failures.append({'path': getattr(self, 'diagnostic_path', ''),
                                          'type': type(caught).__name__, 'message': str(caught)[:500]})
                    raise
                if response.status not in {200, 201, 202}:
                    http_failures.append({'path': getattr(self, 'diagnostic_path', ''),
                        'status': response.status, 'body': response.read(8192).decode('utf-8', errors='replace')})
                return response
        async def execute_once(*args):
            try:
                return await invoke(spec.execute, *args)
            except Exception as caught:
                failures.append({'type': type(caught).__name__, 'message': str(caught)[:500],
                                 'traceback': traceback.format_exc()})
                raise
        self.agent.registry.tools[tool] = replace(spec, execute=execute_once)
        try:
            diagnostic = (mock.patch.object(native_classification.http.client, 'HTTPConnection', DiagnosticConnection)
                          if tool == 'classification_infer' else nullcontext())
            with diagnostic:
                result = asyncio.run(self.agent.registry.dispatch(call, self.ctx))
        finally:
            self.agent.registry.tools[tool] = spec
            if failures:
                (self.root / (self.workspace + '-' + str(self.counter) + '-execute-failures.json')).write_text(
                    json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
            if http_failures:
                (self.root / (self.workspace + '-' + str(self.counter) + '-http-failures.json')).write_text(
                    json.dumps(http_failures, ensure_ascii=False, indent=2), encoding='utf-8')
        launch_failures = []
        if result['status'] == 'error':
            # Preserve the first startup failure. Re-executing a failed launch
            # under the same reservation only reports410 and hides its cause.
            supervisor = self.services.supervisor
            with supervisor._lock:
                entries = list(supervisor._entries.values())
            for entry in entries:
                if entry.future.done() and entry.future.exception() is not None:
                    failure = entry.future.exception()
                    launch_failures.append({'app_id': entry.identity[1],
                        'type': type(failure).__name__, 'message': str(failure)[:500]})
        if result['status'] == 'pending':
            from workbench_platform.application_services.job_lifecycle import adapter
            deadline = time.monotonic() + timeout
            # PDF page requests and image OCR both belong to OCR, but their
            # persisted receipts use different native job lifecycles.
            handler = adapter(result['job_refs'][0]).settle
            while time.monotonic() < deadline:
                value = handler(result['job_refs'][0], self.ctx)
                if value is not None:
                    result = {**value, 'job_refs': result['job_refs']}
                    operation = self.agent.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?', (self.run['id'], call['call_id']))[0]['id']
                    self.agent.store.finish_operation(operation, result)
                    break
                time.sleep(.2)
            else:
                self.fail('Native timeout: ' + tool)
        (self.root / (self.workspace + '-' + str(self.counter) + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if launch_failures:
            (self.root / (self.workspace + '-' + str(self.counter) + '-launch-failures.json')).write_text(
                json.dumps(launch_failures, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertEqual(result['status'], 'success', {'result': result, 'launch_failures': launch_failures})
        return result

    def fixed_set(self, names):
        assets = [self.asset(name) for name in names]
        value = self.call('assets_create_set', {'asset_ids': [asset.asset_id for asset in assets]})
        return value['data'], assets

    def test_real_failed_only_retry_with_damaged_native_copy_preserves_original(self):
        from contextlib import closing
        import hashlib
        import sqlite3
        from workbench_platform.application_services.native import scoped_set, attempt_set, read_job
        prepared = self.call('ocr_prepare', {})['data']
        fixed, assets = self.fixed_set(['invoice-1.png', 'note-1.png'])
        arguments = {'link_id': prepared['link_id'], 'asset_set_id': fixed['asset_set_id'],
                     'asset_set_revision': fixed['revision'], 'engines': ['ppocr']}
        original_call = {'call_id': 'induced_native_failure', 'tool': 'ocr_run', 'arguments': arguments}
        operation, _ = self.agent.store.begin_operation(self.run['id'], 1, original_call)
        context = replace(self.ctx, operation_id=operation['id'])
        attempt = attempt_set(context, scoped_set(context, fixed['asset_set_id'], fixed['revision']))
        handoff = self.services.ocr_handoff
        imported = handoff.begin(self.workspace, prepared['link_id'], attempt.asset_set_id, attempt.revision)
        row = handoff._row(self.workspace, imported['handoff_id'])
        adapter = handoff._adapter(handoff._context(self.workspace, prepared['link_id']))
        native = adapter.import_status(row['request_id'])
        damaged = native['items'][1]
        native_root = self.root / 'native/ocr'
        with closing(sqlite3.connect((native_root/'workbench.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            relative = db.execute('SELECT path FROM versions WHERE id=?', (damaged['target_version_id'],)).fetchone()[0]
        path = (native_root/relative).resolve()
        self.assertTrue(path.is_relative_to(native_root.resolve()))
        path.write_bytes(b'controlled engineering fault: invalid native image copy')
        # Damage only the isolated native working copy. Registered source
        # files remain valid and are read independently before/after retry.
        registry = self.services.asset_library[0]
        for asset in assets:
            with registry.stage_verified(self.workspace,asset.asset_id,revision=asset.revision,blob_hash=asset.blob_hash) as original:
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),asset.blob_hash)
        first = self.call('ocr_run', arguments, call_id='induced_native_failure')
        self.assertEqual(first['coverage']['succeeded'],1,first)
        self.assertEqual(first['coverage']['failed'],1,first)
        old_reference = first['job_refs'][0]
        old_job = read_job(self.ctx,old_reference['object_id'])
        old_batch = adapter.batch(old_job['native_job_ids'])
        success_ids = {task.native_task_id for task in old_batch.tasks if task.native_status=='succeeded'}
        retry = self.call('jobs_retry_failed', {'job_id':old_reference['object_id'], 'engines':['ppocr']})
        new_reference = retry['job_refs'][0]
        new_job = read_job(self.ctx,new_reference['object_id'])
        self.assertEqual(new_job['counts']['total'],1)
        self.assertEqual(new_job['counts']['succeeded'],1)
        self.assertTrue(success_ids.isdisjoint(new_job['native_job_ids']))
        self.assertNotEqual(old_reference['object_id'],new_reference['object_id'])
        retry_operation = self.agent.store.rows('SELECT * FROM operations WHERE run_id=? AND tool=?',(self.run['id'],'jobs_retry_failed'))[0]
        self.assertEqual(retry_operation['parent_id'],operation['id'])
        retained = adapter.batch(old_job['native_job_ids'])
        self.assertEqual((retained.succeeded,retained.failed),(1,1))
        (self.root/'failed-retry-evidence.json').write_text(json.dumps({'is_real_controller':False,
            'fault':'damaged isolated native copy; registered sources unchanged', 'original':old_job,
            'retry':new_job,'parent_operation_id':operation['id'],'success_tasks_not_repeated':True},ensure_ascii=False,indent=2),encoding='utf-8')

    def test_pdf_selected_pages_idempotency_export_and_original_table_bridge(self):
        prepared = self.call('ocr_prepare', {})['data']
        fixed, _ = self.fixed_set(['suppliers-five-pages.pdf'])
        inspected = self.call('ocr_inspect_document', {'link_id': prepared['link_id'],
            'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision']})['data']
        self.assertEqual(inspected['documents'][0]['page_count'], 5)
        document_id = inspected['documents'][0]['document_id']
        arguments = {'handoff_id': inspected['handoff_id'], 'document_id': document_id,
                     'page_numbers': [1, 3, 4, 5], 'mode': 'native'}
        first = self.call('ocr_process_pages', arguments, call_id='selected_pages')
        second = self.call('ocr_process_pages', arguments, call_id='selected_pages')
        self.assertEqual(first['job_refs'], second['job_refs'])
        self.assertEqual(first['coverage'], {'succeeded': 4, 'failed': 0, 'cancelled': 0, 'total': 4})
        _, _, adapter, _ = pdf.bound(self.ctx, inspected['handoff_id'], document_id)
        pages = adapter.document_pages(document_id)['pages']
        self.assertIsNone(next(page for page in pages if page['page_number'] == 2)['image_id'])
        read = self.call('ocr_read_page', {'handoff_id': inspected['handoff_id'], 'document_id': document_id, 'page_number': 1})
        self.assertGreater(read['data']['table_count'], 0)
        ref = read['result_refs'][0]
        described = self.call('results_describe', {'reference': ref, 'table_index': 0})
        self.assertEqual(described['data']['tables'][0]['columns'], 3)
        bridge = self.call('results_to_input', {'reference': ref, 'table_index': 0})
        registry = self.services.asset_library[0]
        asset = registry.get(self.workspace, bridge['data']['asset_id'])
        with registry.stage_verified(self.workspace, asset.asset_id, revision=asset.revision, blob_hash=asset.blob_hash) as path:
            raw = list(csv.reader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
        self.assertEqual(raw[1], ['000001', 'Alpha', '100.00'])
        session = self.call('data_create_session', {'name': 'OCR table bridge'})['data']['data_session_id']
        self.call('data_import', {'data_session_id': session, 'expected_version': 1, 'side': 'left',
            'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
        table = self.call('data_read_table', {'data_session_id': session, 'side': 'left'})
        self.assertEqual(table['data']['rows'][0]['cells'][0]['value'], '000001')
        right = self.asset('suppliers-right.csv')
        self.call('data_import', {'data_session_id': session,
            'expected_version': self.services.data_workbench.get(session)['version'], 'side': 'right',
            'asset_id': right.asset_id, 'asset_revision': right.revision, 'blob_hash': right.blob_hash})
        for side in ('left', 'right'):
            self.call('data_apply_step', {'data_session_id': session,
                'expected_version': self.services.data_workbench.get(session)['version'], 'side': side,
                'step': {'operator': 'cast', 'column': 'c3', 'target': 'decimal'}})
        compared = self.call('data_compare', {'data_session_id': session,
            'expected_version': self.services.data_workbench.get(session)['version'],
            'spec': {'key_pairs': [['c1', 'c1']], 'compare_pairs': [['c3', 'c3']],
                     'amount_pairs': [['c3', 'c3']], 'amount_tolerance': '1'}})
        result_id = compared['data']['result_id']
        differences = self.call('data_read_result', {'data_session_id': session, 'result_id': result_id, 'category': 'different'})
        self.assertEqual(sorted(row['L_c1_value'] for row in differences['data']['rows']), ['000001', '000003'])
        difference_export = self.call('data_export', {'data_session_id': session,
            'expected_version': self.services.data_workbench.get(session)['version'],
            'result_id': result_id, 'categories': ['different']})
        center = self.services.artifact_center
        file = center.file(self.workspace, difference_export['data']['bundle']['bundle_id'], 'results.csv')
        with center.store.stage_download(self.workspace, file.artifact_id, expected_revision=file.revision, expected_hash=file.blob_hash) as path:
            rows = list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
        self.assertEqual(sorted(row['L_c1_value'] for row in rows), ['000001', '000003'])
        self.assertEqual(sorted(row['L_c3_value'] for row in rows), ['100.00', '50.00'])
        exported = self.call('ocr_export', {'handoff_id': inspected['handoff_id']})
        self.assertEqual(sorted(item['page_number'] for item in exported['data']['bundle']['items']), [1, 3, 4, 5])
        delivery = self.call('delivery_publish', {'summary': '指定1、3–5页已处理并导出',
            'artifact_refs': exported['artifact_refs'], 'result_refs': [ref]})
        self.assertTrue(delivery['data']['verified'])

    def test_scanned_pdf_auto_only_selected_pages_real_ocr_and_no_invented_tables(self):
        import hashlib
        manifest = json.loads((RUN / 'fixtures/scan-manifest.json').read_text(encoding='utf-8'))
        fixture = RUN / 'fixtures' / manifest['file']
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), manifest['sha256'])
        prepared = self.call('ocr_prepare', {})['data']
        fixed, assets = self.fixed_set([manifest['file']])
        inspected = self.call('ocr_inspect_document', {'link_id': prepared['link_id'],
            'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision']})['data']
        self.assertEqual(inspected['documents'][0]['page_count'], 3)
        document_id = inspected['documents'][0]['document_id']
        arguments = {'handoff_id': inspected['handoff_id'], 'document_id': document_id,
                     'page_numbers': [1, 3], 'mode': 'auto', 'engine': 'ppocr'}
        first = self.call('ocr_process_pages', arguments, timeout=600)
        self.assertEqual(first['coverage'], {'succeeded': 2, 'failed': 0, 'cancelled': 0, 'total': 2})
        _, _, adapter, _ = pdf.bound(self.ctx, inspected['handoff_id'], document_id)
        before = adapter.page_request(document_id, first['job_refs'][0]['object_id'])
        second = self.call('ocr_process_pages', arguments)
        after = adapter.page_request(document_id, first['job_refs'][0]['object_id'])
        self.assertEqual(first['job_refs'], second['job_refs'])
        self.assertEqual(before['stages'], after['stages'])
        pages = adapter.document_pages(document_id)['pages']
        self.assertIsNone(next(page for page in pages if page['page_number'] == 2)['image_id'])
        readbacks = []
        for number, marker in zip([1, 3], manifest['expected_text_markers']):
            read = self.call('ocr_read_page', {'handoff_id': inspected['handoff_id'],
                'document_id': document_id, 'page_number': number})
            self.assertTrue(read['data']['has_result'])
            self.assertIn(marker, read['data']['text'])
            self.assertEqual(read['data']['table_count'], 0)
            ref = read['result_refs'][0]
            self.assertEqual(ref['coverage']['page_number'], number)
            described = self.call('results_describe', {'reference': ref})
            self.assertEqual(described['data']['tables'], [])
            self.assertEqual(described['data']['source_locator']['page_number'], number)
            invalid = {'call_id': 'no_scan_table_' + str(number), 'tool': 'results_to_input',
                       'arguments': {'reference': ref, 'table_index': 0}}
            rejected = asyncio.run(self.agent.registry.dispatch(invalid, self.ctx))
            self.assertEqual(rejected['status'], 'error')
            readbacks.append(read)
        self.assertEqual(self.agent.store.rows('SELECT * FROM derived_inputs WHERE workspace_id=?', (self.workspace,)), [])
        exported = self.call('ocr_export', {'handoff_id': inspected['handoff_id']})
        self.assertEqual(sorted(item['page_number'] for item in exported['data']['bundle']['items']), [1, 3])
        (self.root / 'scanned-pdf-evidence.json').write_text(json.dumps({
            'is_real_controller': False, 'uses_real_native_services': True,
            'source': {'asset_id': assets[0].asset_id, 'sha256': manifest['sha256']},
            'request': after, 'readbacks': readbacks, 'export': exported,
            'unselected_page_2_not_rendered': True, 'replay_created_no_new_stages': True,
            'missing_tables_rejected': True,
        }, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_real_image_ocr_and_six_forensic_algorithms(self):
        prepared = self.call('ocr_prepare', {})['data']
        fixed, assets = self.fixed_set(['invoice-1.jpg'])
        inferred = self.call('ocr_run', {'link_id': prepared['link_id'], 'asset_set_id': fixed['asset_set_id'],
            'asset_set_revision': fixed['revision'], 'engines': ['ppocr']}, timeout=600)
        self.assertGreater(len(inferred['data']['items'][0]['text']), 10)
        exported = self.call('ocr_export', {'handoff_id': inferred['data']['handoff_id']})
        self.assertTrue(exported['artifact_refs'])
        image = assets[0]
        inspected = self.call('forensics_inspect', {'asset_id': image.asset_id, 'asset_revision': image.revision,
            'blob_hash': image.blob_hash, 'tools': ['metadata', 'noise', 'frequency', 'jpeg', 'jpeg_grid', 'copy_move']})
        self.assertIsNone(inspected['data']['report']['verdict'])
        self.assertEqual(len(inspected['data']['report']['evidence']), 6)
        collected = self.call('forensics_collect', {'inspection_id': inspected['data']['inspection_id']})
        self.assertTrue(any(file['name'] == 'review.html' for file in collected['data']['bundle']['files']))
        delivery = self.call('delivery_publish', {'summary': '取证报告仅供人工复核', 'artifact_refs': collected['artifact_refs']})
        self.assertTrue(delivery['data']['verified'])

    def test_native_classification_prediction_subset_then_ocr_preserves_annotations(self):
        prepared = self.call('classification_prepare', {'categories': ['Invoice', 'Note']})['data']
        names = [f'{kind}-{number}.png' for kind in ('invoice', 'note') for number in (1, 2, 3)]
        fixed, assets = self.fixed_set(names)
        # Test setup trains a small genuine native model. This is not exposed as
        # a v1 Agent tool, and its accuracy is not a controller-quality score.
        handoff = self.services.classification_handoff
        labels = {item['item_id']: 'class_1' if name.startswith('invoice') else 'class_2'
                  for item, name in zip(fixed['items'], names)}
        imported = handoff.begin(self.workspace, prepared['link_id'], fixed['asset_set_id'], fixed['revision'], labels)
        adapter = handoff._adapter(handoff._context(self.workspace, prepared['link_id']))
        project_id = prepared['project_id']
        dataset = adapter._request('POST', f'/api/v1/projects/{project_id}/dataset-versions', {'name': 'Synthetic train validation', 'seed': 42})
        trained = adapter._request('POST', f'/api/v1/projects/{project_id}/experiments',
            {'dataset_id': dataset['id'], 'encoder': 'resnet18', 'head_method': 'logistic_cv', 'epochs': 1, 'device': 'cpu'}, key='test_train_' + self.workspace)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            job = adapter.job(trained['job_id'])
            if job.state in {'succeeded', 'failed', 'cancelled'}:
                break
            time.sleep(.5)
        self.assertEqual(job.state, 'succeeded', job)
        models = self.call('classification_prepare', {'link_id': prepared['link_id']})['data']['models']
        self.assertTrue(models)
        before = {asset['id']: asset.get('label_id') for asset in adapter.assets()}
        inferred = self.call('classification_infer', {'link_id': prepared['link_id'],
            'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision'], 'model_id': models[0]['id']})
        self.assertEqual(inferred['data']['total'], 6)
        result_items = inferred['data']['items']
        label = result_items[0]['prediction_label_id']
        subset = self.call('assets_select_from_results', {'handoff_id': inferred['data']['handoff_id'],
            'label_ids': [label], 'minimum_score': 0.0})
        selected = subset['data']['asset_set']
        expected = {item['asset_id'] for item in result_items if item['prediction_label_id'] == label}
        self.assertEqual({item['asset_id'] for item in selected['items']}, expected)
        after = {asset['id']: asset.get('label_id') for asset in adapter.assets()}
        self.assertTrue(all(after[key] == value for key, value in before.items()))
        self.assertTrue(all(value is None for key, value in after.items() if key not in before))
        ocr_project = self.call('ocr_prepare', {})['data']
        recognized = self.call('ocr_run', {'link_id': ocr_project['link_id'], 'asset_set_id': selected['asset_set_id'],
            'asset_set_revision': selected['revision'], 'engines': ['ppocr']}, timeout=240)
        self.assertEqual(recognized['coverage']['succeeded'], len(expected))
        self.assertEqual(recognized['coverage']['failed'], 0)


if __name__ == '__main__':
    unittest.main()
