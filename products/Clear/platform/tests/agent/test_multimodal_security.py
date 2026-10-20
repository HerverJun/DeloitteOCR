"""Lightweight scope/review regression tests, never real algorithm evidence."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_multimodal_bindings as fixtures
from packages.contracts.agent import AgentRef
from packages.contracts.audit import DirectEvidenceRequest
from workbench_platform.agent.store import canonical, digest
from workbench_platform.agent.tools import multimodal, context
from workbench_platform.application_services.job_lifecycle import adapter
from workbench_platform.application_services.references import verify
from workbench_platform.application_services.review_provenance import inspect_review_provenance
from workbench_platform.audit.direct import snapshot as bind_snapshot
from workbench_platform.audit.service import AuditService
from workbench_platform.data_api.service import DataError


class MultimodalSecurityTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.BindingTests.setUp
    tearDown = fixtures.BindingTests.tearDown

    def prepare(self):
        self.assets = {}
        def asset(identifier, checksum):
            value = {'asset_id': identifier, 'revision': 1, 'blob_hash': checksum, 'workspace_id': 'wtest'}
            return SimpleNamespace(**value, model_dump=lambda **_: dict(value))
        self.assets['asset1'] = asset('asset1', 'a' * 64)
        self.assets['derived1'] = asset('derived1', 'f' * 64)
        self.services.asset_library[0].get = lambda _workspace, identifier: self.assets[identifier]
        self.services.asset_library[0].stage_verified = lambda *_, **__: nullcontext()
        self.ref = multimodal.result_ref(self.ctx, self.snapshot).model_dump()

    def evidence(self, reference=None):
        self.prepare()
        value = bind_snapshot(DirectEvidenceRequest(reference=reference or self.ref), self.ctx)
        value.update(stale=False, outdated_reasons=[])
        self.services.audit_service = SimpleNamespace(drafts=SimpleNamespace(evidence=lambda *_: deepcopy(value)))
        return value, AgentRef(kind='evidence', workspace_id='wtest', object_id=value['evidence_id'],
            parent_id=self.ctx.session_id, revision=1, sha256=value['snapshot_hash']).model_dump()

    def derived(self, *, provenance=None, cycle=False):
        self.prepare()
        sources = [AgentRef(kind='asset', workspace_id='wtest', object_id='derived1', revision=1,
            sha256='f' * 64).model_dump()] if cycle else [self.ref]
        body = {'asset_id': 'derived1', 'input_sha256': 'f' * 64, 'sources': sources,
            'provenance': provenance or {'kind': 'manual_seal_draft', 'analysis_id': 'j1',
                'snapshot_id': 'ms1', 'snapshot_hash': 'd' * 64, 'draft': True, 'review_state': 'unreviewed'}}
        folder = self.store.root / 'bridges'; folder.mkdir(exist_ok=True)
        path = folder / 'source.json'; payload = canonical(body).encode('utf-8'); path.write_bytes(payload)
        with self.store.transaction() as db:
            db.execute('INSERT INTO derived_inputs VALUES(?,?,?,?,?,?,?)', ('wtest', 'derived1', canonical(sources),
                canonical(['asset1']), 'bridges/source.json', hashlib.sha256(payload).hexdigest(), 'bridge'))
        return AgentRef(kind='asset', workspace_id='wtest', object_id='derived1', revision=1, sha256='f' * 64).model_dump(), path

    def confirm(self):
        self.snapshot['review_revision'] = 2
        self.snapshot['analysis']['review'] = {'assessment': 'confirmed', 'actor': 'Actual user', 'request_id': 'user-review'}

    def assertReviewBlocked(self, reference, **kwargs):
        result = inspect_review_provenance(reference, self.ctx, require_review=True, **kwargs)
        self.assertTrue(result['draft'], result)
        self.assertTrue(any(check['name'] == 'human_review' and check['status'] == 'failed' for check in result['checks']), result)

    def test_unreviewed_result_is_allowed_only_as_draft(self):
        self.prepare()
        result = inspect_review_provenance(self.ref, self.ctx)
        self.assertTrue(result['draft'])
        self.assertFalse(any(check['status'] == 'failed' for check in result['checks']))
        self.assertReviewBlocked(self.ref)

    def test_model_cannot_override_native_review_using_reference_coverage(self):
        self.prepare()
        self.ref['coverage'].update(reviewed=True, draft=False)
        self.assertReviewBlocked(self.ref)

    def test_confirmed_native_snapshot_is_accepted(self):
        self.prepare(); self.confirm()
        result = inspect_review_provenance(self.ref, self.ctx, require_review=True)
        self.assertFalse(result['draft'])
        self.assertFalse(any(check['status'] == 'failed' for check in result['checks']))

    def test_fixed_evidence_wrapper_does_not_remove_review_obligation(self):
        value, ref = self.evidence()
        self.assertTrue(value['observation']['draft'])
        self.assertReviewBlocked(ref)

    def test_finding_wrapper_does_not_remove_review_obligation(self):
        captured, _ = self.evidence()
        body = {'draft_id': 'finding1', 'workspace_id': 'wtest', 'task_id': self.ctx.session_id,
            'revision': 1, 'evidence_ids': [captured['evidence_id']],
            'evidence_hashes': {captured['evidence_id']: captured['snapshot_hash']}, 'status': 'confirmed'}
        self.services.audit_service.drafts.store = SimpleNamespace(db=self.store.connect)
        with self.store.transaction() as db:
            db.executescript('CREATE TABLE audit_finding_drafts(draft_id TEXT,workspace_id TEXT,task_id TEXT,revision INTEGER,body TEXT);'
                             'CREATE TABLE audit_draft_history(draft_id TEXT,revision INTEGER,body TEXT);')
            db.execute('INSERT INTO audit_finding_drafts VALUES(?,?,?,?,?)', ('finding1', 'wtest', self.ctx.session_id, 1, canonical(body)))
            db.execute('INSERT INTO audit_draft_history VALUES(?,?,?)', ('finding1', 1, canonical(body)))
        ref = AgentRef(kind='result', application='platform', workspace_id='wtest', object_id='finding1',
            parent_id=self.ctx.session_id, revision=1, sha256=digest(body), coverage={'kind': 'audit_draft'}).model_dump()
        self.assertReviewBlocked(ref)

    def test_draft_derived_fields_cannot_be_relabelled_formal_after_source_review(self):
        ref, _ = self.derived(); self.confirm()
        self.assertReviewBlocked(ref)

    def test_deterministic_confirmed_derivative_keeps_its_original_review(self):
        ref, _ = self.derived(provenance={'kind': 'manual_seal_fields', 'analysis_id': 'j1', 'snapshot_id': 'ms1',
            'snapshot_hash': 'd' * 64, 'draft': False, 'review_state': 'confirmed'})
        self.confirm()
        result = inspect_review_provenance(ref, self.ctx, require_review=True)
        self.assertFalse(result['draft']); self.assertFalse(any(check['status'] == 'failed' for check in result['checks']))

    def test_derived_provenance_hash_changes_and_source_cycles_are_rejected(self):
        ref, path = self.derived(); path.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            inspect_review_provenance(ref, self.ctx)

    def test_derived_reference_cycle_is_rejected(self):
        ref, _ = self.derived(cycle=True)
        with self.assertRaises(ValueError):
            inspect_review_provenance(ref, self.ctx)

    def test_manual_artifact_wrapper_does_not_remove_review_obligation(self):
        self.prepare()
        bundle = {'bundle_id': 'bundle1', 'kind': 'manual_analysis', 'platform_job_id': 'j1',
            'snapshot_id': 'ms1', 'snapshot_hash': 'd' * 64, 'draft': True, 'review_state': 'unreviewed',
            'inputs': [{'asset_id': 'asset1'}], 'files': []}
        self.services.artifact_center = SimpleNamespace(get=lambda *_: bundle)
        ref = AgentRef(kind='artifact', workspace_id='wtest', object_id='bundle1', revision=1).model_dump()
        self.assertReviewBlocked(ref)

    def test_published_audit_draft_keeps_full_fixed_evidence_provenance(self):
        captured, _ = self.evidence()
        content = {'evidence': [captured], 'drafts': [], 'task_id': self.ctx.session_id}
        checksum = digest(content)
        with self.store.transaction() as db:
            db.execute('CREATE TABLE audit_draft_bundle(bundle_id TEXT,workspace_id TEXT,status TEXT,snapshot TEXT,snapshot_hash TEXT)')
            db.execute('INSERT INTO audit_draft_bundle VALUES(?,?,?,?,?)', ('bundle1', 'wtest', 'ready', canonical(content), checksum))
        bundle = {'bundle_id': 'bundle1', 'kind': 'audit_draft', 'snapshot_hash': checksum,
            'inputs': [{'asset_id': 'asset1'}], 'files': []}
        self.services.artifact_center = SimpleNamespace(get=lambda *_: bundle, _connect=self.store.connect)
        self.assertReviewBlocked(AgentRef(kind='artifact', workspace_id='wtest', object_id='bundle1', revision=1))

    def test_formal_direct_evidence_path_rejects_unreviewed_wrapped_result(self):
        captured, _ = self.evidence()
        audit = object.__new__(AuditService)
        audit._source_context = lambda *_: self.ctx
        audit.drafts = self.services.audit_service.drafts
        with self.assertRaises(DataError):
            audit._direct_snapshot({'task_id': self.ctx.session_id, 'source_evidence_id': captured['evidence_id'],
                'snapshot_hash': captured['snapshot_hash']}, 'wtest', self.ctx)

    def test_native_audio_evidence_is_paginated_and_preserves_time_locators(self):
        self.prepare(); self.item['kind'] = self.snapshot['kind'] = 'audio'
        self.item['result'] = {'audio': {'duration_ms': 10000}, 'segments': [
            {'row_id': f'row{index}', 'text': str(index), 'start_ms': index * 1000, 'end_ms': (index + 1) * 1000,
                'source': {'schema_version': '1.0', 'kind': 'media_segment', 'asset_id': 'asset1',
                    'asset_revision': 1, 'blob_hash': 'a' * 64, 'precision': 'exact',
                    'start_ms': index * 1000, 'end_ms': (index + 1) * 1000}}
            for index in range(8)]}
        self.snapshot['analysis'] = deepcopy(self.item)
        self.ref = multimodal.result_ref(self.ctx, self.snapshot).model_dump()
        result = bind_snapshot(DirectEvidenceRequest(reference=self.ref, offset=3, limit=2), self.ctx)
        self.assertEqual([item['row_id'] for item in result['observation']['items']], ['row3', 'row4'])
        self.assertEqual(result['source_count'], 2)
        self.assertEqual([item['start_ms'] for item in result['sources']], [3000, 4000])
        self.assertTrue(result['observation']['draft'])

    def user_review(self):
        self.prepare()
        multimodal.request_action(multimodal.UserAction(action='review', job_id='j1'), self.ctx)
        with self.store.transaction() as db:
            db.execute('UPDATE decisions SET reply=?,request_id=? WHERE id=?', ('确认此版本', 'actual-user-reply', 'op1'))
        approved = deepcopy(self.snapshot)
        approved.update(snapshot_id='msReviewed', revision=2, review_revision=2, review_state='reviewed')
        approved['analysis'].update(revision=2, review={'assessment': 'confirmed', 'actor': 'Actual user'})
        self.native.snapshot_at_revision.return_value = approved
        self.native.snapshot.side_effect = lambda _workspace, _job, snapshot_id=None: deepcopy(approved if snapshot_id == 'msReviewed' else self.snapshot)
        return approved

    async def test_lost_review_reply_recovers_original_operation_without_another_review(self):
        self.user_review()
        receipts = {}
        def lookup(_workspace, kind, identifier):
            return {'found': kind == 'review' and identifier in receipts, 'receipt': receipts.get(identifier)}
        def lost_reply(_workspace, _job, body):
            self.item['revision'] = 2
            receipts[body['request_id']] = deepcopy(self.item)
            raise TimeoutError('native review response lost after durable save')
        self.native.lookup_operation.side_effect = lookup
        self.native.review.side_effect = lost_reply
        call = {'call_id': 'applyLost', 'tool': 'multimodal_apply_user_action', 'arguments': {'decision_id': 'op1'}}
        result = await self.registry.dispatch(call, self.ctx)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['result_refs'][0]['object_id'], 'msReviewed')
        self.assertEqual(result['result_refs'][0]['revision'], 2)
        self.assertEqual(self.native.review.call_count, 1)
        replay = await self.registry.dispatch(call, self.ctx)
        self.assertEqual(replay['result_refs'], result['result_refs'])
        self.assertEqual(self.native.review.call_count, 1)

    def test_recovered_review_receipt_does_not_follow_a_newer_running_attempt(self):
        self.user_review()
        receipt = {**deepcopy(self.item), 'revision': 2}
        self.item.update(revision=3, generation=2, status='running')
        self.native.lookup_operation.return_value = {'found': True, 'receipt': receipt}
        result = multimodal.apply_action(multimodal.ApplyAction(decision_id='op1'), replace(self.ctx, operation_id='originalApply'))
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['result_refs'][0]['object_id'], 'msReviewed')
        self.assertEqual(result['result_refs'][0]['revision'], 2)
        self.native.review.assert_not_called()

    def legacy_bundle(self, assessment='confirmed'):
        approved = self.user_review()
        approved['analysis']['review'] = {'assessment': assessment, 'actor': 'Actual user',
            'request_id': 'legacy-user-review', 'note': 'Saved user review'}
        analysis = deepcopy(approved['analysis'])
        analysis['result_hash'] = 'b' * 64
        self.native.lookup_operation.return_value = {'found': True, 'receipt': analysis}
        self.native.snapshot_at_revision.return_value = approved
        path = self.store.root / 'legacy-result.json'; payload = canonical(analysis).encode('utf-8'); path.write_bytes(payload)
        artifact = SimpleNamespace(artifact_id='artifact1', revision=1, blob_hash=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload), source_refs=[])
        bundle = {'bundle_id': 'legacyBundle', 'kind': 'manual_analysis', 'platform_job_id': 'j1', 'review_revision': 2,
            'inputs': [{'asset_id': 'asset1'}], 'files': [{'name': 'result.json'}]}
        self.services.artifact_center = SimpleNamespace(get=lambda *_: bundle, file=lambda *_: artifact,
            store=SimpleNamespace(stage_download=lambda *_, **__: nullcontext(path)))
        return AgentRef(kind='artifact', workspace_id='wtest', object_id='legacyBundle', revision=1).model_dump()

    def test_legacy_fixed_export_uses_original_confirmed_review_receipt(self):
        ref = self.legacy_bundle()
        result = inspect_review_provenance(ref, self.ctx, require_review=True)
        self.assertFalse(result['draft'], result)
        self.assertFalse(any(check['status'] == 'failed' for check in result['checks']), result)
        self.native.snapshot_at_revision.assert_called_once_with('wtest', 'j1', 2)

    def test_legacy_inconclusive_review_does_not_count_as_human_confirmation(self):
        ref = self.legacy_bundle('inconclusive')
        self.assertReviewBlocked(ref)

    def test_legacy_fixed_export_cannot_inherit_a_different_review_receipt(self):
        ref = self.legacy_bundle()
        self.native.lookup_operation.return_value['receipt'] = {**self.item, 'revision': 99}
        with self.assertRaises(ValueError):
            inspect_review_provenance(ref, self.ctx, require_review=True)

    def test_pending_reference_authorization_survives_a_later_asset_update(self):
        task = self.services.agent.tasks.get(self.ctx.session_id)
        self.services.agent.tasks.update(self.ctx.session_id, {'request_id': 'authFirst', 'expected_revision': task['revision'],
            'authorized_reference_ids': ['face1']})
        task = self.services.agent.tasks.get(self.ctx.session_id)
        self.services.agent.tasks.update(self.ctx.session_id, {'request_id': 'assetsSecond', 'expected_revision': task['revision'],
            'selected_assets': ['asset1', 'asset2']})
        pending = self.services.agent.tasks.get(self.ctx.session_id)['pending_scope']
        self.assertEqual(pending['authorized_reference_ids'], ['face1'])
        self.assertEqual(pending['selected_assets'], ['asset1', 'asset2'])

    def test_pending_assets_survive_a_later_reference_authorization(self):
        task = self.services.agent.tasks.get(self.ctx.session_id)
        self.services.agent.tasks.update(self.ctx.session_id, {'request_id': 'assetsFirst', 'expected_revision': task['revision'],
            'selected_assets': ['asset1', 'asset2']})
        task = self.services.agent.tasks.get(self.ctx.session_id)
        self.services.agent.tasks.update(self.ctx.session_id, {'request_id': 'authSecond', 'expected_revision': task['revision'],
            'authorized_reference_ids': ['face1']})
        pending = self.services.agent.tasks.get(self.ctx.session_id)['pending_scope']
        self.assertEqual(pending['authorized_reference_ids'], ['face1'])
        self.assertEqual(pending['selected_assets'], ['asset1', 'asset2'])

    def test_stop_with_job_cancellation_cannot_control_a_foreign_job_that_was_only_read(self):
        from workbench_platform.agent.runtime import AgentRuntime
        self.item['status'] = 'running'
        operation, _ = self.store.begin_operation(self.ctx.run_id, 1,
            {'call_id': 'foreignRead', 'tool': 'multimodal_read_result', 'arguments': {'job_id': 'j1'}})
        self.store.finish_operation(operation['id'], multimodal.read(multimodal.Result(job_id='j1'), self.ctx))
        runtime = object.__new__(AgentRuntime)
        runtime.store = self.store; runtime.graph = None; runtime.context = lambda _: self.ctx
        runtime.cancel(self.ctx.run_id, cancel_owned_jobs=True)
        self.native.control.assert_not_called()

    def test_result_identity_accepts_omitted_redundant_metadata(self):
        self.prepare()
        self.ref['coverage'] = {}
        result = verify(self.ref, self.ctx)
        self.assertEqual(result['snapshot_id'], 'ms1')
        self.assertEqual(result['content_sha256'], 'd' * 64)

    def test_result_identity_rejects_false_supplied_metadata(self):
        self.prepare()
        for key, invalid in [('kind', 'audio'), ('generation', 99)]:
            with self.subTest(key=key):
                reference = deepcopy(self.ref)
                reference['coverage'] = {key: invalid}
                with self.assertRaisesRegex(ValueError, '快照身份'):
                    verify(reference, self.ctx)

    def test_result_only_delivery_canonicalizes_metadata_and_exports_real_files(self):
        self.prepare()
        self.ref['coverage'] = {}
        path = self.store.root / '结果 01.csv'
        payload = '字段,值\n状态,未复核\n'.encode('utf-8')
        path.write_bytes(payload)
        checksum = hashlib.sha256(payload).hexdigest()
        self.native.snapshot_export.return_value = (self.snapshot, {path.name: path})
        self.native.file_hash.side_effect = lambda file: hashlib.sha256(file.read_bytes()).hexdigest()
        result = context.delivery(context.Delivery(summary='下载固定草稿', result_refs=[self.ref], finalize=False), self.ctx)
        self.assertEqual(result['status'], 'success', result)
        self.assertTrue(result['data']['draft'])
        self.assertEqual(result['result_refs'][0]['coverage']['kind'], 'face')
        self.assertEqual(result['result_refs'][0]['coverage']['generation'], 1)
        bundle = result['data']['delivery'][0]
        self.assertEqual((bundle['bundle_id'], bundle['kind'], bundle['snapshot_id'], bundle['native_job_id']),
                         ('ms1', 'multimodal_snapshot', 'ms1', 'j1'))
        self.assertEqual(bundle['files'][0]['sha256'], checksum)
        self.assertEqual(bundle['files'][0]['size_bytes'], len(payload))
        self.assertTrue(bundle['files'][0]['download_url'].endswith('/files/%E7%BB%93%E6%9E%9C%2001.csv'))
        self.assertTrue(bundle['download_url'].endswith('/jobs/j1/snapshots/ms1/download'))
        self.native.snapshot_export.assert_called_once_with('wtest', 'j1', 'ms1')

    def transfer(self, status='running', *, draft=True):
        if not self.store.rows('SELECT asset_id FROM derived_inputs WHERE workspace_id=? AND asset_id=?', ('wtest', 'derived1')):
            self.derived()
        self.item['kind'] = self.snapshot['kind'] = 'seal'
        self.snapshot['analysis']['kind'] = 'seal'
        self.data = Mock()
        self.services.data_workbench = self.data
        self.data.get.return_value = {'id': 'dataSession', 'workspace_id': 'wtest', 'version': 7}
        job = {'id': 'dataJob', 'session_id': 'dataSession', 'status': status}
        self.data.job.return_value = job
        self.data.operation.return_value = job
        value = {'job': job, 'session_id': 'dataSession', 'asset': {'asset_id': 'derived1',
            'asset_revision': 1, 'blob_hash': 'f' * 64}}
        result = multimodal.transfer_pending(value, self.snapshot, self.ctx, draft=draft)
        return result, result['job_refs'][0]

    def test_seal_transfer_waits_on_original_native_import_key(self):
        result, reference = self.transfer()
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(reference['application'], 'data')
        self.assertEqual(reference['parent_id'], 'dataSession')
        self.assertEqual(reference['coverage'], {'kind': 'multimodal_transfer', 'request_id': 'bridge',
            'analysis_job_id': 'j1', 'snapshot_id': 'ms1', 'derived_asset_id': 'derived1', 'draft': True})
        self.assertEqual(result['result_refs'][0]['object_id'], 'ms1')
        self.assertEqual(result['result_refs'][1]['object_id'], 'derived1')
        self.data.operation.assert_called_once_with('bridge')
        self.data.reset_mock()
        self.assertIsNone(adapter(reference).settle(reference, self.ctx))
        self.data.operation.assert_not_called()
        self.data.resume_operation.assert_not_called()
        self.native.draft_to_data.assert_not_called()

    def test_seal_transfer_recovery_resumes_native_key_and_keeps_original_job(self):
        _, reference = self.transfer('interrupted')
        self.data.reset_mock()
        self.data.resume_operation.return_value = {'id': 'dataJob', 'session_id': 'dataSession', 'status': 'running'}
        self.assertIsNone(adapter(reference).settle(reference, self.ctx))
        self.data.operation.assert_called_once_with('bridge')
        self.data.resume_operation.assert_called_once_with('bridge')
        self.native.draft_to_data.assert_not_called()

    def test_seal_transfer_recovery_rejects_an_unverified_original_operation(self):
        _, reference = self.transfer('interrupted')
        for receipt in [None, {'id': 'anotherJob', 'session_id': 'dataSession'},
                        {'id': 'dataJob', 'session_id': 'anotherSession'}]:
            with self.subTest(receipt=receipt):
                self.data.operation.return_value = receipt
                self.data.resume_operation.reset_mock()
                with self.assertRaises(ValueError):
                    adapter(reference).settle(reference, self.ctx)
                self.data.resume_operation.assert_not_called()

    def test_seal_transfer_creation_rejects_an_unrelated_session_receipt(self):
        self.transfer()
        self.data.operation.return_value = {'id': 'dataJob', 'session_id': 'anotherSession'}
        value = {'job': {'id': 'dataJob'}, 'session_id': 'dataSession',
            'asset': {'asset_id': 'derived1', 'asset_revision': 1, 'blob_hash': 'f' * 64}}
        with self.assertRaisesRegex(ValueError, '回执身份'):
            multimodal.transfer_pending(value, self.snapshot, self.ctx, draft=True)

    def test_seal_transfer_finished_keeps_fixed_source_and_draft_state(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                _, reference = self.transfer('succeeded', draft=draft)
                settled = {'status': 'success', 'summary': '导入完成', 'data': {'version': 8}, 'result_refs': []}
                with patch('workbench_platform.application_services.operations.data.settle', return_value=settled) as settle:
                    result = adapter(reference).settle(reference, self.ctx)
                settle.assert_called_once_with(reference, self.ctx)
                self.assertEqual(result['data']['snapshot_id'], 'ms1')
                self.assertEqual(result['data']['draft'], draft)
                self.assertEqual(result['data']['review_state'], 'unreviewed' if draft else 'reviewed')
                self.assertEqual([ref['object_id'] for ref in result['result_refs']], ['ms1', 'derived1'])
                self.assertEqual(result['result_refs'][1]['sha256'], 'f' * 64)
                self.data.resume_operation.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
