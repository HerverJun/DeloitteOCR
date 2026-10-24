"""CPU-only native receipt fixtures, never an OCR/GPU/controller run."""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('screen_native_report', ROOT / 'scripts/screen_native_report.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
TOOLS = Path('F:/OCR/scripts/screen_benchmark')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def native_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class NativeFixture:
    """Small authoritative SQLite plus each real harness's captured schema."""

    def __init__(self, root, flavor='agent', statuses=('completed',), accepted=False):
        self.root, self.flavor = root, flavor
        self.run = root / 'actual-run'
        self.workspace = self.run / 'native/ocr'
        self.source = root / 'frozen-source'
        self.bundle = root / 'bundle'
        self.inputs = root / 'inputs.json'
        self.truth = root / 'truth.json'
        self.policy = {'strategy_version': 'fixture-v1', 'primary_engine': 'hunyuan',
                       'fallback_engines': ['hunyuan', 'glm']}
        self.actual_policy = {**self.policy, 'fallback_engines': ['glm']}
        (self.source / 'src/ocr_workbench').mkdir(parents=True)
        (self.source / 'src/ocr_workbench/service.py').write_text('# unit fixture only\n', encoding='utf-8')
        save(self.source / 'config/screen-policy.json', self.policy)
        save(self.bundle / 'config/engines.json', {'engine': 'fixture-not-executed'})
        self.tables = {name: [] for name in adapter.TABLES}
        self.tables['screen_batches'] = [{'id': 'batch1', 'project_id': 'project1',
                                          'policy': json.dumps(self.actual_policy)}]
        self.details = {}
        self.mapping, self.samples, self.truths = [], [], []
        for position, status in enumerate(statuses):
            ident, image_id, version_id = f'sample{position:03d}', f'image{position}', f'version{position}'
            original = root / 'sources' / (ident + '.png')
            original.parent.mkdir(parents=True, exist_ok=True)
            pixels = Image.new('RGB', (8, 6), (position * 20, 90, 210))
            info = PngImagePlugin.PngInfo()
            info.add_text('source', 'preserved input')
            pixels.save(original, compress_level=0, pnginfo=info)
            source_sha = adapter.digest(original)
            copied = self.run / 'inputs' / (ident + '.png')
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copied)
            table = {'rows': 1, 'columns': 1, 'cells': [{'row': 0, 'column': 0,
                     'text': '002', 'row_span': 1, 'column_span': 1}]}
            self.samples.append({'id': ident, 'image': str(original), 'image_sha256': source_sha,
                                 'width': 8, 'height': 6})
            self.truths.append({'id': ident, 'image_sha256': source_sha, 'tables': [table],
                'cohort': 'fixture', 'level': 'test', 'source_id': 'source' + str(position),
                'group_id': 'group' + str(position), 'context_targets': [{'text': '单位：元'}]})
            if flavor == 'agent':
                mapping = {'position': position, 'sample_id': ident, 'source_image_sha256': source_sha,
                           'normalized_input': str(copied), 'normalized_png_sha256': source_sha}
            else:
                mapping = {'position': position, 'sample_id': ident, 'source_image': str(original),
                           'image_sha256': source_sha, 'input_copy': str(copied)}
            self.mapping.append(mapping)
            if status == 'missing':
                continue
            if flavor == 'recovery':
                mapping.update(input_version_id=version_id, image_id=image_id)
            original_relative, version_relative = f'originals/{ident}.png', f'versions/{ident}.png'
            native_original, native_image = self.workspace / original_relative, self.workspace / version_relative
            native_original.parent.mkdir(parents=True, exist_ok=True)
            native_image.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, native_original)
            pixels.save(native_image, compress_level=9)
            version_sha = adapter.digest(native_image)
            self.tables['images'].append({'id': image_id, 'project_id': 'project1',
                'original_path': original_relative, 'sha256': source_sha, 'active_version': version_id})
            self.tables['versions'].append({'id': version_id, 'image_id': image_id,
                'path': version_relative, 'sha256': version_sha, 'width': 8, 'height': 6})
            review = 'accepted' if accepted else 'automatic' if status == 'completed' else 'pending'
            item = {'batch_id': 'batch1', 'input_version_id': version_id, 'image_id': image_id,
                'source_hash': version_sha, 'status': status, 'review_state': review,
                'result_id': None, 'result_revision': None, 'issues': '[]', 'candidates': '[]', 'error': None}
            self.tables['screen_items'].append(item)
            detail = {'sample_id': ident, 'source_image_sha256': source_sha, 'input_version_id': version_id,
                      'image_id': image_id, 'status': status, 'review_state': review,
                      'result_id': None, 'result_revision': None, 'result': None}
            self.details[ident] = detail
            if status not in {'completed', 'needs_review'}:
                continue
            source_task, final_task = f'ocr{position}', f'final{position}'
            source_result, final_result = f'ocrresult{position}', f'finalresult{position}'
            prep_id = f'crop{position}'
            metadata = {'path': str(native_image), 'kind': 'table_crop'}
            source_entry = {'result_id': source_result, 'task_id': source_task, 'revision': 0,
                           'engine': 'hunyuan', 'prepared_id': prep_id, 'prepared_metadata': metadata}
            model_table = deepcopy(table)
            model_table['cells'][0]['text'] = '001'
            raw = {'origin': 'screen_batch', 'engine': 'hunyuan', 'project_image_version': version_id,
                'text': '001', 'tables': [model_table], 'screen': {'batch_id': 'batch1',
                'input_sha256': version_sha, 'strategy_version': self.policy['strategy_version'],
                'sources': [source_entry], 'business_metadata': [{'text': '单位：元'}]}}
            candidate = {'id': f'candidate{position}', 'engine': 'hunyuan', 'raw': deepcopy(raw)}
            raw['screen']['selection'] = {'selected_id': candidate['id'],
                'automatic_delivery': status == 'completed' and not accepted,
                'review_required': status == 'needs_review', 'modifications': []}
            edited = {'text': '002' if accepted else '001', 'tables': deepcopy(raw['tables'])}
            if accepted:
                edited['tables'][0]['cells'][0]['text'] = '002'
            source_raw = {'project_image_version': version_id, 'screen_input': metadata,
                          'text': '001', 'tables': [model_table]}
            for task_id, result_id, kind in ((source_task, source_result, 'ocr'),
                                             (final_task, final_result, 'screen_final')):
                self.tables['tasks'].append({'id': task_id, 'kind': kind, 'status': 'succeeded',
                    'engine': 'hunyuan', 'image_id': image_id, 'input_version_id': version_id,
                    'result_id': result_id})
            self.tables['results'].extend([
                {'id': source_result, 'task_id': source_task, 'revision': 0,
                 'original': json.dumps(source_raw), 'edited': json.dumps({'text': '001', 'tables': [model_table]})},
                {'id': final_result, 'task_id': final_task, 'revision': int(accepted),
                 'original': json.dumps(raw), 'edited': json.dumps(edited)}])
            self.tables['screen_tasks'].append({'task_id': source_task, 'batch_id': 'batch1',
                'input_version_id': version_id, 'prepared_id': prep_id,
                'metadata': json.dumps(metadata), 'prepared_path': version_relative})
            self.tables['screen_final_tasks'].append({'task_id': final_task, 'batch_id': 'batch1',
                'input_version_id': version_id, 'content_hash': native_hash(raw)})
            item.update(result_id=final_result, result_revision=int(accepted), candidates=json.dumps([candidate]))
            detail.update(result_id=final_result, result_revision=int(accepted),
                result={'id': final_result, 'task_id': final_task, 'revision': int(accepted),
                        'original': raw, 'edited': edited})
        save(self.inputs, {'truth_available_to_inference': False, 'samples': self.samples})
        save(self.truth, {'samples': self.truths})
        files = adapter.source_inventory(self.source)
        self.prepared = {'format': 'screen-agent-actual-native-prepared-v1' if flavor == 'agent'
            else 'screen-native-eight-recovery-prepared-v1', 'inputs': str(self.inputs),
            'inputs_sha256': adapter.digest(self.inputs), 'native_source': str(self.source),
            'native_source_files': files, 'native_source_inventory_sha256': adapter.fingerprint(files, ascii=flavor == 'agent'),
            'bundle': str(self.bundle), 'bundle_engines_sha256': adapter.digest(self.bundle / 'config/engines.json'),
            'truth_read': False, 'frozen_results_loaded': False}
        self.summary = {'format': 'screen-agent-actual-native-result-v1' if flavor == 'agent'
            else 'screen-native-eight-recovery-result-v1', 'status': 'partial',
            'scope_inputs': len(statuses), 'native_source_inventory_sha256': self.prepared['native_source_inventory_sha256'],
            'truth_read': False, 'frozen_results_loaded': False, 'wall_seconds': 12.345,
            'wall_scope': 'actual-fixture-scope', 'delivery_completeness': 'partial'}
        if flavor == 'agent':
            self.prepared.update(input_count=len(statuses), screen_policy=self.policy,
                screen_policy_sha256=adapter.digest(self.source / 'config/screen-policy.json'))
            self.summary.update(real_queue_and_engine_adapter=True, shared_context_injected=False,
                inputs_sha256=self.prepared['inputs_sha256'], screen_policy_sha256=self.prepared['screen_policy_sha256'],
                native_batches=[{'batch_id': 'batch1', 'policy': self.actual_policy,
                                'resolved_policy_sha256': adapter.fingerprint(self.actual_policy)}])
        else:
            save(self.run / 'run-policy.json', self.policy)
            self.prepared.update(selected_ids=[sample['id'] for sample in self.samples], scope_inputs=len(statuses),
                run_policy=self.policy, run_policy_sha256=adapter.digest(self.run / 'run-policy.json'),
                source_default_policy_sha256=adapter.digest(self.source / 'config/screen-policy.json'),
                workspace=str(self.workspace))
            self.summary.update(prefilled_cache=False, batch_id='batch1', resolved_policy=self.actual_policy,
                                run_policy_sha256=self.prepared['run_policy_sha256'])
        save(self.run / 'prepared.json', self.prepared)
        if flavor == 'recovery':
            self.summary['prepared_sha256'] = adapter.digest(self.run / 'prepared.json')
        save(self.run / ('agent-native-summary.json' if flavor == 'agent' else 'native-recovery-summary.json'), self.summary)
        save(self.run / ('input-map.json' if flavor == 'agent' else 'native-input-map.json'), {'items': self.mapping})
        self.sync_database()

    def sync_database(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        dbpath = self.workspace / 'workbench.sqlite3'
        with closing(sqlite3.connect(dbpath)) as db, db:
            for name, rows in self.tables.items():
                db.execute('DROP TABLE IF EXISTS "' + name + '"')
                columns = list(rows[0]) if rows else ['fixture_empty']
                types = ['INTEGER' if rows and isinstance(rows[0][key], int) else 'TEXT' for key in columns]
                db.execute('CREATE TABLE "' + name + '" (' + ','.join('"' + key + '" ' + kind for key, kind in zip(columns, types)) + ')')
                if rows:
                    db.executemany('INSERT INTO "' + name + '" VALUES (' + ','.join('?' for _ in columns) + ')',
                                   [tuple(row.get(key) for key in columns) for row in rows])
        if self.flavor == 'agent':
            for name, rows in self.tables.items():
                save(self.run / 'native-receipts' / (name + '.json'), rows)
            for ident, detail in self.details.items():
                save(self.run / 'native-receipts' / (ident + '.json'), detail)
        else:
            save(self.run / 'after-restart-tables.json', self.tables)


class NativeReportTests(unittest.TestCase):
    def setUp(self):
        base = adapter.fpath(os.environ.get('SCREEN_NATIVE_TEST_TEMP', 'F:/OCR/output/ocr-screen-benchmark-20261007/temp'))
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='native-report-unit-', dir=base)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_both_actual_harness_shapes_accept_visible_reencoded_png(self):
        for flavor in ('agent', 'recovery'):
            with self.subTest(flavor=flavor):
                fixture = NativeFixture(self.root / flavor, flavor=flavor)
                index = adapter.load_native_index(fixture.run, fixture.inputs)
                row = index['rows'][0]
                self.assertNotEqual(row['input_sha256'], row['native_version_sha256'])
                self.assertEqual(index['scope_inputs'], 1)
                self.assertFalse(index['truth_opened'])
                self.assertEqual(index['gpu_calls'], 0)

    def test_selected_and_human_edited_view_are_scored_separately(self):
        fixture = NativeFixture(self.root, accepted=True)
        output = self.root / 'offline.json'
        read = adapter.read_json
        truth_reads = []
        def observed_read(path):
            if Path(path).resolve() == fixture.truth.resolve():
                frozen = read(output.with_suffix('.native-index.json'))
                self.assertFalse(frozen['truth_opened'])
                self.assertEqual(frozen['rows'][0]['selected_candidate']['tables'][0]['cells'][0]['text'], '001')
                truth_reads.append(True)
            return read(path)
        with patch.object(adapter, 'read_json', side_effect=observed_read):
            result = adapter.report(fixture.run, fixture.inputs, fixture.truth, TOOLS, output)
        self.assertEqual(truth_reads, [True])
        self.assertEqual(result['candidate_quality']['summary']['totals']['perfect_images'], 0)
        self.assertEqual(result['edited_export_view_quality']['summary']['totals']['perfect_images'], 1)
        self.assertEqual(result['candidate_quality']['summary']['totals']['context_retained'], 1)
        self.assertEqual(result['human_accepted_images'], 1)
        self.assertEqual(result['manual_edited_images'], 1)
        self.assertEqual(result['actual_native_wall']['wall_seconds'], 12.345)
        self.assertEqual(result['candidate_quality']['summary']['totals']['automatic_delivered_images'], 0)
        self.assertFalse((fixture.run / 'sample000/record.json').exists())
        with self.assertRaisesRegex(ValueError, 'immutable'):
            adapter.report(fixture.run, fixture.inputs, fixture.truth, TOOLS, output)

    def test_fail_cancel_missing_and_pending_stay_in_denominator(self):
        fixture = NativeFixture(self.root, statuses=('completed', 'failed', 'cancelled', 'missing', 'paused'))
        result = adapter.report(fixture.run, fixture.inputs, fixture.truth, TOOLS, self.root / 'all.json')
        totals = result['candidate_quality']['summary']['totals']
        self.assertEqual(totals['images'], 5)
        self.assertEqual(totals['failed_images'], 2)
        self.assertEqual(totals['missing_images'], 1)
        self.assertEqual(totals['pending_images'], 1)
        self.assertEqual(totals['critical_cells'], 5)
        self.assertEqual(result['native_counts']['cancelled'], 1)

    def test_frozen_input_and_native_bytes_or_active_identity_tamper_rejected(self):
        for changed in ('source', 'native', 'active'):
            with self.subTest(changed=changed):
                fixture = NativeFixture(self.root / changed)
                if changed == 'source':
                    Path(fixture.samples[0]['image']).write_bytes(b'changed')
                elif changed == 'native':
                    (fixture.workspace / fixture.tables['versions'][0]['path']).write_bytes(b'changed')
                else:
                    fixture.tables['images'][0]['active_version'] = 'different-version'
                    fixture.sync_database()
                with self.assertRaises(ValueError):
                    adapter.load_native_index(fixture.run, fixture.inputs)

    def test_source_policy_model_configuration_and_captured_revision_tamper_rejected(self):
        for changed in ('source', 'policy', 'model', 'revision'):
            with self.subTest(changed=changed):
                fixture = NativeFixture(self.root / changed)
                if changed == 'revision':
                    with closing(sqlite3.connect(fixture.workspace / 'workbench.sqlite3')) as db, db:
                        db.execute("UPDATE results SET revision=77 WHERE id='finalresult0'")
                else:
                    path = {'source': fixture.source / 'src/ocr_workbench/service.py',
                            'policy': fixture.source / 'config/screen-policy.json',
                            'model': fixture.bundle / 'config/engines.json'}[changed]
                    path.write_text('changed', encoding='utf-8')
                with self.assertRaises(ValueError):
                    adapter.load_native_index(fixture.run, fixture.inputs)

    def test_duplicate_native_input_mapping_and_wrong_source_crop_rejected(self):
        fixture = NativeFixture(self.root / 'duplicate', statuses=('completed', 'completed'))
        fixture.details['sample001']['input_version_id'] = 'version0'
        save(fixture.run / 'native-receipts/sample001.json', fixture.details['sample001'])
        with self.assertRaisesRegex(ValueError, 'share one native input'):
            adapter.load_native_index(fixture.run, fixture.inputs)
        fixture = NativeFixture(self.root / 'crop')
        fixture.tables['screen_tasks'][0]['prepared_path'] = 'versions/another.png'
        fixture.sync_database()
        with self.assertRaisesRegex(ValueError, 'crop path changed'):
            adapter.load_native_index(fixture.run, fixture.inputs)

    def test_bad_truth_leaves_immutable_actual_selection_without_formal_score(self):
        fixture = NativeFixture(self.root)
        fixture.truth.write_text('{invalid truth', encoding='utf-8')
        output = self.root / 'bad.json'
        with self.assertRaises(json.JSONDecodeError):
            adapter.report(fixture.run, fixture.inputs, fixture.truth, TOOLS, output)
        index = adapter.read_json(output.with_suffix('.native-index.json'))
        self.assertEqual(index['rows'][0]['selected_candidate']['tables'][0]['cells'][0]['text'], '001')
        self.assertFalse(output.exists())
        self.assertFalse(index['formal_run_created'])


if __name__ == '__main__':
    unittest.main()
