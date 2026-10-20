"""Agent adapter and authorization tests; never count mocks as real inference."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from packages.contracts.agent import AgentRef
from workbench_platform.agent.registry import ToolContext, ToolRegistry
from workbench_platform.agent.store import AgentStore, digest
from workbench_platform.agent.tasks import AuditTasks
from workbench_platform.agent.tools import multimodal, context
from workbench_platform.application_services.references import verify, reference_location
from workbench_platform.application_services.job_lifecycle import adapter

ROOT = Path('F:/OCR/output/clear-platform-tools-20261016/temp')


class BindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.store = AgentStore(Path(self.temp.name) / 'agent')
        runtime = SimpleNamespace(store=self.store)
        runtime.tasks = AuditTasks(runtime)
        self.session = self.store.create_session('wtest', 'test', ['asset1'], 'sreq')
        self.run, _ = self.store.start_run(self.session['id'], 'test', 'rreq', 1)
        self.registry = ToolRegistry()
        context.register(self.registry)
        multimodal.register(self.registry)
        self.asset = SimpleNamespace(asset_id='asset1', revision=1, blob_hash='a' * 64)
        self.native = Mock()
        self.native.lookup_operation.return_value = {'found': False}
        self.native.references.return_value = {'items': [{'reference_id': 'face1'}, {'reference_id': 'face2'}]}
        self.item = {'job_id': 'j1', 'workspace_id': 'wtest', 'kind': 'face', 'generation': 1, 'revision': 1,
            'inputs': [{'asset_id': 'asset1', 'asset_revision': 1, 'blob_hash': 'a' * 64}],
            'status': 'succeeded', 'options': {'device': 'cpu'}, 'result': {'images': []}, 'review': None}
        self.snapshot = {'snapshot_id': 'ms1', 'workspace_id': 'wtest', 'job_id': 'j1', 'kind': 'face',
            'generation': 1, 'revision': 1, 'review_revision': 0, 'result_hash': 'b' * 64,
            'model_identity': {'model': 'c' * 64}, 'sources': deepcopy(self.item['inputs']),
            'reference_scope': [], 'review_state': 'unreviewed', 'analysis': deepcopy(self.item), 'snapshot_hash': 'd' * 64}
        self.native.get.return_value = self.item
        self.native.snapshot.return_value = self.snapshot
        self.native.create.return_value = self.item
        runtime.registry = self.registry
        assets = SimpleNamespace(get=lambda workspace, identifier: self.asset if identifier in {'asset1', 'asset2'} else None)
        self.services = SimpleNamespace(agent=runtime, multimodal_service=self.native, asset_library=(assets, None, None),
            workspace_store=SimpleNamespace(get_workspace=lambda _: True))
        self.ctx = ToolContext('wtest', self.session['id'], self.run['id'], 1, 0, 'op1', ('asset1',), self.services, self.store)

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self, **options):
        return multimodal.Analyze(inputs=self.item['inputs'], options=options)

    def test_all_new_contracts_strict_and_chinese_discovery(self):
        self.assertEqual(len(self.registry.tools), 25)
        self.assertTrue(all(spec.input_schema.model_config['extra'] == 'forbid' for spec in self.registry.tools.values()))
        page = self.registry.catalog(keyword='录音', services=None)
        self.assertIn('audio_transcribe', [item['name'] for item in page['tools']])
        self.assertEqual([item['name'] for item in self.registry.catalog(capability='seal')['tools']], ['seal_extract'])
        self.native.create.assert_not_called()

    def test_pending_capability_is_explicit(self):
        registry = ToolRegistry()
        values = registry.capability_catalog()
        self.assertEqual(next(item for item in values if item['id'] == 'face')['binding'], 'pending')
        self.assertTrue(next(item for item in values if item['id'] == 'face')['blockers'])

    def test_face_search_needs_explicit_authorization(self):
        with self.assertRaises(ValueError):
            multimodal.analyze('face', self.analyze(action='search', reference_ids=['face2']), self.ctx)
        self.native.create.assert_not_called()

    def test_authorized_search_freezes_only_selected_ids(self):
        task = self.services.agent.tasks.get(self.session['id'])
        self.services.agent.tasks.update(self.session['id'], {'request_id': 'auth', 'expected_revision': task['revision'],
            'authorized_reference_ids': ['face1']})
        self.services.agent.tasks.apply_scope(self.session['id'])
        value = multimodal.analyze('face', self.analyze(action='search', reference_ids=['face1']), self.ctx)
        self.assertEqual(value['status'], 'pending')
        self.assertEqual(self.native.create.call_args.args[1]['options']['reference_ids'], ['face1'])

    def test_missing_search_scope_creates_real_user_decision(self):
        value = multimodal.analyze('face', self.analyze(action='search'), self.ctx)
        self.assertEqual(value['status'], 'needs_user')
        self.assertEqual(len(self.store.rows('SELECT id FROM decisions')), 1)
        self.native.create.assert_not_called()

    def test_input_scope_checked_before_submission(self):
        args = self.analyze()
        args.inputs[0].asset_id = 'asset2'
        with self.assertRaises(ValueError):
            multimodal.analyze('face', args, self.ctx)
        self.native.create.assert_not_called()

    def test_unknown_receipt_queries_original_identity_without_submission(self):
        self.native.lookup_operation.return_value = {'found': True, 'resource_id': 'j1'}
        value = multimodal.lookup_analysis('face', self.analyze(), self.ctx)
        self.assertEqual(value['job_refs'][0]['object_id'], 'j1')
        self.native.lookup_operation.assert_called_once_with('wtest', 'create', 'op1')
        self.native.create.assert_not_called()

    def test_snapshot_reference_is_verified_and_routed(self):
        ref = multimodal.result_ref(self.ctx, self.snapshot)
        self.assertEqual(verify(ref.model_dump(), self.ctx)['content_sha256'], 'd' * 64)
        self.assertIn('snapshot=ms1', reference_location(ref.model_dump()))
        ref.sha256 = 'e' * 64
        with self.assertRaises(ValueError):
            verify(ref.model_dump(), self.ctx)

    def test_wait_settlement_does_not_poll_model_or_resubmit(self):
        self.item['status'] = 'running'
        ref = multimodal.job_ref(self.ctx, self.item).model_dump()
        self.assertIsNone(adapter(ref).settle(ref, self.ctx))
        self.native.create.assert_not_called()

    def test_user_action_cannot_be_forged_by_model_arguments(self):
        with self.assertRaises(ValueError):
            multimodal.ApplyAction(decision_id='op1', actor='human', confirmation='yes')
        with self.assertRaises(ValueError):
            multimodal.apply_action(multimodal.ApplyAction(decision_id='op1'), self.ctx)
        self.native.review.assert_not_called()

    def test_review_bound_to_snapshot_and_real_user_reply(self):
        value = multimodal.request_action(multimodal.UserAction(action='review', job_id='j1'), self.ctx)
        self.assertEqual(value['status'], 'needs_user')
        with self.assertRaises(ValueError):
            multimodal.apply_action(multimodal.ApplyAction(decision_id='op1'), self.ctx)
        with self.store.transaction() as db:
            db.execute('UPDATE decisions SET reply=?,request_id=? WHERE id=?', ('确认此版本', 'userreply', 'op1'))
        self.item['revision'] = 2
        with self.assertRaises(ValueError):
            multimodal.apply_action(multimodal.ApplyAction(decision_id='op1'), replace(self.ctx, operation_id='apply'))
        self.native.review.assert_not_called()

    def test_unreviewed_draft_allowed_but_formal_delivery_fails(self):
        self.assertFalse(any(item['status'] == 'failed' for item in multimodal.delivery_checks(self.snapshot)))
        self.assertTrue(any(item['status'] == 'failed' for item in multimodal.delivery_checks(self.snapshot, require_review=True)))

    def test_audio_delivery_rejects_out_of_range_or_missing_locator(self):
        value = deepcopy(self.snapshot)
        value['kind'] = 'audio'
        value['analysis']['result'] = {'duration_ms': 1000, 'segments': [{'start_ms': 0, 'end_ms': 2000}]}
        self.assertEqual(multimodal.delivery_checks(value)[-1]['status'], 'failed')

    def test_reference_list_does_not_expose_other_project_references(self):
        self.assertEqual(multimodal.list_references(multimodal.Empty(), self.ctx)['data']['items'], [])

    def test_reading_another_job_cannot_grant_control_ownership(self):
        from workbench_platform.agent.tools.jobs import owned
        call = {'call_id': 'readforeign', 'tool': 'multimodal_read_result', 'arguments': {'job_id': 'j1'}}
        operation, _ = self.store.begin_operation(self.ctx.run_id, 1, call)
        self.store.finish_operation(operation['id'], multimodal.read(multimodal.Result(job_id='j1'), self.ctx))
        with self.assertRaises(ValueError):
            owned(self.ctx, 'j1')

    def test_snapshot_cannot_leak_unauthorized_reference_scores(self):
        self.snapshot['reference_scope'] = [{'reference_id': 'face2'}]
        with self.assertRaises(ValueError):
            multimodal.checked_snapshot(self.ctx, multimodal.Result(job_id='j1'))

    def test_scope_changes_apply_at_safe_boundary(self):
        task = self.services.agent.tasks.get(self.session['id'])
        self.services.agent.tasks.update(self.session['id'], {'request_id': 'scope', 'expected_revision': task['revision'],
            'authorized_reference_ids': ['face1']})
        self.assertEqual(multimodal.authorized(self.ctx), set())
        self.services.agent.tasks.apply_scope(self.session['id'])
        self.assertEqual(multimodal.authorized(self.ctx), {'face1'})
