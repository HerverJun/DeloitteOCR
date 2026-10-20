"""Real saved-draft references and repair-aware delivery over immutable history."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_harness_delivery_checks as delivery_tests
from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.agent.tasks import AuditTasks
from workbench_platform.agent.tools import audit
from workbench_platform.agent.tools.context import _delivery_failures
from workbench_platform.application_services.references import verify, reference_location
from workbench_platform.audit.service import AuditService


class DraftDeliveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = delivery_tests.DeliveryChecksTests.setUp
    tearDown = delivery_tests.DeliveryChecksTests.tearDown
    publish = delivery_tests.DeliveryChecksTests.publish
    deliver = delivery_tests.DeliveryChecksTests.deliver
    check = delivery_tests.DeliveryChecksTests.check

    async def asyncSetUp(self):
        self.services.agent = SimpleNamespace(store=self.store, services=self.services,
            connection=SimpleNamespace(view=lambda: {'model': 'local-test'}))
        self.services.agent.tasks = AuditTasks(self.services.agent)
        self.services.batch_service = SimpleNamespace(workspace=lambda identifier: self.workspaces.get_workspace(identifier))
        self.services.audit_service = AuditService(SimpleNamespace(state=self.services), self.root / 'audit')
        audit.register(self.registry)

    async def tool(self, name, arguments):
        self.sequence += 1
        call = {'call_id': 'audit' + str(self.sequence), 'tool': name, 'arguments': arguments}
        self.registry.validate_batch([call], self.registry.tools)
        return await self.registry.dispatch(call, self.ctx)

    def record(self, tool, arguments, result):
        self.sequence += 1
        operation, _ = self.store.begin_operation(self.ctx.run_id, self.ctx.generation,
            {'call_id': 'native' + str(self.sequence), 'tool': tool, 'arguments': arguments})
        self.store.finish_operation(operation['id'], AgentToolResult.model_validate(result).model_dump())
        return operation['id']

    def source_reference(self):
        return AgentRef(kind='asset', workspace_id='wtest', object_id=self.source.asset_id,
                        revision=self.source.revision, sha256=self.source.blob_hash).model_dump()

    async def test_save_read_verify_and_deliver_draft_without_exporting_any_file(self):
        self.services.agent.connection = SimpleNamespace(view=lambda: {'model': 'changed-default'},
            resolve=lambda revision: ({'model': 'frozen-run-model'}, 'not-returned-in-author'))
        written = await self.tool('audit_write_finding', {'expected_revision': 0, 'title': '待补资料', 'missing_information': '请补充台账'})
        self.assertEqual(written['status'], 'success', written)
        self.assertEqual(written['data']['author']['model'], 'frozen-run-model')
        self.assertNotIn('not-returned-in-author', json.dumps(written))
        reference = written['result_refs'][0]
        self.assertEqual(reference['kind'], 'result')
        self.assertEqual(reference['coverage']['kind'], 'audit_draft')
        self.assertEqual(reference['parent_id'], self.ctx.session_id)
        saved = verify(reference, self.ctx)
        self.assertEqual(saved['missing_information'], '请补充台账')
        read = await self.tool('audit_read_finding', {'draft_id': reference['object_id']})
        self.assertEqual(read['result_refs'], written['result_refs'])
        result = await self.deliver([], result_refs=written['result_refs'])
        self.assertEqual(result['status'], 'success', result)
        self.assertTrue(result['data']['verified'])
        self.assertEqual(result['data']['completeness'], 'complete')
        self.assertEqual(result['data']['delivery'], [])
        self.assertEqual(self.center.list('wtest', 0, 50)['total'], 0)
        self.assertIn('/findings?', reference_location(reference))
        self.assertIn('sha256=' + reference['sha256'], reference_location(reference))

    async def test_exact_history_revision_hash_parent_and_workspace_are_enforced(self):
        first = await self.tool('audit_write_finding', {'expected_revision': 0, 'title': '原草稿', 'missing_information': '原信息'})
        original = first['result_refs'][0]
        second = await self.tool('audit_write_finding', {'draft_id': original['object_id'], 'expected_revision': 1,
                                                        'title': '新草稿', 'missing_information': '新信息'})
        latest = second['result_refs'][0]
        self.assertEqual(verify(original, self.ctx)['title'], '原草稿')
        self.assertEqual(verify(latest, self.ctx)['title'], '新草稿')
        self.assertNotEqual(original['sha256'], latest['sha256'])
        for changes in ({'revision': 2}, {'sha256': '0' * 64}, {'parent_id': 'wrongTask'},
                        {'workspace_id': 'wother'}, {'revision': '1'}):
            with self.assertRaises(ValueError):
                verify({**original, **changes}, self.ctx)
        with self.services.audit_service.store.transaction() as db:
            body = json.loads(db.execute('SELECT body FROM audit_draft_history WHERE draft_id=? AND revision=1',
                                         (original['object_id'],)).fetchone()['body'])
            body['title'] = 'Tampered'
            db.execute('UPDATE audit_draft_history SET body=? WHERE draft_id=? AND revision=1',
                       (json.dumps(body), original['object_id']))
        with self.assertRaises(ValueError):
            verify(original, self.ctx)

    async def test_draft_evidence_sources_are_verified_against_current_reader_scope(self):
        evidence = await self.tool('audit_bind_evidence', {'reference': self.source_reference()})
        self.assertEqual(evidence['status'], 'success', evidence)
        draft = await self.tool('audit_write_finding', {'expected_revision': 0, 'title': '已核实原件', 'facts': '原件含供应商信息',
                                                      'evidence_ids': [evidence['data']['evidence_id']]})
        reference = draft['result_refs'][0]
        self.assertEqual(reference['sources'][0]['object_id'], evidence['data']['evidence_id'])
        self.assertEqual(verify(reference, self.ctx)['source_assets'], [self.source.asset_id])
        with self.assertRaises(ValueError):
            verify(reference, replace(self.ctx, selected_assets=('differentAsset',)))

    async def test_corrected_revision_conflict_becomes_complete_and_original_error_is_preserved(self):
        failure = await self.tool('audit_write_finding', {'expected_revision': 1, 'title': '待补', 'missing_information': '缺资料'})
        self.assertEqual(failure['error']['code'], 'revision_conflict')
        corrected = await self.tool('audit_write_finding', {'expected_revision': 0, 'title': '待补', 'missing_information': '缺资料'})
        result = await self.deliver([], result_refs=corrected['result_refs'])
        self.assertEqual(result['data']['completeness'], 'complete', result)
        self.assertEqual(len(result['data']['repaired_operations']), 1)
        errors = self.store.rows("SELECT result FROM operations WHERE json_extract(result,'$.error.code')='revision_conflict'")
        self.assertEqual(len(errors), 1)
        self.assertEqual(json.loads(errors[0]['result'])['status'], 'error')

    async def test_success_on_different_draft_does_not_erase_failure_on_original_draft(self):
        original = await self.tool('audit_write_finding', {'expected_revision': 0, 'title': 'A', 'missing_information': 'A'})
        identifier = original['data']['draft_id']
        await self.tool('audit_write_finding', {'draft_id': identifier, 'expected_revision': 99, 'title': 'A', 'missing_information': 'A'})
        unrelated = await self.tool('audit_write_finding', {'expected_revision': 0, 'title': 'B', 'missing_information': 'B'})
        partial = await self.deliver([], result_refs=unrelated['result_refs'])
        self.assertEqual(partial['data']['completeness'], 'partial', partial)
        repaired = await self.tool('audit_write_finding', {'draft_id': identifier, 'expected_revision': 1, 'title': 'A repaired', 'missing_information': 'A'})
        complete = await self.deliver([], result_refs=repaired['result_refs'])
        self.assertEqual(complete['data']['completeness'], 'complete', complete)

    async def test_native_failed_coverage_and_unprocessed_regions_remain_partial_after_later_success(self):
        arguments = {'data_session_id': 'nativeSession', 'side': 'left', 'step': {'operator': 'trim', 'column': 'c2'}}
        for details in ({'coverage': {'failed': 1}}, {'data': {'native_status': 'failed'}},
                        {'data': {'job': {'counts': {'cancelled': 1}}}}, {'warnings': ['未处理区域仍需复核']}):
            self.record('data_apply_step', arguments, {'status': 'error', 'summary': 'Native incomplete',
                'error': {'code': 'failed', 'message': 'Native incomplete'}, **details})
        self.record('data_apply_step', {**arguments, 'expected_version': 2}, {'status': 'success', 'summary': 'Later completed'})
        result = await self.deliver([], result_refs=[self.source_reference()])
        self.assertEqual(result['data']['completeness'], 'partial', result)
        self.assertEqual(len(result['data']['limitations']), 4)
        self.assertEqual(result['data']['repaired_operations'], [])

    async def test_same_csv_same_requirements_repair_transient_readback_without_erasing_history(self):
        reference, _ = self.publish(b'id\n000001\n000002\n')
        checks = [self.check(reference, 'row_count', expected_rows=2)]
        original_open = Path.open
        def unavailable(path, *args, **kwargs):
            if kwargs.get('encoding') == 'utf-8-sig':
                raise OSError('Transient CSV readback unavailable')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', unavailable):
            failed = await self.deliver([reference], checks)
        self.assertEqual(failed['error']['code'], 'delivery_check_failed')
        repaired = await self.deliver([reference], checks)
        self.assertEqual(repaired['status'], 'success')
        self.assertEqual(repaired['data']['completeness'], 'complete', repaired)
        self.assertEqual(len(repaired['data']['repaired_operations']), 1)
        self.assertEqual(len(self.store.rows("SELECT id FROM operations WHERE json_extract(result,'$.error.code')='delivery_check_failed'")), 1)

    async def test_other_artifact_or_changed_requirement_cannot_clear_failed_acceptance(self):
        original, _ = self.publish(b'id\n000001\n000002\n')
        await self.deliver([original], [self.check(original, 'row_count', expected_rows=3)])
        changed = await self.deliver([original], [self.check(original, 'row_count', expected_rows=2)])
        self.assertEqual(changed['data']['completeness'], 'partial', changed)
        other, _ = self.publish(b'id\n000001\n000002\n000003\n')
        unrelated = await self.deliver([other], [self.check(other, 'row_count', expected_rows=3)])
        self.assertEqual(unrelated['data']['completeness'], 'partial', unrelated)
        self.assertEqual(unrelated['data']['repaired_operations'], [])


class DraftReferenceUnitTests(unittest.TestCase):
    def test_unknown_receipt_is_not_erased_by_another_success_on_the_same_object(self):
        records = [{'id': 'unknown', 'tool': 'data_apply_step', 'arguments': {'data_session_id': 'data'},
                    'result': {'status': 'error', 'summary': 'Unknown receipt', 'error': {'code': 'response_unknown'}}},
                   {'id': 'new', 'tool': 'data_apply_step', 'arguments': {'data_session_id': 'data'},
                    'result': {'status': 'success', 'summary': 'Different operation succeeded'}}]
        failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'delivery')
        self.assertEqual([value['summary'] for value in failed], ['Unknown receipt'])
        self.assertEqual(repaired, [])

    def test_author_uses_frozen_connection_revision_without_exposing_secret(self):
        resolved = []
        def resolve(revision):
            resolved.append(revision)
            return {'model': 'original-run-model'}, 'private-not-in-author'
        ctx = SimpleNamespace(run_id='run', session_id='task', store=SimpleNamespace(run=lambda _: {'connection_revision': 2}),
            services=SimpleNamespace(agent=SimpleNamespace(connection=SimpleNamespace(resolve=resolve,
                view=lambda: {'model': 'changed-default-model'}))))
        value = audit.author(ctx)
        self.assertEqual(resolved, [2])
        self.assertEqual(value['model'], 'original-run-model')
        self.assertNotIn('private-not-in-author', json.dumps(value))

    def test_version_location_includes_the_pinned_snapshot_hash(self):
        reference = AgentRef(kind='result', application='platform', workspace_id='workspace', object_id='draft',
            parent_id='task', revision=3, sha256='a' * 64, coverage={'kind': 'audit_draft'})
        location = reference_location(reference.model_dump())
        self.assertIn('session=task', location)
        self.assertIn('draft=draft', location)
        self.assertIn('revision=3', location)
        self.assertIn('sha256=' + 'a' * 64, location)


if __name__ == '__main__':
    unittest.main(verbosity=2)
