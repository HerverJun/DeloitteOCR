"""Goal-sized entry points use real SQLite receipts and the existing Data worker."""
import asyncio
import copy
import csv
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_data_tools as fixtures
from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.application_services import composed, classification_action
from workbench_platform.application_services.composed_business import ClassificationPredict, CompareFiles, resolve_columns
from workbench_platform.application_services.operations import classification, data
from workbench_platform.agent.store import RunSuperseded, digest


class BusinessCompositeTests(fixtures.unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DataToolTests.setUp
    tearDown = fixtures.DataToolTests.tearDown
    asset = fixtures.DataToolTests.asset

    def configure(self):
        composed.register(self.registry)
        async def settle(state, call, pending, operation_id):
            child = replace(self.ctx, operation_id=operation_id)
            for _ in range(400):
                result = await asyncio.to_thread(data.settle, pending['job_refs'][0], child)
                if result is not None:
                    self.store.finish_operation(operation_id, result)
                    return result
                await asyncio.sleep(.025)
            self.fail('Native Data worker did not settle')
        self.services.agent = SimpleNamespace(registry=self.registry,
            graph=SimpleNamespace(load=lambda _: {'run_id': self.ctx.run_id, 'generation': self.ctx.generation}),
            harness_settle=settle)

    async def call(self, tool, arguments, call_id='composite'):
        return await self.registry.dispatch({'call_id': call_id, 'tool': tool, 'arguments': arguments}, self.ctx)

    def compare_args(self):
        left, right = self.asset('suppliers-left.csv'), self.asset('suppliers-right.csv')
        return {'left_asset_id': left.asset_id, 'right_asset_id': right.asset_id,
                'steps': [{'side': side, 'step': {'operator': 'trim', 'column': 'c2'}} for side in ('left', 'right')],
                'compare_spec': {'key_pairs': [['供应商编号', '供应商编号']],
                    'compare_pairs': [['供应商名称', '供应商名称'], ['金额', '金额']],
                    'amount_pairs': [['金额', '金额']], 'amount_tolerance': '1', 'trim_text': True}}

    async def test_two_files_compare_and_download_once_preserve_originals_and_decimal(self):
        self.configure()
        args = self.compare_args()
        hashes = [self.assets.get('wtest', args[side + '_asset_id']).blob_hash for side in ('left', 'right')]
        result = await self.call('data_compare_files', args)
        self.assertEqual(result['status'], 'success', result)
        self.assertTrue(result['data']['downloads'])
        bundle = result['data']['bundle']
        artifact = self.center.file('wtest', bundle['bundle_id'], 'results.csv')
        with self.center.store.stage_download('wtest', artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
        differences = sorted(row['L_c1_value'] for row in rows if row['category'] == 'different')
        self.assertEqual(differences, ['000001', '000003'])
        self.assertIn('123456789012345678901234', [row['L_c1_value'] for row in rows])
        jobs = [row['id'] for row in self.store.rows('SELECT id FROM operations')]
        self.assertEqual(result, await self.call('data_compare_files', args))
        self.assertEqual(jobs, [row['id'] for row in self.store.rows('SELECT id FROM operations')])
        self.assertEqual(hashes, [self.assets.get('wtest', args[side + '_asset_id']).blob_hash for side in ('left', 'right')])
        self.assertEqual(len(self.store.rows('SELECT id FROM runs')), 1)
        self.assertEqual(len(self.native.list('wtest')), 1)
        self.assertFalse(self.store.rows('SELECT id FROM model_requests'))

    async def test_compare_export_failure_reuses_import_calculation_and_fixed_result(self):
        self.configure()
        args = self.compare_args()
        with patch.object(self.center, 'publish_data_export', side_effect=OSError('interrupted download publication')):
            failed = await self.call('data_compare_files', args)
        self.assertEqual(failed['status'], 'error', failed)
        self.assertEqual(failed['error']['code'], 'export_incomplete', failed)
        before = self.store.rows("SELECT id,arguments FROM operations WHERE tool IN ('data_import','data_apply_step','data_compare') ORDER BY id")
        fixed = failed['result_refs']
        resumed = await self.call('data_compare_files', failed['data']['repair']['arguments'], call_id='repair')
        self.assertEqual(resumed['status'], 'success', resumed)
        self.assertEqual(resumed['result_refs'], fixed)
        self.assertEqual(before, self.store.rows("SELECT id,arguments FROM operations WHERE tool IN ('data_import','data_apply_step','data_compare') ORDER BY id"))
        self.assertEqual(len(self.native.list('wtest')), 1)

    async def test_ambiguous_columns_create_one_question_and_no_compare(self):
        self.configure()
        args = self.compare_args()
        args['compare_spec']['key_pairs'] = [['不存在', '供应商编号']]
        result = await self.call('data_compare_files', args)
        self.assertEqual(result['status'], 'needs_user', result)
        self.assertIn('c1=供应商编号', result['summary'])
        self.assertFalse(self.store.rows("SELECT id FROM operations WHERE tool='data_compare'"))
        self.assertEqual(len(self.store.rows('SELECT id FROM decisions')), 1)
        columns = [{'column_id': 'c1', 'header': '编号'}, {'column_id': 'c2', 'header': '编号'}]
        spec = CompareFiles.model_validate(self.compare_args()).compare_spec
        spec.key_pairs = [['编号', '编号']]
        with self.assertRaisesRegex(ValueError, '不能唯一确定'):
            resolve_columns(spec, {'left': {'columns': columns}, 'right': {'columns': columns}})

    def classification_fixture(self):
        # Inference is a controlled native receipt; asset sets and operation
        # recovery use real SQLite. This does not claim model accuracy.
        asset = self.assets.register('wtest', 'fixtures', 'suppliers-left.csv', media_type='image/png', storage_mode='managed')
        snapshot = {'handoff_id': 'handoff', 'items': [{'asset_id': asset.asset_id, 'prediction_label_id': 'invoice',
            'prediction_score': .8, 'annotation_label_id': None}], 'model_id': 'model'}
        ref = AgentRef(kind='result', workspace_id='wtest', application='classification', object_id='prediction',
            parent_id='handoff', sha256=digest(snapshot['items']), revision=digest(snapshot['items'])).model_dump()
        prediction = AgentToolResult(status='success', summary='预测完成', data={'handoff_id': 'handoff', 'items': snapshot['items']},
            result_refs=[ref], coverage={'total': 1, 'succeeded': 1}).model_dump()
        bundle = {'bundle_id': 'classification-bundle', 'files': [{'name': 'predictions.csv'}]}
        self.center.classification = SimpleNamespace(publish=Mock(return_value=bundle))
        return asset, snapshot, prediction

    async def test_classification_uses_active_model_and_export_resume_keeps_original_snapshot(self):
        self.configure()
        asset, snapshot, prediction = self.classification_fixture()
        args = {'asset_ids': [asset.asset_id]}
        catalog = AgentToolResult(status='success', summary='models', data={'link_id': 'link', 'requires_project_selection': False,
            'models': [{'id': 'other'}, {'id': 'model'}]}).model_dump()
        adapter = SimpleNamespace(project=lambda: {'active_model_id': 'model'})
        with patch.object(classification, 'prepare_tool', return_value=catalog) as prepare, \
             patch('workbench_platform.application_services.native_mapping.adapter_for', return_value=adapter), \
             patch.object(classification, 'infer', return_value=prediction) as infer, \
             patch.object(classification, 'snapshot', side_effect=lambda *_: copy.deepcopy(snapshot)):
            self.center.classification.publish.side_effect = OSError('export interrupted')
            failed = await self.call('classification_predict', args)
            self.assertEqual(failed['error']['code'], 'export_incomplete', failed)
            snapshot['items'][0]['annotation_label_id'] = 'human-edit-after-prediction'
            self.center.classification.publish.side_effect = None
            resumed = await self.call('classification_predict', failed['data']['repair']['arguments'], call_id='repair')
            self.assertEqual(resumed['status'], 'success', resumed)
            self.assertEqual(infer.call_count, 1)
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(infer.call_args.args[0].model_id, 'model')
            exported = self.center.classification.publish.call_args.kwargs['snapshot']
            self.assertIsNone(exported['items'][0]['annotation_label_id'])
            self.assertTrue(resumed['data']['downloads'][0]['download_url'].endswith('predictions.csv'))
            self.assertEqual(len(self.store.rows('SELECT id FROM runs')), 1)

    async def test_multiple_models_ask_once_before_import_or_inference(self):
        self.configure()
        asset, snapshot, prediction = self.classification_fixture()
        catalog = AgentToolResult(status='success', summary='models', data={'link_id': 'link', 'requires_project_selection': False,
            'models': [{'id': 'one', 'name': '票据'}, {'id': 'two', 'name': '合同'}]}).model_dump()
        with patch.object(classification, 'prepare_tool', return_value=catalog), \
             patch('workbench_platform.application_services.native_mapping.adapter_for', return_value=SimpleNamespace(project=lambda: {})), \
             patch.object(classification, 'infer') as infer:
            result = await self.call('classification_predict', {'asset_ids': [asset.asset_id]})
        self.assertEqual(result['status'], 'needs_user', result)
        self.assertIn('one（票据）', result['summary'])
        infer.assert_not_called()
        self.assertFalse(self.store.rows("SELECT id FROM operations WHERE tool='classification_inputs'"))

    async def test_cancel_before_export_never_restarts_or_publishes(self):
        self.configure()
        asset, snapshot, prediction = self.classification_fixture()
        catalog = AgentToolResult(status='success', summary='models', data={'link_id': 'link', 'requires_project_selection': False,
            'models': [{'id': 'model'}]}).model_dump()
        def stop(*_):
            self.store.status(self.ctx.run_id, self.ctx.generation, 'cancelled')
            return snapshot
        with patch.object(classification, 'prepare_tool', return_value=catalog), \
             patch('workbench_platform.application_services.native_mapping.adapter_for', return_value=SimpleNamespace(project=lambda: {})), \
             patch.object(classification, 'infer', return_value=prediction), \
             patch.object(classification, 'snapshot', side_effect=stop):
            with self.assertRaises(RunSuperseded):
                await self.call('classification_predict', {'asset_ids': [asset.asset_id]})
        self.center.classification.publish.assert_not_called()
