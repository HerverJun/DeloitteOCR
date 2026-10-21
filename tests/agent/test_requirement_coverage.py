"""Fixed input/page/engine requirements, independent of immutable attempt status."""
from copy import deepcopy
import json
import hashlib
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_harness_delivery_checks as delivery_fixtures
import pytest

from workbench_platform.agent.requirement_coverage import project_requirements
from workbench_platform.agent.tools.context import _delivery_failures, _refused_pdf_imports


def unit(asset='imageA', *, revision=1, blob_hash='a' * 64, engine='ppocr', state='succeeded', page=None):
    return {'asset_id': asset, 'asset_revision': revision, 'blob_hash': blob_hash,
            'operation': 'extract_text', 'scope': {'kind': 'image'} if page is None else {'kind': 'page', 'page_number': page},
            'engine': engine, 'state': state, 'result_id': 'result-' + asset + '-' + engine if state == 'succeeded' else None,
            'native_task_id': 'task-' + asset + '-' + engine}


def record(identifier, units, *, error=None, engines=None):
    failed = sum(item['state'] == 'failed' for item in units)
    result = {'status': 'error' if failed else 'success', 'summary': identifier,
              'coverage': {'failed': failed, 'succeeded': len(units) - failed},
              'job_refs': [{'application': 'ocr', 'object_id': identifier, 'parent_id': identifier}]}
    if error:
        result['error'] = {'code': error}
    return {'id': identifier, 'tool': 'ocr_run', 'arguments': {'engines': engines or [units[0]['engine']]}, 'result': result}


def project(attempts, delivered, *, engines=()):
    records = [record(identifier, units) for identifier, units in attempts.items()]
    refs = [{'kind': 'result', 'application': 'ocr', 'parent_id': 'delivered', 'object_id': item['result_id']} for item in delivered]
    ctx = SimpleNamespace(run_id='run', session_id='session',
                          store=SimpleNamespace(run=lambda _: {'driver_state': {'request': {'engines': list(engines)}}}))
    with patch('workbench_platform.agent.requirement_coverage.OcrCoverageReader.attempt',
               side_effect=lambda ref: attempts[ref['object_id']]), patch(
            'workbench_platform.agent.requirement_coverage.OcrCoverageReader.result',
            side_effect=lambda _, identifier: next(item for item in delivered if item['result_id'] == identifier)):
        projection = project_requirements(records, refs, ctx)
    return records, projection


def test_fallback_repairs_same_fixed_input_and_keeps_original_failure_receipt():
    failure, success = unit(engine='paddlevl', state='failed'), unit()
    records, projection = project({'old': [failure], 'retry': [success]}, [success])
    original = deepcopy(records)
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'delivery', requirement_coverage=projection)
    assert not failed and repaired[0]['operation_id'] == 'old'
    assert projection['rule_version'] == 'input-requirements-v1'
    assert (projection['total'], projection['satisfied'], projection['missing']) == (1, 1, 0)
    assert projection['repaired_job_ids'] == ['old']
    assert records == original and records[0]['result']['status'] == 'error'


def test_fallback_never_repairs_other_input_revision_hash_or_page():
    success = unit(page=2)
    for failure in (unit('other', state='failed', page=2), unit(revision=2, state='failed', page=2),
                    unit(blob_hash='b' * 64, state='failed', page=2), unit(state='failed', page=5)):
        records, projection = project({'old': [failure], 'retry': [success]}, [success])
        failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'delivery', requirement_coverage=projection)
        assert projection['missing'] == 1 and not repaired
        assert failed[0]['summary'] == 'old'


def test_explicit_engine_and_multi_engine_requirements_are_not_substituted():
    failure, success = unit(engine='paddlevl', state='failed'), unit()
    for required in (['paddlevl'], ['paddlevl', 'ppocr']):
        _, projection = project({'old': [failure], 'retry': [success]}, [success], engines=required)
        assert projection['missing'] == 1 and not projection['repaired_operations']


def test_three_inputs_only_failed_subset_retry_requires_original_successes_in_delivery():
    a, b, c = unit('a'), unit('b'), unit('c', engine='paddlevl', state='failed')
    retry = unit('c')
    _, complete = project({'original': [a, b, c], 'retry': [retry]}, [a, b, retry])
    assert (complete['total'], complete['satisfied'], complete['missing']) == (3, 3, 0)
    _, partial = project({'original': [a, b, c], 'retry': [retry]}, [retry])
    assert partial['missing'] == 2


