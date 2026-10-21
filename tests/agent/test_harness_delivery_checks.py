"""Content acceptance against real artifact storage, bundles, hashes and refs."""
from __future__ import annotations

from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from packages.contracts.agent import AgentRef
from packages.contracts.models import AssetSetItem, Counts, JobRef, SourceLocator
from workbench_platform.agent.registry import ToolContext, ToolRegistry
from workbench_platform.agent.store import AgentStore, now
from workbench_platform.agent.tools import context
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.assets.registry import AssetRegistry
from workbench_platform.assets.sets import AssetSetStore
from workbench_platform.store import WorkspaceStore


ROOT = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence/delivery-check-tests')


class FixtureNativeReadback:
    """Read-only synthetic native receipts; artifact bytes and storage are real."""
    def __init__(self):
        self.sessions, self.jobs = {}, {}

    def get(self, session_id):
        return self.sessions[session_id]

    def job(self, session_id, job_id):
        return dict(self.jobs[(session_id, job_id)])


class DeliveryChecksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        (self.root / 'fixtures').mkdir()
        (self.root / 'blobs').mkdir()
        (self.root / 'fixtures/source.csv').write_text('id,name\n000001,Source\n', encoding='utf-8')
        self.workspaces = WorkspaceStore(self.root / 'workspaces.sqlite3')
        self.workspaces.create_workspace('Acceptance', workspace_id='wtest')
        self.workspaces.create_workspace('Other', workspace_id='wother')
        self.assets = AssetRegistry(self.root / 'assets.sqlite3', self.root / 'blobs', {'fixtures': self.root / 'fixtures'})
        self.sets = AssetSetStore(self.assets)
        self.source = self.assets.register('wtest', 'fixtures', 'source.csv', media_type='text/csv', storage_mode='managed')
        self.locator = SourceLocator(schema_version='1.0', kind='file', asset_id=self.source.asset_id,
            asset_revision=self.source.revision, blob_hash=self.source.blob_hash, precision='exact')
        self.asset_set = self.sets.create('wtest', [AssetSetItem(schema_version='1.0', item_id='sourceItem',
            asset_id=self.source.asset_id, asset_revision=self.source.revision, blob_hash=self.source.blob_hash,
            source_locator=self.locator)])
        self.center = ArtifactCenter(self.root / 'artifact-center', self.assets, self.sets, None)
        self.registry = ToolRegistry()
        context.register(self.registry)
        self.store = AgentStore(self.root / 'agent')
        session = self.store.create_session('wtest', 'Acceptance', [self.source.asset_id], 'session')
        run, _ = self.store.start_run(session['id'], 'Verify artifact contents', 'run', 1)
        self.services = SimpleNamespace(workspace_store=self.workspaces, artifact_center=self.center,
                                        asset_library=(self.assets, self.sets, None), data_workbench=FixtureNativeReadback())
        self.ctx = ToolContext('wtest', session['id'], run['id'], 1, 0, '', (self.source.asset_id,), self.services, self.store)
        self.sequence = 0

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, payload, *, filename='cleaned.csv', workspace='wtest'):
        self.sequence += 1
        identifier = 'bundle' + str(self.sequence)
        path = self.root / ('output' + str(self.sequence) + '.csv')
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path = payload
        sha = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                sha.update(chunk)
        job = JobRef(schema_version='1.0', platform_job_id='job' + str(self.sequence), app_id='data', instance_id='data_local',
            native_job_id='job' + str(self.sequence), operation_id='export' + str(self.sequence),
            input_manifest_hash=self.asset_set.manifest_hash, native_status='succeeded', status='succeeded', stage='export',
            counts=Counts(schema_version='1.0', total=1, succeeded=1, failed=0, cancelled=0, pending=0),
            capabilities=[], last_event_seq=0, updated_at=now().replace('+00:00', 'Z'))
        self.services.data_workbench.sessions['native' + str(self.sequence)] = {'workspace_id': workspace}
        self.services.data_workbench.jobs[('native' + str(self.sequence), 'export' + str(self.sequence))] = {
            'status': job.status, 'counts': job.counts.model_dump()}
        with path.open('rb') as stream:
            artifact = self.center.store.publish_managed(workspace, stream, expected_hash=sha.hexdigest(),
                expected_size=path.stat().st_size, job_ref=job, source_refs=[self.locator] if workspace == 'wtest' else [],
                media_type='text/csv' if filename.endswith('.csv') else 'application/octet-stream',
                application_version='acceptance-test', rule_or_model_version='synthetic-csv-v1')
        # Use the actual ArtifactCenter catalog/display/file path, with a
        # fixture export receipt. Storage publication and source refs are real.
        with closing(self.center._connect()) as db:
            db.execute('INSERT INTO data_artifact_bundle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, workspace, 'native' + str(self.sequence), 'Acceptance export', 'export' + str(self.sequence),
                 None, sha.hexdigest(), self.asset_set.asset_set_id, self.asset_set.revision,
                 json.dumps([{'asset_id': self.source.asset_id}]) if workspace == 'wtest' else '[]',
                 json.dumps({filename: artifact.artifact_id}), 'ready', now()))
        return AgentRef(kind='artifact', workspace_id=workspace, object_id=identifier, revision=1).model_dump(), artifact

    async def deliver(self, refs, checks=(), *, result_refs=()):
        self.sequence += 1
        call = {'call_id': 'delivery' + str(self.sequence), 'tool': 'delivery_publish',
                'arguments': {'summary': 'Verified synthetic export', 'artifact_refs': refs, 'checks': list(checks),
                              'result_refs': list(result_refs)}}
        self.registry.validate_batch([call], self.registry.tools)
        return await self.registry.dispatch(call, self.ctx)

    def check(self, ref, name, **fields):
        return {'name': name, 'bundle_id': ref['object_id'], 'filename': 'cleaned.csv', **fields}

    async def test_100k_rows_preserve_zeroes_decimals_and_trim_with_full_readback(self):
        path = self.root / 'large.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['c1 | row_id', 'c2 | supplier', 'c3 | amount', 'source_row'])
            for index in range(100_000):
                writer.writerow([f'{index:08d}', 'ACME', '10.00', index + 1])
        ref, artifact = self.publish(path)
        requirements = [self.check(ref, 'row_count', expected_rows=100_000),
            self.check(ref, 'column_trimmed', column='supplier'),
            self.check(ref, 'column_prefix', column='row_id', prefix='000'),
            self.check(ref, 'column_text_equals', column='amount', expected_value='10.00'),
            self.check(ref, 'column_decimal_places', column='c3 | amount', places=2)]
        started = time.perf_counter()
        result = await self.deliver([ref], requirements)
        elapsed = time.perf_counter() - started
        self.assertEqual(result['status'], 'success', result)
        self.assertTrue(result['data']['verified'])
        outcomes = [item for item in result['data']['delivery_checks'] if item['name'] != 'native_job_status']
        self.assertEqual(len([item for item in result['data']['delivery_checks'] if item['name'] == 'native_job_status' and item['status'] == 'passed']), 1)
        self.assertEqual(outcomes[0]['status'], 'measured')
        self.assertEqual(len(outcomes), 6)
        for outcome in outcomes:
            self.assertEqual(outcome['evidence']['sha256'], artifact.blob_hash)
            self.assertEqual(outcome['evidence']['measured_rows'], 100_000)
            self.assertTrue(outcome['evidence']['complete'])
        self.assertTrue(all(value['status'] == 'passed' for value in outcomes[1:]))
        self.assertEqual(outcomes[2]['evidence']['resolved_column'], 'c2 | supplier')
        (ROOT / 'large-csv-acceptance.json').write_text(json.dumps({'rows': 100_000, 'sha256': artifact.blob_hash,
            'seconds': elapsed, 'delivery_checks': outcomes}, ensure_ascii=False, indent=2), encoding='utf-8')

    async def test_no_requirements_collects_measurements_without_claiming_requirement_match(self):
        ref, _ = self.publish(b'id,supplier\n000001,ACME\n000002,OTHER\n')
        result = await self.deliver([ref])
        self.assertEqual(result['status'], 'success')
        outcomes = [item for item in result['data']['delivery_checks'] if item['name'] != 'native_job_status']
        self.assertEqual([value['status'] for value in outcomes], ['measured'])
        self.assertEqual(outcomes[0]['evidence']['measured_rows'], 2)
        self.assertNotIn('requirement', outcomes[0])

    async def test_trim_failure_counts_all_rows_and_keeps_only_bounded_row_numbers(self):
        ref, _ = self.publish(b'id,supplier\n' + b'000001, ACME \n' * 20)
        result = await self.deliver([ref], [self.check(ref, 'column_trimmed', column='supplier')])
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['data']['verified'])
        self.assertEqual(result['error']['code'], 'delivery_check_failed')
        evidence = result['data']['delivery_checks'][1]['evidence']
        self.assertEqual(evidence['failed_rows'], 20)
        self.assertEqual(evidence['failure_rows'], [1, 2, 3, 4, 5])
        self.assertNotIn(' ACME ', json.dumps(result))

    async def test_wrong_row_count_is_an_ordinary_failed_check(self):
        ref, _ = self.publish(b'id\n000001\n000002\n')
        result = await self.deliver([ref], [self.check(ref, 'row_count', expected_rows=3)])
        self.assertEqual(result['status'], 'error')
        check = result['data']['delivery_checks'][1]
        self.assertEqual(check['status'], 'failed')
        self.assertEqual(check['evidence']['measured_rows'], 2)
        self.assertEqual(check['requirement']['expected_rows'], 3)

    async def test_numeric_coercion_is_never_used_for_text_or_decimal_requirements(self):
        ref, _ = self.publish(b'id,amount\n000001,10.00\n000001,10.0\n000001,1e1\n')
        result = await self.deliver([ref], [self.check(ref, 'column_text_equals', column='id', expected_value='1'),
                                           self.check(ref, 'column_decimal_places', column='amount', places=2)])
        checks = result['data']['delivery_checks'][1:]
        self.assertEqual(result['status'], 'error')
        self.assertEqual(checks[0]['evidence']['failed_rows'], 3)
        self.assertEqual(checks[1]['evidence']['matched_rows'], 1)
        self.assertEqual(checks[1]['evidence']['failed_rows'], 2)

    async def test_ambiguous_suffix_and_missing_columns_fail_exact_header_still_works(self):
        ref, _ = self.publish(b'c1 | supplier,c2 | supplier\nACME,ACME\n')
        for column in ('supplier', 'absent'):
            result = await self.deliver([ref], [self.check(ref, 'column_trimmed', column=column)])
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['data']['delivery_checks'][1]['status'], 'failed')
        exact = await self.deliver([ref], [self.check(ref, 'column_trimmed', column='c1 | supplier')])
        self.assertEqual(exact['status'], 'success')

    async def test_duplicate_exact_headers_are_ambiguous(self):
        ref, _ = self.publish(b'name,name\nACME,OTHER\n')
        result = await self.deliver([ref], [self.check(ref, 'column_trimmed', column='name')])
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['data']['delivery_checks'][1]['evidence']['resolved_column'])

    async def test_checks_cannot_address_unpublished_other_bundle_or_unknown_filename(self):
        first, _ = self.publish(b'id\n000001\n')
        other, _ = self.publish(b'id\n000002\n')
        for check in (self.check(other, 'row_count', expected_rows=1),
                      {**self.check(first, 'row_count', expected_rows=1), 'filename': '../cleaned.csv'}):
            result = await self.deliver([first], [check])
            self.assertEqual(result['status'], 'error')
            self.assertFalse(result['data']['verified'])
        other_workspace, _ = self.publish(b'id\n000002\n', workspace='wother')
        result = await self.deliver([other_workspace], [self.check(other_workspace, 'row_count', expected_rows=1)])
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result.get('data', {}).get('verified', False))

    async def test_invalid_csv_shape_encoding_and_quotes_fail_automatic_readback(self):
        for payload in (b'id,name\n1\n', b'id\n\xff\n', b'id\n"unfinished\n', b''):
            ref, _ = self.publish(payload)
            result = await self.deliver([ref])
            self.assertEqual(result['status'], 'error')
            self.assertFalse(result['data']['verified'])
            self.assertFalse(result['data']['delivery_checks'][0]['evidence']['complete'])

    async def test_multiline_fields_and_empty_rows_follow_csv_record_semantics(self):
        ref, _ = self.publish(b'id,note\r\n000001,"first\nsecond"\r\n\r\n000002,""\r\n')
        result = await self.deliver([ref], [self.check(ref, 'row_count', expected_rows=2),
                                           self.check(ref, 'column_prefix', column='id', prefix='000')])
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['data']['delivery_checks'][0]['evidence']['measured_rows'], 2)

    async def test_corrupt_blob_is_rejected_by_native_hash_before_content_acceptance(self):
        ref, artifact = self.publish(b'id\n000001\n')
        self.center.store._blob(artifact.blob_hash).write_bytes(b'id\n000002\n')
        result = await self.deliver([ref], [self.check(ref, 'row_count', expected_rows=1)])
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result.get('data', {}).get('verified', False))

    async def test_result_reference_only_remains_backward_compatible(self):
        source_ref = AgentRef(kind='asset', workspace_id='wtest', object_id=self.source.asset_id,
                              revision=self.source.revision, sha256=self.source.blob_hash).model_dump()
        result = await self.deliver([], result_refs=[source_ref])
        self.assertEqual(result['status'], 'success', result)
        self.assertTrue(result['data']['verified'])
        self.assertEqual(result['data']['delivery_checks'], [])

    def test_checks_are_strict_bounded_and_require_exact_parameters(self):
        base = {'name': 'row_count', 'bundle_id': 'bundle', 'filename': 'cleaned.csv'}
        for fields in ({}, {'expected_rows': -1}, {'expected_rows': True}, {'expected_rows': 1, 'column': 'id'},
                       {'expected_rows': 1, 'unexpected': 'value'}):
            with self.assertRaises(ValidationError):
                context.DeliveryCheck.model_validate({**base, **fields})
        for fields in ({'name': 'column_decimal_places', 'column': 'amount', 'places': 31},
                       {'name': 'column_prefix', 'column': 'id', 'prefix': ''}):
            with self.assertRaises(ValidationError):
                context.DeliveryCheck.model_validate({**base, **fields})
        with self.assertRaises(ValidationError):
            context.Delivery.model_validate({'summary': 'test', 'checks': [{**base, 'expected_rows': 1}] * 51})


if __name__ == '__main__':
    unittest.main(verbosity=2)
