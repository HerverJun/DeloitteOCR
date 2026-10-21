"""Real TXT bytes/provenance; synthetic, read-only fixed native OCR receipts."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest
import test_harness_delivery_checks as fixtures

from packages.contracts.agent import AgentRef
from workbench_platform.agent.tools.context import _delivery_failures, _persistent_incompleteness
from workbench_platform.application_services.identity import canonical, digest
from workbench_platform.application_services.references import verify as real_verify
from workbench_platform.artifacts.native_exports import publish
from workbench_platform.artifacts.text_outputs import TEXT_RULE_VERSION, verify_text_artifact


ROOT = Path('F:/OCR/output/user-flow-fixes-20261018/evidence/delivery-text-artifact-tests')


class DeliveryTextArtifactTests(unittest.IsolatedAsyncioTestCase):
    tearDown = fixtures.DeliveryChecksTests.tearDown
    deliver = fixtures.DeliveryChecksTests.deliver

    def setUp(self):
        root_patch = patch.object(fixtures, 'ROOT', ROOT)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        fixtures.DeliveryChecksTests.setUp(self)
        self.services.agent = SimpleNamespace(registry=self.registry)
        self.units, self.values, self.refs = [], {}, []
        for page, amount in ((2, '100.20'), (5, '118.75')):
            text = f'第{page}页\n编号：00000{page}\n金额：{amount}\n' + ('完整原文，保留空格  与精度。\n' * 400)
            snapshot_hash = hashlib.sha256(('native snapshot ' + text).encode()).hexdigest()
            ref = AgentRef(kind='result', workspace_id='wtest', application='ocr', object_id='result' + str(page),
                parent_id='handoff', native_id='document', revision=1, sha256=snapshot_hash,
                coverage={'page_number': page})
            self.refs.append(ref)
            self.values[ref.object_id] = {'snapshot': {'available': True, 'edited': {'text': text}},
                'source_asset': {'asset_id': self.source.asset_id, 'asset_revision': self.source.revision,
                    'blob_hash': self.source.blob_hash, 'source_locator': self.locator.model_dump(mode='json')},
                'locator': {'page_number': page}, 'content_sha256': snapshot_hash}
            self.units.append({'asset_id': self.source.asset_id, 'asset_revision': self.source.revision,
                'blob_hash': self.source.blob_hash, 'operation': 'extract_text',
                'scope': {'kind': 'page', 'page_number': page}, 'engine': 'ppocr', 'state': 'succeeded',
                'result_id': ref.object_id, 'native_task_id': 'task' + str(page)})
        self.original_job = AgentRef(kind='job', workspace_id='wtest', application='ocr', object_id='originalJob',
            parent_id='handoff', native_id='document', revision=1, coverage={'kind': 'pdf_pages'})
        self.remember('ocr_process_pages', {'handoff_id': 'handoff'}, {'status': 'success', 'summary': 'Two fixed pages',
            'coverage': {'total': 2, 'succeeded': 2, 'failed': 0},
            'job_refs': [self.original_job.model_dump()], 'result_refs': [ref.model_dump() for ref in self.refs]})
        for target, replacement in (
            ('workbench_platform.application_services.references.verify', self.native_readback),
            ('workbench_platform.application_services.requirement_coverage.OcrCoverageReader.attempt', lambda _: deepcopy(self.units)),
            ('workbench_platform.application_services.requirement_coverage.OcrCoverageReader.result',
                lambda _, identifier: deepcopy(next(unit for unit in self.units if unit['result_id'] == identifier))),
        ):
            native_patch = patch(target, side_effect=replacement)
            native_patch.start()
            self.addCleanup(native_patch.stop)
        job_patch = patch('workbench_platform.application_services.operations.jobs.read_reference',
            side_effect=lambda reference, _: self.job_readback(reference))
        self.job_reads = job_patch.start()
        self.addCleanup(job_patch.stop)

    def native_readback(self, reference, ctx):
        ref = AgentRef.model_validate(reference)
        if ref.kind == 'result' and ref.application == 'ocr':
            expected = next(item for item in self.refs if item.object_id == ref.object_id)
            fields = ('kind', 'workspace_id', 'application', 'object_id', 'parent_id', 'native_id', 'revision', 'sha256')
            if any(getattr(ref, key) != getattr(expected, key) for key in fields):
                raise ValueError('Fixed native result identity changed')
            return deepcopy(self.values[ref.object_id])
        return real_verify(reference, ctx)

    def job_readback(self, reference):
        assert reference['object_id'] == 'originalJob'
        return {'status': 'succeeded'}

    def remember(self, tool, arguments, value):
        self.sequence += 1
        record, _ = self.store.begin_operation(self.ctx.run_id, self.ctx.generation,
            {'call_id': 'history-' + str(self.sequence), 'tool': tool, 'arguments': arguments})
        self.store.finish_operation(record['id'], value)
        return record['id']

    def text_bundle(self, ref, *, mutate=None, omit=(), text_override=None):
        value = self.values[ref.object_id]
        text = value['snapshot']['edited']['text']
        identity = {'rule_version': TEXT_RULE_VERSION, 'reference': ref.model_dump(),
            'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        source = value['source_asset']
        locator = {**source['source_locator'], 'kind': 'page', 'page_number': value['locator']['page_number'], 'precision': 'page_only'}
        provenance = {**identity, 'source': deepcopy(source), 'source_locator': locator, 'text_total_chars': len(text)}
        if mutate:
            mutate(provenance)
        files = {'ocr-text.txt': ((text if text_override is None else text_override).encode(), 'text/plain'),
            'text-provenance.json': (canonical(provenance).encode(), 'application/json')}
        for name in omit:
            files.pop(name)
        bundle = publish(replace(self.ctx, operation_id='o' + digest(identity)[:48]), 'OCR 原文', files,
            [{'asset_id': source['asset_id'], 'revision': source['asset_revision'], 'blob_hash': source['blob_hash'],
                'source_locator': locator}], [locator], TEXT_RULE_VERSION)
        return AgentRef(kind='artifact', workspace_id='wtest', object_id=bundle['bundle_id'], revision=1).model_dump()

    async def test_corrected_artifact_only_delivery_recovers_full_pages_without_resubmission(self):
        refs = [self.text_bundle(ref) for ref in self.refs]
        before = {item['object_id']: self.center.get('wtest', item['object_id']) for item in refs}
        typo = {**refs[1], 'object_id': refs[1]['object_id'][:-4]}
        refused = await self.deliver([refs[0], typo])
        self.assertEqual(refused['error']['code'], 'invalid_input', refused)
        failed_row = self.store.rows('SELECT id,result FROM operations ORDER BY rowid DESC LIMIT 1')[0]
        outcome = await self.deliver(refs)
        self.assertEqual(outcome['status'], 'success', outcome)
        self.assertEqual(outcome['data']['completeness'], 'complete', outcome)
        coverage = outcome['data']['requirement_coverage']
        self.assertEqual((coverage['total'], coverage['satisfied'], coverage['missing']), (2, 2, 0))
        self.assertEqual(len(coverage['text_artifact_proofs']), 2)
        self.assertEqual(len(outcome['result_refs']), 2)
        self.assertEqual(len(outcome['data']['readable_text']), 2)
        self.assertEqual(outcome['data']['limitations'], [])
        self.assertEqual(outcome['data']['nonblocking_operations'][0]['reason'], 'delivery_request_refused')
        self.assertEqual(json.loads(self.store.rows('SELECT result FROM operations WHERE id=?', (failed_row['id'],))[0]['result']),
            json.loads(failed_row['result']))
        for item in refs:
            self.assertEqual(self.center.get('wtest', item['object_id']), before[item['object_id']])
        native = self.store.rows("SELECT * FROM operations WHERE tool='ocr_process_pages'")
        self.assertEqual(len(native), 1)
        self.assertEqual(self.job_reads.call_count, 1)

    async def test_one_page_or_summary_only_does_not_satisfy_both_pages(self):
        ref = self.text_bundle(self.refs[0])
        first = await self.deliver([ref])
        self.assertEqual(first['data']['completeness'], 'partial')
        self.assertEqual(first['data']['requirement_coverage']['missing'], 1)
        # A real CSV summary has valid bytes, but supplies no OCR text proof.
        bundle = publish(replace(self.ctx, operation_id='oSummary'), 'Summary',
            {'summary.txt': (b'Amounts differ', 'text/plain')}, [], [], 'summary-only-v1')
        ref = AgentRef(kind='artifact', workspace_id='wtest', object_id=bundle['bundle_id'], revision=1).model_dump()
        summary = await self.deliver([ref])
        self.assertEqual(summary['data']['completeness'], 'partial')
        self.assertGreaterEqual(summary['data']['requirement_coverage']['missing'], 1)

    async def test_missing_txt_or_sidecar_never_counts_as_full_text(self):
        refs = [self.text_bundle(self.refs[0], omit=('ocr-text.txt',)),
            self.text_bundle(self.refs[1], omit=('text-provenance.json',))]
        outcome = await self.deliver(refs)
        self.assertEqual(outcome['data']['completeness'], 'partial', outcome)
        self.assertEqual(outcome['data']['requirement_coverage']['missing'], 2)

    async def test_explicit_and_derived_fixed_result_are_not_duplicated(self):
        refs = [self.text_bundle(ref) for ref in self.refs]
        outcome = await self.deliver(refs, result_refs=[ref.model_copy(update={'coverage': {}}).model_dump() for ref in self.refs])
        self.assertEqual(outcome['data']['completeness'], 'complete', outcome)
        self.assertEqual(len(outcome['result_refs']), 2)
        self.assertEqual(len(outcome['data']['readable_text']), 2)

    async def test_derived_refs_are_reverified_from_staged_artifacts_without_enlarging_input_requirements(self):
        from workbench_platform.agent import delivery_staging
        refs = [self.text_bundle(ref) for ref in self.refs]
        self.sequence += 1
        call = {'call_id': 'stage-' + str(self.sequence), 'tool': 'delivery_publish',
            'arguments': {'summary': 'Both full original pages', 'finalize': False, 'artifact_refs': refs}}
        staged = await self.registry.dispatch(call, self.ctx)
        self.assertEqual(staged['status'], 'success', staged)
        stages, _ = delivery_staging.snapshot(self.ctx)
        self.assertEqual(stages[0]['result_refs'], [])
        self.assertEqual(len(stages[0]['artifact_refs']), 2)
        final = await self.registry.dispatch({'call_id': 'final-from-stages', 'tool': 'delivery_publish',
            'arguments': {'summary': 'Final verification of both original pages'}}, self.ctx)
        self.assertEqual(final['data']['completeness'], 'complete', final)
        self.assertEqual(len(final['result_refs']), 2)
        self.assertEqual(len(final['data']['requirement_coverage']['text_artifact_proofs']), 2)


@pytest.mark.parametrize('case', ['wrong_page', 'wrong_revision', 'wrong_hash', 'wrong_source_revision',
    'wrong_source_hash', 'truncated_txt', 'summary_txt', 'wrong_text_hash', 'wrong_length'])
def test_text_sidecar_claims_cannot_override_native_identity_or_full_bytes(case):
    fixture = DeliveryTextArtifactTests()
    fixture.setUp()
    try:
        def mutate(value):
            if case == 'wrong_page':
                value['source_locator']['page_number'] = 5
            elif case == 'wrong_revision':
                value['reference']['revision'] = 2
            elif case == 'wrong_hash':
                value['reference']['sha256'] = 'f' * 64
            elif case == 'wrong_source_revision':
                value['source']['asset_revision'] = 2
            elif case == 'wrong_source_hash':
                value['source']['blob_hash'] = 'f' * 64
            elif case == 'wrong_text_hash':
                value['text_sha256'] = 'f' * 64
            elif case == 'wrong_length':
                value['text_total_chars'] -= 1
        text = fixture.values[fixture.refs[0].object_id]['snapshot']['edited']['text']
        override = text[:80] if case == 'truncated_txt' else 'Amounts differ' if case == 'summary_txt' else None
        ref = fixture.text_bundle(fixture.refs[0], mutate=mutate, text_override=override)
        bundle = real_verify(ref, fixture.ctx)
        with pytest.raises(ValueError):
            verify_text_artifact(ref, fixture.ctx, bundle=bundle)
    finally:
        fixture.tearDown()
        fixture.doCleanups()


def refusal(*, tool='delivery_publish', code='invalid_input', **updates):
    return {'id': 'refused', 'tool': tool, 'arguments': {'artifact_refs': []},
        'result': {'status': 'error', 'summary': 'Refused', 'error': {'code': code}, **updates}}


@pytest.mark.parametrize('case', ['unverified_final', 'no_fixed_refs', 'missing_page', 'unverified_source',
    'native_data', 'job_ref', 'result_ref', 'artifact_ref', 'persistent', 'unknown', 'business_write', 'failed_check'])
def test_delivery_refusal_exemption_requires_verified_complete_final_with_no_accepted_receipt(case):
    old = refusal()
    coverage = {'missing': 0, 'unverified': []}
    current = {'arguments': {'artifact_refs': [{'kind': 'artifact', 'workspace_id': 'w', 'object_id': 'actual', 'revision': 1}]},
        'outcomes': [], 'references_verified': case != 'no_fixed_refs'}
    if case == 'missing_page':
        coverage['missing'] = 1
    elif case == 'unverified_source':
        coverage['unverified'] = ['native']
    elif case == 'native_data':
        old['result']['data'] = {'accepted_write': True}
    elif case in {'job_ref', 'result_ref', 'artifact_ref'}:
        old['result'][case[:-4] + '_refs'] = [{'object_id': 'accepted'}]
    elif case == 'persistent':
        old['result']['coverage'] = {'pending': 1}
    elif case == 'unknown':
        old['result']['error']['code'] = 'response_unknown'
    elif case == 'business_write':
        old['tool'] = 'data_import'
    elif case == 'failed_check':
        old['result']['error']['code'] = 'delivery_check_failed'
    unchanged, nonblocking = deepcopy(old), []
    failed, repaired = _delivery_failures([old], current, 'final', requirement_coverage=coverage,
        verified_final=case != 'unverified_final', nonblocking_operations=nonblocking)
    assert failed == [old['result']] and not repaired and not nonblocking
    assert old == unchanged


@pytest.mark.parametrize('case', ['same_business', 'default_options', 'other_asset', 'other_session',
    'other_revision', 'other_hash', 'other_sheet', 'other_header', 'other_side', 'unknown'])
def test_pre_submit_data_failure_requires_later_success_on_same_business_request(case):
    arguments = {'data_session_id': 'session', 'asset_id': 'asset', 'asset_revision': 1,
        'blob_hash': 'a' * 64, 'side': 'left', 'expected_version': 1}
    old = refusal(tool='data_import', code='qa_pre_submit_failure', data={'accepted_write': False, 'failure_stage': 'pre_submit'})
    old['arguments'] = arguments
    later = {'id': 'later', 'tool': 'data_import', 'arguments': {**arguments, 'expected_version': 2},
        'result': {'status': 'success', 'summary': 'Original input imported', 'data': {'accepted_write': True}}}
    if case == 'other_asset':
        later['arguments']['asset_id'] = 'other'
    elif case == 'other_session':
        later['arguments']['data_session_id'] = 'other'
    elif case == 'other_revision':
        later['arguments']['asset_revision'] = 2
    elif case == 'other_hash':
        later['arguments']['blob_hash'] = 'b' * 64
    elif case == 'other_sheet':
        later['arguments']['options'] = {'sheet_name': 'Other'}
    elif case == 'other_header':
        later['arguments']['options'] = {'header_row': 2}
    elif case == 'default_options':
        later['arguments']['options'] = {'sheet_name': None, 'header_row': 1, 'encoding': 'utf-8-sig', 'delimiter': ','}
    elif case == 'other_side':
        later['arguments']['side'] = 'right'
    elif case == 'unknown':
        old['result']['error']['code'] = 'response_unknown'
    original = deepcopy(old)
    failed, repaired = _delivery_failures([old, later], {'arguments': {}, 'outcomes': []}, 'final', verified_final=True)
    assert bool(repaired) == (case in {'same_business', 'default_options'})
    assert bool(failed) == (case not in {'same_business', 'default_options'})
    assert old == original


def test_old_successful_partial_receipt_is_history_and_native_unknown_still_blocks():
    old = {'id': 'oldPartial', 'tool': 'delivery_publish', 'arguments': {}, 'result': {'status': 'success',
        'summary': 'Two of three delivered', 'data': {'completeness': 'partial', 'requirement_coverage': {'missing': 1},
            'limitations': ['third pre-submit failure']}}}
    assert not _persistent_incompleteness(old['result'])
    failed, _ = _delivery_failures([old], {'arguments': {}, 'outcomes': []}, 'final')
    assert not failed
    old['result']['data']['native_status'] = 'unknown'
    failed, _ = _delivery_failures([old], {'arguments': {}, 'outcomes': []}, 'final')
    assert failed == [old['result']]