def test_unknown_receipt_remains_unresolved_even_with_matching_success():
    failure, success = unit(state='failed'), unit()
    records, projection = project({'old': [failure], 'retry': [success]}, [success])
    records[0]['result']['error'] = {'code': 'response_unknown'}
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'delivery', requirement_coverage=projection)
    assert failed[0]['summary'] == 'old' and repaired == []


def test_native_unknown_response_does_not_license_fallback_repair():
    failure = {**unit(state='failed'), 'failure_code': 'engine_response_unknown'}
    records, projection = project({'old': [failure]}, [unit()])
    assert projection['repaired_operations'] == [] and projection['repaired_job_ids'] == []
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'delivery', requirement_coverage=projection)
    assert failed and not repaired


def test_unprocessed_region_warning_is_not_erased_by_whole_input_retry():
    records, projection = project({'old': [unit(state='failed')]}, [unit()])
    records[0]['result']['warnings'] = ['未处理区域需要单独核实']
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'delivery', requirement_coverage=projection)
    assert failed and not repaired


def refusal(identifier, tool='ocr_read_page', code='invalid_input', **updates):
    result = {'status': 'error', 'summary': identifier, 'error': {'code': code}, **updates}
    return {'id': identifier, 'tool': tool, 'arguments': {'result_id': identifier}, 'result': result}


def test_verified_final_delivery_classifies_read_refusals_without_claiming_repair():
    records = [refusal('bad-input'), refusal('stale-version', code='revision_conflict')]
    original, nonblocking = deepcopy(records), []
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'final',
        tool_effects={'ocr_read_page': 'read'}, verified_final=True, nonblocking_operations=nonblocking)
    assert failed == repaired == []
    assert {item['operation_id'] for item in nonblocking} == {'bad-input', 'stale-version'}
    assert all(item['reason'] == 'read_request_refused' and item['verified_by_delivery'] == 'final' for item in nonblocking)
    assert records == original and all(item['result']['status'] == 'error' for item in records)


def test_read_refusal_classification_requires_final_verification_and_registered_read_effect():
    records = [refusal('read')]
    for effects, verified in (({'ocr_read_page': 'read'}, False), ({}, True),
                              ({'ocr_read_page': 'derive'}, True)):
        nonblocking = []
        failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'final',
            tool_effects=effects, verified_final=verified, nonblocking_operations=nonblocking)
        assert failed == [records[0]['result']] and repaired == nonblocking == []


def test_verified_delivery_does_not_discard_permission_unknown_or_observation_failures():
    for code in ('permission_denied', 'scope_denied', 'response_unknown', 'engine_response_unknown',
                 'observation_timeout', 'service_unavailable'):
        records, nonblocking = [refusal(code, code=code)], []
        failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'final',
            tool_effects={'ocr_read_page': 'read'}, verified_final=True, nonblocking_operations=nonblocking)
        assert failed == [records[0]['result']] and repaired == nonblocking == []


def test_read_refusal_with_persistent_native_incompleteness_still_blocks_delivery():
    for updates in ({'coverage': {'failed': 1}}, {'coverage': {'pending': 1}},
                    {'data': {'native_status': 'unknown'}}, {'data': {'job': {'counts': {'cancelled': 1}}}},
                    {'warnings': ['未处理区域需要单独核实']}, {'warnings': ['图片提取失败']}):
        records, nonblocking = [refusal('persistent', **updates)], []
        failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'final',
            tool_effects={'ocr_read_page': 'read'}, verified_final=True, nonblocking_operations=nonblocking)
        assert failed == [records[0]['result']] and repaired == nonblocking == []


def test_conversation_delivery_guard_is_satisfied_only_by_verified_final_delivery():
    records = [refusal('guard', 'conversation_answer', 'delivery_required')]
    for verified in (False, True):
        nonblocking = []
        failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'final',
            tool_effects={'conversation_answer': 'read'}, verified_final=verified, nonblocking_operations=nonblocking)
        assert repaired == []
        if verified:
            assert failed == [] and nonblocking[0]['reason'] == 'delivery_precondition_satisfied'
        else:
            assert failed == [records[0]['result']] and nonblocking == []


