import pytest
from pydantic import ValidationError
from packages.contracts.automation import (
    BatchCoverage, BatchDisposition, BatchHistoryEvent, BatchNativeRevisionPage, ReviewBundle,
)
from packages.contracts.semantics import ContractError, check_review_completion

ACTOR = {'id': 'localUser', 'display_name': '本机编制人', 'source': 'local'}
REQUEST = {'request_id': 'requestA', 'expected_revision': 1, 'actor': ACTOR,
           'item_ids': ['itemA'], 'action': 'confirm'}


def test_old_unidentified_disposition_cannot_silently_write():
    with pytest.raises(ValidationError):
        BatchDisposition.model_validate({'item_ids': ['itemA'], 'action': 'confirm'})
    assert BatchDisposition.model_validate(REQUEST).automation_version == '2.0'
    with pytest.raises(ValidationError):
        BatchDisposition.model_validate(REQUEST | {'automation_version': '1.0'})


def test_adoption_requires_explicit_single_item_and_native_revision():
    with pytest.raises(ValidationError):
        BatchDisposition.model_validate(REQUEST | {'action': 'adopt'})
    request = REQUEST | {'action': 'adopt', 'adoption': {'native_revision': 2,
                                                       'native_result_id': '01result'}}
    assert BatchDisposition.model_validate(request).adoption.native_revision == 2
    with pytest.raises(ValidationError):
        BatchDisposition.model_validate(request | {'item_ids': ['itemA', 'itemB']})


def test_coverage_rejects_truncated_index_and_wrong_disposition_sum():
    with pytest.raises(ValidationError):
        BatchCoverage.model_validate({'result_rows': 100002, 'required_items': 100002,
            'scanned_results': 100000, 'indexed_items': 100000, 'pending': 100000,
            'index_complete': True})
    with pytest.raises(ValidationError):
        BatchCoverage.model_validate({'result_rows': 1, 'required_items': 1,
            'scanned_results': 1, 'indexed_items': 1, 'confirmed': 2})


@pytest.mark.parametrize('change,pending_operations', [
    ({'index_complete': False}, 0), ({'confirmed': 0, 'pending': 1}, 0),
    ({'confirmed': 0, 'deferred': 1}, 0), ({}, 1)])
def test_final_review_rejects_incomplete_index_deferred_and_unknown_operations(change, pending_operations):
    coverage = {'result_rows': 1, 'required_items': 1, 'scanned_results': 1,
                'indexed_items': 1, 'confirmed': 1, 'index_complete': True}
    with pytest.raises(ContractError) as error:
        check_review_completion(coverage | change, execution_state='succeeded',
                                pending_operations=pending_operations)
    assert error.value.code == 'REVIEW_INCOMPLETE'
    check_review_completion(coverage, execution_state='succeeded')


def test_history_keeps_unidentified_legacy_separate_from_identified_events():
    legacy = {'event_id': 'legacyA', 'batch_id': 'batchA', 'item_id': 'itemA',
        'operation_id': None, 'revision': None, 'action': 'confirm', 'note': '',
        'actor': None, 'previous_state': {}, 'current_state': {}, 'adoption': None,
        'legacy': True, 'created_at': '2026-10-02T00:00:00Z'}
    assert BatchHistoryEvent.model_validate(legacy).actor is None
    with pytest.raises(ValidationError):
        BatchHistoryEvent.model_validate(legacy | {'legacy': False})


def test_platform_confirmation_cannot_claim_ocr_native_review_qualification():
    revision = {'schema_version': '1.0', 'batch_id': 'batchA', 'revision': 1,
        'item_id': 'itemA', 'scenario': 'ocr', 'original': {}, 'source': {},
        'adoption': None, 'instance_id': 'instanceA', 'project_id': 'projectA',
        'offset': 0, 'limit': 30, 'current_revision': 2, 'adoptable': True,
        'native_review_qualification': 'independent', 'current': {}, 'history': {}}
    assert BatchNativeRevisionPage.model_validate(revision).current_revision == 2
    with pytest.raises(ValidationError):
        BatchNativeRevisionPage.model_validate(revision | {'native_review_qualification': 'native_disposition'})


def review_metadata():
    return {'bundle_id': 'bundleA', 'workspace_id': 'workspaceA', 'kind': 'batch_review',
        'batch_id': 'batchA', 'batch_revision': 2, 'delivery_kind': 'final',
        'predecessor_bundle_id': 'originalA', 'successor_bundle_ids': [], 'successor_count': 0,
        'status': 'current', 'request_id': 'reviewA', 'payload_hash': 'a' * 64,
        'snapshot_hash': 'b' * 64, 'title': '复核成果', 'created_at': '2026-10-03T00:00:00Z',
        'files': [], 'inputs': [], 'coverage': {'result_rows': 1, 'required_items': 1,
            'scanned_results': 1, 'indexed_items': 1, 'confirmed': 1, 'index_complete': True},
        'unresolved_count': 0, 'unresolved_preview': [], 'execution_status': 'succeeded',
        'pending_operations': 0}


@pytest.mark.parametrize('change', [
    {'execution_status': 'needs_verification'}, {'pending_operations': 1},
    {'coverage': {'result_rows': 1, 'required_items': 1, 'scanned_results': 1,
        'indexed_items': 1, 'pending': 1, 'index_complete': True}, 'unresolved_count': 1},
    {'coverage': {'result_rows': 1, 'required_items': 1, 'index_complete': False}},
])
def test_review_metadata_cannot_mislabel_unresolved_or_incomplete_snapshot_final(change):
    metadata = review_metadata()
    assert ReviewBundle.model_validate(metadata).delivery_kind == 'final'
    with pytest.raises(ValidationError):
        ReviewBundle.model_validate(metadata | change)
    assert ReviewBundle.model_validate(metadata | change | {'delivery_kind': 'intermediate'})


def test_review_metadata_preview_is_bounded_and_preserves_full_counts():
    preview = {'item_id': 'itemA', 'category': 'difference', 'disposition': 'pending', 'title': '差异'}
    metadata = review_metadata() | {'delivery_kind': 'intermediate', 'unresolved_count': 100002,
        'coverage': {'result_rows': 100002, 'required_items': 100002, 'scanned_results': 100002,
            'indexed_items': 100002, 'pending': 100002, 'index_complete': True},
        'unresolved_preview': [preview] * 30, 'successor_count': 101,
        'successor_bundle_ids': [f'bundle{i}' for i in range(100)]}
    assert ReviewBundle.model_validate(metadata).unresolved_count == 100002
    technical = review_metadata() | {'delivery_kind': 'intermediate', 'unresolved_count': 1,
        'unresolved_preview': [preview | {'category': 'collection_failed'}]}
    assert ReviewBundle.model_validate(technical).unresolved_count == 1
    for change in ({'unresolved_preview': [preview] * 31},
                   {'successor_bundle_ids': [f'bundle{i}' for i in range(101)]},
                   {'successor_count': 99}, {'unresolved_count': 100001}):
        with pytest.raises(ValidationError):
            ReviewBundle.model_validate(metadata | change)
