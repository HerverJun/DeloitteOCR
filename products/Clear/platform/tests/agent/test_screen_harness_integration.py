"""Cross-version screen exports use the current output and Harness services."""
from types import SimpleNamespace

import pytest

from workbench_platform.agent.delivery_jobs import verify_native_jobs, NativeDeliveryBlocked
from workbench_platform.application_services.references import verify
from workbench_platform.artifacts.ocr_results import _readable_export


def fixture(status='completed', pending=0, needs_review=0):
    screen = SimpleNamespace(
        _row=lambda *_: {'handoff_id': 'handoffA'},
        status=lambda *_, **__: {'status': status, 'counts': {
            'total': 2, 'pending': pending, 'needs_review': needs_review,
            'failed': 0, 'cancelled': 0}})
    context = SimpleNamespace(workspace_id='wtest', session_id='sessionA',
        services=SimpleNamespace(screen_batches=screen),
        store=SimpleNamespace(rows=lambda *_: []), current=lambda: None)
    bundle = {'kind': 'ocr_result', 'handoff_id': 'batchA',
        'platform_job_id': 'jProjection', 'screen_delivery': {'current': True}}
    return context, bundle


def test_excel_batch_export_is_not_parsed_as_ordinary_json_text():
    assert _readable_export({'platform_job': {'stage': 'screen_batch'},
        'media_type': 'application/zip', 'export': b'xlsx-archive',
        'items': [{'input_version_id': 'versionA'}]}) is None
    assert _readable_export({'media_type': 'application/json',
        'export': b'{"id":"resultA","revision":1,"edited":{"text":"00123"}}',
        'items': [{'native_result_id': 'resultA', 'revision': 1}]}) == '00123'


def test_shared_output_reads_actual_screen_batch_instead_of_projected_job():
    context, bundle = fixture()
    checks, limitations = verify_native_jobs([], context, verified_objects=[bundle])
    assert checks[0]['evidence']['job_id'] == 'batchA'
    assert checks[0]['status'] == 'passed' and limitations == []


def test_review_completed_batch_remains_a_partial_result():
    context, bundle = fixture('needs_review', needs_review=1)
    checks, limitations = verify_native_jobs([], context, verified_objects=[bundle])
    assert checks[0]['status'] == 'failed' and limitations


def test_running_batch_requires_explicit_partial_export():
    context, bundle = fixture('running', pending=1)
    with pytest.raises(NativeDeliveryBlocked):
        verify_native_jobs([], context, verified_objects=[bundle])
    checks, limitations = verify_native_jobs([], context, verified_objects=[bundle],
        partial_screen_batches={'batchA'})
    assert limitations and checks[0]['evidence']['explicit_partial_export'] is True


def test_shared_reference_verification_rejects_stale_excel_bundle():
    context, _ = fixture()
    context.services.artifact_center = SimpleNamespace(get=lambda *_: {
        'bundle_id': 'bundleA', 'handoff_id': 'batchA',
        'items': [{'input_version_id': 'versionA'}], 'files': []})
    context.services.screen_batches.export_freshness = lambda *_: {'current': False}
    with pytest.raises(ValueError, match='历史成果'):
        verify({'kind': 'artifact', 'workspace_id': 'wtest', 'object_id': 'bundleA',
            'revision': 1, 'application': 'ocr', 'parent_id': 'batchA'}, context)
