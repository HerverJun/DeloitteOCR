"""Fixed native-binding provenance using real platform stores and native read stand-ins."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pytest
import test_harness_delivery_checks as fixtures

from packages.contracts.agent import AgentRef
from packages.contracts.models import AssetSetItem, SourceLocator
from workbench_platform.application_services.identity import canonical
from workbench_platform.application_services import native_mapping
from workbench_platform.application_services.references import verify
from workbench_platform.application_services.requirement_coverage import project_requirements
from workbench_platform.artifacts.text_outputs import verify_text_artifact
from workbench_platform.jobs.native_ocr import ResultArtifact


ROOT = Path('F:/OCR/output/user-flow-fixes-20261018/evidence/native-binding-tests018')


class NativeBindingDeliveryTests(unittest.IsolatedAsyncioTestCase):
    tearDown = fixtures.DeliveryChecksTests.tearDown
    deliver = fixtures.DeliveryChecksTests.deliver

    def setUp(self):
        root = patch.object(fixtures, 'ROOT', ROOT)
        root.start()
        self.addCleanup(root.stop)
        fixtures.DeliveryChecksTests.setUp(self)
        self.services.agent = SimpleNamespace(registry=self.registry)
        native_mapping.ensure_table(self.ctx)
        self.refs, self.bindings, self.native_results = [], {}, []
        self.state = {'documents': [{'id': 'document', 'sha256': self.source.blob_hash}], 'images': [], 'tasks': []}
        for page in (2, 5):
            identifier = str(page)
            ref = AgentRef(kind='result', workspace_id='wtest', application='ocr', object_id='result' + identifier,
                native_id='result' + identifier, parent_id='binding' + identifier, revision=0, sha256=identifier * 64)
            self.refs.append(ref)
            source = {'asset_id': self.source.asset_id, 'asset_revision': self.source.revision,
                'blob_hash': self.source.blob_hash, 'native_kind': 'document', 'native_id': 'document'}
            value = {'id': ref.parent_id, 'workspace_id': 'wtest', 'application': 'ocr', 'link_id': 'original-link',
                'object_id': ref.object_id, 'sources': {'document': source}, 'asset_set_id': self.asset_set.asset_set_id,
                'asset_set_revision': self.asset_set.revision, 'native_items': ['task' + identifier]}
            self.bindings[ref.parent_id] = value
            with self.store.transaction() as db:
                db.execute('INSERT INTO native_bindings VALUES(?,?,?,?,?,?)', (ref.parent_id, 'wtest', 'ocr',
                    'original-link', ref.object_id, canonical({key: value[key] for key in
                        ('sources', 'asset_set_id', 'asset_set_revision', 'native_items')})))
            self.state['images'].append({'id': 'image' + identifier, 'sha256': 'c' * 64,
                'document_id': 'document', 'page_number': page})
            self.state['tasks'].append({'id': 'task' + identifier, 'image_id': 'image' + identifier,
                'version_id': 'version' + identifier, 'status': 'succeeded', 'result_id': ref.object_id, 'engine': 'ppocr'})
            self.native_results.append(ResultArtifact(ref.object_id, 'task' + identifier, 'image' + identifier,
                'version' + identifier, True, {'kind': 'image', 'image_id': 'image' + identifier,
                    'version_id': 'version' + identifier, 'document_id': 'document', 'page_number': page}))
        self.adapter = Mock()
        self.adapter.snapshot.side_effect = lambda: deepcopy(self.state)
        self.adapter.results.side_effect = lambda tasks: [item for item in self.native_results if item.native_task_id in tasks
            and next(task for task in self.state['tasks'] if task['id'] == item.native_task_id)['status'] == 'succeeded']
        self.adapter.result_snapshot.side_effect = lambda result, revision: {'available': revision == 0, 'original_sha256': 'c' * 64,
            'edited': {'text': 'Complete page ' + str(result.locator['page_number']) + ' 000731 100.20\n'}}
        self.adapter.result_tables.side_effect = lambda result, revision: {'content_sha256': next(ref.sha256 for ref in self.refs
            if ref.object_id == result.native_result_id), 'tables': [], 'source': 'none', 'warnings': []}
        self.adapter.result_revision.return_value = 0
        adapter = patch('workbench_platform.application_services.native_mapping.adapter_for', return_value=self.adapter)
        adapter.start()
        self.addCleanup(adapter.stop)

    async def test_binding_describe_text_sidecar_and_final_delivery_preserve_original_metadata(self):
        from workbench_platform.agent.tools import results
        results.register(self.registry)
        before = [dict(row) for row in self.store.rows('SELECT * FROM native_bindings ORDER BY id')]
        for ref in self.refs:
            value = verify(ref.model_dump(), self.ctx)
            self.assertEqual(value['source_asset']['source_locator'], self.locator.model_dump(mode='json'))
            self.assertEqual(value['locator']['page_number'], int(ref.object_id[-1]))
            described = await self.registry.dispatch({'call_id': 'describe-' + ref.object_id, 'tool': 'results_describe',
                'arguments': {'reference': ref.model_dump()}}, self.ctx)
            self.assertEqual(described['status'], 'success', described)
        delivered = await self.deliver([], result_refs=[ref.model_dump() for ref in self.refs])
        self.assertEqual(delivered['status'], 'success', delivered)
        self.assertEqual(delivered['data']['completeness'], 'complete', delivered)
        coverage = delivered['data']['requirement_coverage']
        self.assertEqual((coverage['total'], coverage['satisfied'], coverage['missing'], coverage['unverified']), (2, 2, 0, []))
        self.assertEqual(len(delivered['data']['readable_text']), 2)
        self.assertEqual([dict(row) for row in self.store.rows('SELECT * FROM native_bindings ORDER BY id')], before)
        for output in delivered['data']['delivery']:
            ref = AgentRef(kind='artifact', workspace_id='wtest', object_id=output['bundle_id'], revision=1)
            proof = verify_text_artifact(ref, self.ctx, bundle=verify(ref.model_dump(), self.ctx))
            self.assertIn(proof['proof']['scope']['page_number'], (2, 5))


@pytest.mark.parametrize('case', ['wrong_page', 'wrong_result_revision', 'wrong_result_hash', 'wrong_input_revision',
    'wrong_input_hash', 'wrong_set', 'other_workspace_set', 'duplicate_member', 'wrong_member_page',
    'wrong_original_hash', 'outside_scope', 'failed_task', 'unknown_task'])
def test_fixed_binding_never_supplies_text_or_coverage_for_unverified_sources(case):
    fixture = NativeBindingDeliveryTests()
    fixture.setUp()
    try:
        ref, binding = fixture.refs[0], deepcopy(fixture.bindings[fixture.refs[0].parent_id])
        source = binding['sources']['document']
        if case == 'wrong_page':
            ref = ref.model_copy(update={'coverage': {'page_number': 5}})
        elif case == 'wrong_result_revision':
            ref = ref.model_copy(update={'revision': 7})
        elif case == 'wrong_result_hash':
            ref = ref.model_copy(update={'sha256': 'f' * 64})
        elif case == 'wrong_input_revision':
            source['asset_revision'] = 2
        elif case == 'wrong_input_hash':
            source['blob_hash'] = 'f' * 64
        elif case == 'wrong_set':
            binding['asset_set_id'] = 'missing-fixed-set'
        elif case == 'other_workspace_set':
            other = fixture.assets.register('wother', 'fixtures', 'source.csv', media_type='text/csv', storage_mode='managed')
            locator = SourceLocator(schema_version='1.0', kind='file', asset_id=other.asset_id,
                asset_revision=other.revision, blob_hash=other.blob_hash, precision='exact')
            fixed = fixture.sets.create('wother', [AssetSetItem(schema_version='1.0', item_id='other', asset_id=other.asset_id,
                asset_revision=other.revision, blob_hash=other.blob_hash, source_locator=locator)])
            binding['asset_set_id'], binding['asset_set_revision'] = fixed.asset_set_id, fixed.revision
        elif case == 'duplicate_member':
            fixed = fixture.sets.create('wtest', [fixture.asset_set.items[0],
                fixture.asset_set.items[0].model_copy(update={'item_id': 'second'})])
            binding['asset_set_id'], binding['asset_set_revision'] = fixed.asset_set_id, fixed.revision
        elif case == 'wrong_member_page':
            member = fixture.asset_set.items[0]
            locator = member.source_locator.model_copy(update={'kind': 'page', 'page_number': 5, 'precision': 'page_only'})
            fixed = fixture.sets.create('wtest', [member.model_copy(update={'source_locator': locator})])
            binding['asset_set_id'], binding['asset_set_revision'] = fixed.asset_set_id, fixed.revision
        elif case == 'wrong_original_hash':
            fixture.state['documents'][0]['sha256'] = 'f' * 64
        elif case == 'outside_scope':
            fixture.ctx = fixture.ctx.__class__(**{**fixture.ctx.__dict__, 'selected_assets': ('different-asset',)})
        elif case in {'failed_task', 'unknown_task'}:
            fixture.state['tasks'][0]['status'] = 'failed' if case == 'failed_task' else 'unknown'
        with fixture.store.transaction() as db:
            db.execute('UPDATE native_bindings SET payload=? WHERE id=?', (canonical({key: binding[key] for key in
                ('sources', 'asset_set_id', 'asset_set_revision', 'native_items')}), binding['id']))
        with pytest.raises((ValueError, KeyError, StopIteration)):
            native_mapping.verify_reference(fixture.ctx, ref, binding)
        if case in {'wrong_set', 'other_workspace_set', 'outside_scope', 'wrong_input_revision', 'wrong_input_hash', 'duplicate_member'}:
            fixture.adapter.results.assert_not_called()
        coverage = project_requirements([], [ref.model_dump()], fixture.ctx)
        assert coverage['satisfied'] == 0 and coverage['unverified']
        with closing(fixture.center._connect()) as db:
            assert db.execute('SELECT COUNT(*) FROM native_export_bundles').fetchone()[0] == 0
    finally:
        fixture.tearDown()
        fixture.doCleanups()


@pytest.mark.parametrize('case', ['different_task_version', 'different_actual_page', 'ambiguous_tasks'])
def test_binding_coverage_requires_the_actual_unique_task_input_and_page(case):
    fixture = NativeBindingDeliveryTests()
    fixture.setUp()
    try:
        ref = fixture.refs[0]
        if case == 'different_task_version':
            fixture.state['tasks'][0]['version_id'] = 'different-version'
        elif case == 'different_actual_page':
            fixture.state['images'][0]['page_number'] = 4
        else:
            binding = fixture.bindings[ref.parent_id]
            binding['native_items'].append('task5')
            with fixture.store.transaction() as db:
                db.execute('UPDATE native_bindings SET payload=? WHERE id=?', (canonical({key: binding[key] for key in
                    ('sources', 'asset_set_id', 'asset_set_revision', 'native_items')}), ref.parent_id))
        coverage = project_requirements([], [ref.model_dump()], fixture.ctx)
        assert coverage['satisfied'] == 0 and coverage['unverified']
    finally:
        fixture.tearDown()
        fixture.doCleanups()
