"""Cold reader launch boundaries: exact original scope and no business replay."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pytest

from workbench_platform.application_services.errors import NativeReaderUnavailable
from workbench_platform.application_services.native import ensure_read_service, scoped_handoff
from workbench_platform.application_services.identity import RunSuperseded
from workbench_platform.agent.tools.context import _delivery_failures
from workbench_platform.catalog.launch import LaunchStatus
import test_harness_delivery_checks as fixtures


def context():
    live = SimpleNamespace(instance_id='new-native-service')
    launch = Mock()
    launch.current.side_effect = [LaunchStatus(404, 'NOT_FOUND'), live]
    workspace = Mock()
    workspace.list_links.return_value = [SimpleNamespace(app_id='ocr', link_id='original-link',
        module_project_id='original-project', role='owner')]
    services = SimpleNamespace(launch_service=launch, workspace_store=workspace,
        supervisor=SimpleNamespace(registry=SimpleNamespace(installations={'ocr': SimpleNamespace(installed=True)})))
    return SimpleNamespace(workspace_id='workspace', current=Mock(), services=services,
        grants=frozenset({'read', 'derive'})), launch


def test_cold_read_starts_only_existing_service_without_creating_or_submitting_business_work():
    ctx, launches = context()
    with patch('workbench_platform.application_services.native.OcrProjects') as projects:
        ensure_read_service(ctx, 'ocr', 'original-link')
    launches.start.assert_called_once()
    assert launches.start.call_args.args[0] == 'ocr'
    assert launches.start.call_args.args[1].startswith('l')
    assert launches.current.call_count == 2
    projects.assert_not_called()


def test_live_reader_does_not_start_or_require_write_authority():
    ctx, launches = context()
    launches.current.side_effect = None
    ctx.grants = frozenset({'read'})
    ensure_read_service(ctx, 'ocr', 'original-link')
    launches.start.assert_not_called()


def test_live_viewer_preserves_legal_pure_read_without_launching_service():
    ctx, launches = context()
    launches.current.side_effect = None
    ctx.services.workspace_store.list_links.return_value[0].role = 'viewer'
    ctx.grants = frozenset({'read'})
    ensure_read_service(ctx, 'ocr', 'original-link')
    launches.current.assert_called_once_with('ocr')
    launches.start.assert_not_called()


@pytest.mark.parametrize('case', ['other_workspace_link', 'other_app_link', 'missing_project', 'viewer',
    'not_installed', 'readonly', 'generation', 'unknown_process', 'stale_process', 'id_conflict', 'network_unknown'])
def test_uncertain_identity_permission_scope_or_readonly_snapshot_never_starts(case):
    ctx, launches = context()
    expected = (ValueError, PermissionError, OSError)
    if case == 'other_workspace_link':
        ctx.services.workspace_store.list_links.return_value[0].link_id = 'other-workspace-link'
    elif case == 'other_app_link':
        ctx.services.workspace_store.list_links.return_value[0].app_id = 'classification'
    elif case == 'missing_project':
        ctx.services.workspace_store.list_links.return_value[0].module_project_id = ''
    elif case == 'viewer':
        ctx.services.workspace_store.list_links.return_value[0].role = 'viewer'
    elif case == 'not_installed':
        ctx.services.supervisor.registry.installations['ocr'].installed = False
    elif case == 'readonly':
        ctx.grants = frozenset({'read'})
    elif case == 'generation':
        ctx.current.side_effect = RunSuperseded('Stopped')
        expected = (RunSuperseded,)
    elif case in {'unknown_process', 'stale_process', 'id_conflict'}:
        launches.current.side_effect = LaunchStatus({'unknown_process': 503, 'stale_process': 410, 'id_conflict': 409}[case], 'APP_UNAVAILABLE')
    elif case == 'network_unknown':
        launches.current.side_effect = OSError('Uncertain transport')
    with pytest.raises(expected):
        ensure_read_service(ctx, 'ocr', 'original-link')
    launches.start.assert_not_called()


def test_read_scope_and_fixed_input_version_are_checked_before_preparation():
    ctx, launches = context()
    service = Mock()
    service._row.return_value = {'asset_set_id': 'fixed-inputs', 'asset_set_revision': 3, 'link_id': 'original-link'}
    ctx.services.ocr_handoff = service
    with patch('workbench_platform.application_services.native.scoped_set', side_effect=ValueError('Fixed source mismatch')):
        with pytest.raises(ValueError, match='Fixed source mismatch'):
            scoped_handoff(ctx, service, 'original-handoff')
    launches.start.assert_not_called()


@pytest.mark.parametrize('case', ['current_verified', 'current_unknown', 'other_conflict'])
def test_concurrent_reader_launch_reuses_only_verified_current_receipt(case):
    ctx, launches = context()
    launches.start.side_effect = LaunchStatus(409, 'IDEMPOTENCY_CONFLICT' if case == 'other_conflict' else 'RESOURCE_BUSY')
    if case == 'current_unknown':
        launches.current.side_effect = [LaunchStatus(404, 'NOT_FOUND'), LaunchStatus(503, 'APP_UNAVAILABLE')]
    if case == 'current_verified':
        ensure_read_service(ctx, 'ocr', 'original-link')
    else:
        with pytest.raises(NativeReaderUnavailable):
            ensure_read_service(ctx, 'ocr', 'original-link')
    launches.start.assert_called_once()


def test_generation_loss_during_launch_prevents_the_native_read():
    ctx, launches = context()
    def launch(*_):
        ctx.current.side_effect = RunSuperseded('Stopped while starting service')
    launches.start.side_effect = launch
    with pytest.raises(RunSuperseded):
        ensure_read_service(ctx, 'ocr', 'original-link')
    assert launches.current.call_count == 1


def test_service_restore_is_not_a_generic_prepare_for_other_apps():
    ctx, launches = context()
    ensure_read_service(ctx, 'classification', 'other-link')
    ensure_read_service(ctx, 'data', 'other-link')
    launches.start.assert_not_called()
    launches.current.assert_not_called()


@pytest.mark.parametrize('case', ['restored', 'unknown_receipt', 'accepted_write', 'missing_requirement',
    'unverified_requirement', 'no_verified_refs', 'different_failure_stage', 'extra_data', 'result_ref'])
def test_old_reader_prepare_refusal_requires_actual_complete_verified_final_to_be_nonblocking(case):
    old = {'id': 'prepare-failed', 'tool': 'delivery_publish', 'arguments': {}, 'result': {'status': 'error',
        'summary': 'Original service unavailable', 'error': {'code': 'native_reader_unavailable'},
        'data': {'failure': {'stage': 'native_reader_prepare', 'application': 'ocr', 'accepted_write': False}}}}
    current = {'arguments': {'result_refs': [{'kind': 'result', 'workspace_id': 'w', 'object_id': 'fixed', 'revision': 1}]},
        'outcomes': [], 'references_verified': True}
    coverage = {'missing': 0, 'unverified': []}
    if case == 'unknown_receipt':
        old['result']['error']['code'] = 'response_unknown'
    elif case == 'accepted_write':
        old['result']['data']['failure']['accepted_write'] = True
    elif case == 'missing_requirement':
        coverage['missing'] = 1
    elif case == 'unverified_requirement':
        coverage['unverified'] = ['native']
    elif case == 'no_verified_refs':
        current['references_verified'] = False
    elif case == 'different_failure_stage':
        old['result']['data']['failure']['stage'] = 'submission'
    elif case == 'extra_data':
        old['result']['data']['job'] = {'id': 'native'}
    elif case == 'result_ref':
        old['result']['result_refs'] = [{'object_id': 'accepted'}]
    original, nonblocking = deepcopy(old), []
    failed, repaired = _delivery_failures([old], current, 'actual-final', requirement_coverage=coverage,
        verified_final=True, nonblocking_operations=nonblocking)
    assert bool(failed) == (case != 'restored') and not repaired
    assert bool(nonblocking) == (case == 'restored')
    if nonblocking:
        assert nonblocking[0]['reason'] == 'native_reader_service_restored'
    assert old == original


class NativeReaderFaultProjectionTests(unittest.IsolatedAsyncioTestCase):
    tearDown = fixtures.DeliveryChecksTests.tearDown

    def setUp(self):
        root = patch.object(fixtures, 'ROOT', Path('F:/OCR/output/user-flow-fixes-20261018/evidence/native-reader-tests017'))
        root.start()
        self.addCleanup(root.stop)
        fixtures.DeliveryChecksTests.setUp(self)

    async def test_fixed_text_read_preserves_service_failure_stage_at_shared_tool_boundary(self):
        from workbench_platform.agent.tools import results
        results.register(self.registry)
        reference = {'kind': 'result', 'workspace_id': 'wtest', 'application': 'ocr',
            'object_id': 'original-result', 'parent_id': 'original-handoff', 'revision': 0, 'sha256': 'a' * 64}
        with patch('workbench_platform.application_services.result_text.verify',
                side_effect=NativeReaderUnavailable('ocr', cause_code='APP_UNAVAILABLE')):
            outcome = await self.registry.dispatch({'call_id': 'cold-native-text', 'tool': 'results_read_text',
                'arguments': {'reference': reference}}, self.ctx)
        self.assertEqual(outcome['error']['code'], 'native_reader_unavailable', outcome)
        self.assertEqual(outcome['data']['failure']['stage'], 'native_reader_prepare')
        self.assertFalse(outcome['data']['failure']['accepted_write'])
        self.assertEqual(outcome['result_refs'], [])