def test_final_delivery_keeps_write_failures_and_missing_native_requirements():
    records = [refusal('bad-read'), refusal('bad-write', 'data_apply_step')]
    nonblocking = []
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'final',
        tool_effects={'ocr_read_page': 'read', 'data_apply_step': 'derive'}, verified_final=True,
        requirement_coverage={'missing': 1, 'unverified': ['native'], 'repaired_operations': []},
        nonblocking_operations=nonblocking)
    assert failed == [records[1]['result']] and repaired == []
    assert [item['operation_id'] for item in nonblocking] == ['bad-read']


def pdf_import_refusals(tmp_path):
    database = tmp_path / 'ocr-handoffs.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE ocr_handoff(workspace_id TEXT,asset_set_id TEXT,link_id TEXT)')
    old = refusal('oRejectedImport', 'ocr_inspect_document')
    old['arguments'] = {'link_id': 'not-a-workspace-link', 'asset_set_id': 'fixed-input', 'asset_set_revision': 1}
    later = {'id': 'oSuccessfulInspection', 'tool': 'ocr_inspect_document',
        'arguments': {**old['arguments'], 'link_id': 'valid-link'},
        'result': {'status': 'success', 'summary': 'Actual fixed PDF pages', 'data': {'handoff_id': 'actual-handoff'}}}
    services = SimpleNamespace(workspace_store=SimpleNamespace(list_links=lambda _: [
        SimpleNamespace(link_id='valid-link', app_id='ocr')]), ocr_handoff=SimpleNamespace(db_path=database))
    ctx = SimpleNamespace(workspace_id='workspace', services=services, current=lambda: None)
    return [old, later], ctx, database


def test_pdf_pre_import_refusal_needs_same_fixed_inputs_success_and_readonly_absence_proof(tmp_path):
    records, ctx, database = pdf_import_refusals(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    original, nonblocking = deepcopy(records), []
    proof = _refused_pdf_imports(records, ctx)
    assert proof['oRejectedImport']['attempt_set_id'] == 'sRejectedImport'
    assert proof['oRejectedImport']['satisfied_by_operation'] == 'oSuccessfulInspection'
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'verified-final',
        tool_effects={'ocr_inspect_document': 'derive'}, verified_final=True,
        refused_pdf_imports=proof, nonblocking_operations=nonblocking)
    assert failed == repaired == [] and nonblocking[0]['reason'] == 'pre_import_rejected'
    assert records == original and hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('case', ['accepted_on_other_link', 'different_input', 'different_revision',
    'different_revision_same_real_target',
    'real_old_target', 'unknown_receipt', 'permission', 'persistent', 'result_receipt', 'data_receipt',
    'unverifiable_database', 'unverifiable_links', 'no_later_success', 'unverified_final'])
def test_pdf_import_refusal_never_exempts_real_target_intent_unknown_or_different_inputs(tmp_path, case):
    records, ctx, database = pdf_import_refusals(tmp_path)
    old, later = records
    verified = case != 'unverified_final'
    if case == 'accepted_on_other_link':
        with sqlite3.connect(database) as db:
            db.execute('INSERT INTO ocr_handoff VALUES(?,?,?)', ('workspace', 'sRejectedImport', 'different-link'))
    elif case == 'different_input':
        later['arguments']['asset_set_id'] = 'other-input'
    elif case == 'different_revision':
        later['arguments']['asset_set_revision'] = 2
    elif case == 'different_revision_same_real_target':
        old['arguments']['link_id'] = 'valid-link'
        later['arguments']['asset_set_revision'] = 2
    elif case == 'real_old_target':
        ctx.services.workspace_store.list_links = lambda _: [SimpleNamespace(link_id=key, app_id='ocr')
            for key in ('not-a-workspace-link', 'valid-link')]
    elif case in {'unknown_receipt', 'permission'}:
        old['result']['error']['code'] = 'response_unknown' if case == 'unknown_receipt' else 'permission_denied'
    elif case == 'persistent':
        old['result']['coverage'] = {'pending': 1}
    elif case == 'result_receipt':
        old['result']['result_refs'] = [{'object_id': 'accepted-object'}]
    elif case == 'data_receipt':
        old['result']['data'] = {'handoff_id': 'accepted-handoff'}
    elif case == 'unverifiable_database':
        ctx.services.ocr_handoff.db_path = tmp_path / 'missing.sqlite3'
    elif case == 'unverifiable_links':
        def unavailable(_):
            raise OSError('authoritative link store unavailable')
        ctx.services.workspace_store.list_links = unavailable
    elif case == 'no_later_success':
        later['result']['status'] = 'error'
    proof = _refused_pdf_imports(records, ctx)
    nonblocking = []
    failed, repaired = _delivery_failures(records, {'arguments': {}, 'outcomes': []}, 'final',
        tool_effects={'ocr_inspect_document': 'derive'}, verified_final=verified,
        refused_pdf_imports=proof, nonblocking_operations=nonblocking)
    assert old['result'] in failed and nonblocking == []
    assert not ctx.services.ocr_handoff.db_path.name == 'missing.sqlite3' or not ctx.services.ocr_handoff.db_path.exists()


class VerifiedDeliveryProjectionTests(unittest.IsolatedAsyncioTestCase):
    tearDown = delivery_fixtures.DeliveryChecksTests.tearDown
    publish = delivery_fixtures.DeliveryChecksTests.publish
    deliver = delivery_fixtures.DeliveryChecksTests.deliver

    def setUp(self):
        delivery_fixtures.DeliveryChecksTests.setUp(self)
        from workbench_platform.agent.tools import audit
        audit.register(self.registry)
        self.services.agent = SimpleNamespace(registry=self.registry)

    def historical_refusal(self, tool, code):
        self.sequence += 1
        value = {'status': 'error', 'summary': 'Historical refused request', 'error': {'code': code}}
        record, _ = self.store.begin_operation(self.ctx.run_id, self.ctx.generation,
            {'call_id': 'refused-' + str(self.sequence), 'tool': tool, 'arguments': {'different_parameters': True}})
        self.store.finish_operation(record['id'], value)
        return record['id'], value

    async def test_real_artifact_delivery_preserves_refusal_history_and_versions_completion_rule(self):
        read_id, read_value = self.historical_refusal('assets_describe', 'invalid_input')
        guard_id, guard_value = self.historical_refusal('conversation_answer', 'delivery_required')
        ref, _ = self.publish(b'id\n000001\n')
        value = await self.deliver([ref])
        self.assertEqual(value['status'], 'success', value)
        self.assertEqual(value['data']['completeness'], 'complete')
        self.assertEqual(value['data']['completion_rule_version'], 'delivery-obligations-v3')
        self.assertEqual({item['operation_id'] for item in value['data']['nonblocking_operations']}, {read_id, guard_id})
        self.assertFalse(value['data']['repaired_operations'])
        for identifier, expected in ((read_id, read_value), (guard_id, guard_value)):
            self.assertEqual(json.loads(self.store.rows('SELECT result FROM operations WHERE id=?', (identifier,))[0]['result']), expected)

    async def test_missing_requirement_and_unknown_requirement_still_make_verified_delivery_partial(self):
        self.historical_refusal('assets_describe', 'invalid_input')
        ref, _ = self.publish(b'id\n000001\n')
        for missing, unverified in ((1, []), (0, ['missing-native-receipt'])):
            projection = {'rule_version': 'input-requirements-v1', 'total': 1, 'satisfied': 0,
                'missing': missing, 'unverified': unverified, 'repaired_operations': [], 'repaired_job_ids': []}
            with patch('workbench_platform.agent.requirement_coverage.project_requirements', return_value=projection):
                value = await self.deliver([ref])
            self.assertEqual(value['status'], 'success', value)
            self.assertEqual(value['data']['completeness'], 'partial')
            self.assertEqual(value['data']['requirement_coverage'], projection)

    async def test_unfinished_native_job_blocks_before_read_refusal_exemption(self):
        self.historical_refusal('assets_describe', 'invalid_input')
        ref, _ = self.publish(b'id\n000001\n')
        next(iter(self.services.data_workbench.jobs.values()))['status'] = 'running'
        value = await self.deliver([ref])
        self.assertEqual(value['status'], 'error', value)
        self.assertEqual(value['error']['code'], 'native_delivery_unverified')
        self.assertFalse(value['data']['verified'])
        self.assertNotIn('nonblocking_operations', value['data'])
