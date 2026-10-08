"""Bridge and model-boundary contracts; deterministic native receipts only."""
from contextlib import closing
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from workbench_platform.assets.sets import AssetSetConflict
from workbench_platform.jobs.native_ocr import OcrUnavailable
from workbench_platform.jobs.ocr_handoff import _json
from workbench_platform.jobs.screen_batches import ScreenAction, ScreenBatches, ScreenResolution
from workbench_platform.agent.tools.screen_batches import diagnostic_page
from workbench_platform.agent.tools import screen_batches as screen_tools


@pytest.fixture
def issue_bridge(tmp_path, monkeypatch):
    sources = [{'input_version_id': 'versionA', 'image_id': 'imageA', 'asset_id': 'assetA'}]
    member = SimpleNamespace(asset_id='assetA', item_id='itemA')
    fixed = SimpleNamespace(asset_set_id='setA', revision=1, items=[member])
    sets = {'setA': fixed}

    def create(workspace, members, asset_set_id, parents):
        assert parents == {'itemA': ('setA', 1, 'itemA')}
        sets.setdefault(asset_set_id, SimpleNamespace(asset_set_id=asset_set_id, revision=1, items=members))
        return sets[asset_set_id]

    original = {'link_id': 'linkA', 'asset_set_id': 'setA', 'asset_set_revision': 1}
    handoff = SimpleNamespace(_row=lambda *args: original,
        sets=SimpleNamespace(create=create, get=lambda workspace, identifier, revision: sets[identifier]))
    service = ScreenBatches(tmp_path/'screen.sqlite3', handoff, None)
    with closing(service._connect()) as db:
        db.execute('INSERT INTO screen_batch_refs VALUES(?,?,?,?,?,?,?,?,?)',
            ('wtest', 'original1', 'a'*64, 'handoffA', 'projectA', 'batchA', _json(sources), '{}', 'now'))

    class Native:
        def __init__(self):
            self.posts = 0
            self.lookups = 0
            self.saved = None
            self.fail_once = False
            self.foreign_child = False
            self.corrupt_revision = False
            self.blocked = False

        def _request(self, method, path, body=None, **kwargs):
            if path.endswith('/resolve-lookup'):
                self.lookups += 1
                return deepcopy(self.saved)
            if path.endswith('/resolve'):
                self.posts += 1
                if self.saved is None:
                    policy = {'primary_engine': 'glm', 'fallback_engines': [], 'max_upgrades': 0,
                              'require_human_review': True}
                    self.saved = {'request_id': body['request_id'], 'action': 'precision_subbatch',
                        'parent_batch_id': 'batchA', 'root_batch_id': 'batchA',
                        'status': 'blocked' if self.blocked else 'submitted',
                        'batch_id': None if self.blocked else 'childA', 'bindings': deepcopy(body['targets']),
                        'policy': policy, 'requested_budget_seconds': body['budget_seconds'],
                        'values_modified': False, 'selection_applied': False, 'human_review': 'not_performed',
                        'reason': 'lineage_budget_exhausted' if self.blocked else 'precision_candidate_requires_human_review'}
                if self.corrupt_revision:
                    self.saved['bindings'][0]['result_revision'] += 1
                if self.fail_once:
                    self.fail_once = False
                    raise OcrUnavailable()
                return deepcopy(self.saved)
            assert method == 'GET' and '/screen-batches/childA?' in path
            return {'batch_id': 'childA', 'project_id': 'projectA',
                'parent_batch_id': 'otherParent' if self.foreign_child else 'batchA',
                'policy': self.saved['policy'], 'status': 'running',
                'counts': {'total': 1, 'completed': 0, 'needs_review': 0, 'failed': 0, 'cancelled': 0, 'pending': 1},
                'items': [{'input_version_id': 'versionA', 'image_id': 'imageA', 'status': 'queued',
                           'stage': 'context', 'result_id': None, 'result_revision': None, 'issues': []}]}

    native = Native()
    monkeypatch.setattr(service, '_adapter', lambda workspace, row:
        (native, original, service.fixed(workspace, row)))
    args = ScreenResolution(request_id='resolve01', budget_seconds=30, targets=[
        {'input_version_id': 'versionA', 'result_id': 'resultA', 'result_revision': 0, 'issue_ids': ['issue-1']}])
    return service, native, args


def test_lost_native_response_recovers_exact_child_without_resubmission(issue_bridge):
    service, native, args = issue_bridge
    native.fail_once = True
    with pytest.raises(OcrUnavailable):
        service.resolve('wtest', 'batchA', args)
    recovered = service.resolve('wtest', 'batchA', args, allow_new=False)
    assert native.posts == 1 and native.lookups == 1
    assert recovered['job_registered'] is True
    assert recovered['attempt']['counts']['total'] == 1
    assert recovered['attempt']['asset_set_id'] != 'setA'
    assert service.fixed('wtest', service._row('wtest', 'childA')).items[0].asset_id == 'assetA'
    assert service.resolve('wtest', 'batchA', args, allow_new=False)['batch_id'] == 'childA'
    assert native.posts == 1


def test_unknown_request_lookup_is_read_only_and_changed_parameters_conflict(issue_bridge):
    service, native, args = issue_bridge
    assert service.resolve('wtest', 'batchA', args, allow_new=False) is None
    assert native.posts == 0 and native.lookups == 0
    service.resolve('wtest', 'batchA', args)
    changed = args.model_copy(update={'budget_seconds': 40})
    with pytest.raises(AssetSetConflict):
        service.resolve('wtest', 'batchA', changed, allow_new=False)
    assert native.posts == 1


def test_foreign_parent_receipt_does_not_grant_a_new_scoped_job(issue_bridge):
    service, native, args = issue_bridge
    native.foreign_child = True
    receipt = service.resolve('wtest', 'batchA', args)
    assert receipt['job_registered'] is False and receipt['attempt'] is None
    with pytest.raises(KeyError):
        service._row('wtest', 'childA')


def test_revision_mismatch_is_rejected_and_budget_block_does_not_register_child(issue_bridge):
    service, native, args = issue_bridge
    native.corrupt_revision = True
    with pytest.raises(OcrUnavailable):
        service.resolve('wtest', 'batchA', args)
    with pytest.raises(KeyError):
        service._row('wtest', 'childA')
    native.saved = None
    native.corrupt_revision = False
    native.blocked = True
    blocked = service.resolve('wtest', 'batchA', args)
    assert blocked['reason'] == 'lineage_budget_exhausted'
    assert blocked['job_registered'] is False and blocked['attempt'] is None


@pytest.mark.parametrize(('field', 'changed'), [
    ('input_version_id', 'foreign-version'),
    ('result_id', 'foreign-result'),
    ('result_revision', 1),
    ('issue_ids', ['foreign-issue']),
])
def test_all_resolution_binding_fields_are_checked_before_child_registration(issue_bridge, monkeypatch, field, changed):
    service, native, args = issue_bridge
    original_request = native._request

    def wrong_binding(method, path, body=None, **kwargs):
        value = original_request(method, path, body, **kwargs)
        if path.endswith('/resolve'):
            value['bindings'][0][field] = changed
        return value

    monkeypatch.setattr(native, '_request', wrong_binding)
    with pytest.raises(OcrUnavailable):
        service.resolve('wtest', 'batchA', args)
    with pytest.raises(KeyError):
        service._row('wtest', 'childA')


@pytest.mark.parametrize(('field', 'changed'), [
    ('request_id', 'foreign-request'),
    ('parent_batch_id', 'foreign-parent'),
    ('action', 'select_candidate'),
    ('values_modified', True),
    ('selection_applied', True),
    ('human_review', 'confirmed'),
])
def test_resolution_cannot_claim_rebinding_selection_or_human_signoff(issue_bridge, monkeypatch, field, changed):
    service, native, args = issue_bridge
    original_request = native._request

    def wrong_receipt(method, path, body=None, **kwargs):
        value = original_request(method, path, body, **kwargs)
        if path.endswith('/resolve'):
            value[field] = changed
        return value

    monkeypatch.setattr(native, '_request', wrong_receipt)
    with pytest.raises(OcrUnavailable):
        service.resolve('wtest', 'batchA', args)
    with pytest.raises(KeyError):
        service._row('wtest', 'childA')


def test_late_precision_receipt_stays_separate_from_current_original_selection(issue_bridge, monkeypatch):
    service, native, args = issue_bridge
    service.resolve('wtest', 'batchA', args)
    source_before = dict(service._row('wtest', 'batchA'))
    current = {'input_version_id': 'versionA', 'image_id': 'imageA', 'status': 'needs_review',
               'stage': 'complete', 'result_id': 'manual-current', 'result_revision': 1, 'issues': []}
    original_request = native._request

    def late_native(method, path, body=None, **kwargs):
        if method == 'GET' and '/screen-batches/batchA?' in path:
            return {'batch_id': 'batchA', 'project_id': 'projectA', 'status': 'needs_review',
                'policy': {'primary_engine': 'glm'},
                'counts': {'total': 1, 'completed': 0, 'needs_review': 1, 'failed': 0, 'cancelled': 0, 'pending': 0},
                'items': [deepcopy(current)]}
        value = original_request(method, path, body, **kwargs)
        if method == 'GET' and '/screen-batches/childA?' in path:
            value.update(status='needs_review')
            value['counts'].update(needs_review=1, pending=0)
            value['items'][0].update(status='needs_review', stage='complete', result_id='late-precision', result_revision=0)
        return value

    monkeypatch.setattr(native, '_request', late_native)
    recovered = service.resolve('wtest', 'batchA', args, allow_new=False)
    assert recovered['bindings'][0]['result_revision'] == 0  # Frozen history is retained.
    assert recovered['attempt']['items'][0]['result_id'] == 'late-precision'
    original = service.status('wtest', 'batchA')['items'][0]
    assert original['result_id'] == current['result_id'] == 'manual-current'
    assert original['result_revision'] == current['result_revision'] == 1
    assert recovered['values_modified'] is recovered['selection_applied'] is False
    assert recovered['human_review'] == 'not_performed'
    assert dict(service._row('wtest', 'batchA')) == source_before
    assert native.posts == 1


def test_action_lookup_recovers_same_operation_without_another_control_commit(issue_bridge, monkeypatch):
    service, native, _ = issue_bridge
    committed, looked_up = [], []
    state = {'status': 'running'}
    original_request = native._request

    def control_native(method, path, body=None, **kwargs):
        if path.endswith('/action'):
            committed.append(deepcopy(body))
            state['status'] = 'paused'
            raise OcrUnavailable()  # Native commit succeeded; only its response was lost.
        if path.endswith('/action-lookup'):
            looked_up.append(deepcopy(body))
        elif method != 'GET' or '/screen-batches/batchA?' not in path:
            return original_request(method, path, body, **kwargs)
        return {'batch_id': 'batchA', 'project_id': 'projectA', 'status': state['status'],
            'policy': {'primary_engine': 'glm'},
            'counts': {'total': 1, 'completed': 0, 'needs_review': 0, 'failed': 0, 'cancelled': 0, 'pending': 1},
            'items': [{'input_version_id': 'versionA', 'image_id': 'imageA', 'status': 'queued', 'issues': []}]}

    monkeypatch.setattr(native, '_request', control_native)
    monkeypatch.setattr(screen_tools, 'owned', lambda context, batch: None)
    ctx = SimpleNamespace(workspace_id='wtest', operation_id='controlA', services=SimpleNamespace(screen_batches=service))
    tool_args = screen_tools.Action(batch_id='batchA', action='pause')
    assert screen_tools.action_lookup(tool_args, ctx) is None
    assert not committed and not looked_up
    with pytest.raises(OcrUnavailable):
        service.action('wtest', 'batchA', ScreenAction(action='pause', request_id='controlA'))
    receipt = screen_tools.action_lookup(tool_args, ctx)
    assert receipt['status'] == 'success' and receipt['data']['status'] == 'paused'
    assert committed == looked_up == [{'action': 'pause', 'request_id': 'controlA', 'edits': []}]
    with pytest.raises(AssetSetConflict):
        screen_tools.action_lookup(screen_tools.Action(batch_id='batchA', action='resume'), ctx)
    assert len(committed) == len(looked_up) == 1


@pytest.mark.parametrize('kind', ['action', 'resolve'])
@pytest.mark.parametrize(('http_status', 'raw', 'absent'), [
    (200, b'null', True),
    (503, b'null', False),
    (200, b'invalid json', False),
    (200, b'[]', False),
])
def test_persisted_intent_lookup_uses_explicit_http_absence_without_resubmitting(
        issue_bridge, monkeypatch, kind, http_status, raw, absent):
    """Exercise the real HTTP decoder, not a mock returning Python None."""
    from workbench_platform.jobs import native_ocr

    service, _, resolution_args = issue_bridge
    adapter = native_ocr.OcrAdapter.__new__(native_ocr.OcrAdapter)
    adapter.port, adapter.timeout = 8765, 3.0
    adapter._credential = lambda: 'test-credential'
    transport = {'status': 503, 'raw': b'null'}
    calls, closed = [], []

    def connection(*args, **kwargs):
        response = SimpleNamespace(status=transport['status'], getheader=lambda *_: '0',
                                   read=lambda *_: transport['raw'])
        return SimpleNamespace(
            request=lambda method, path, **kw: calls.append((method, path)),
            getresponse=lambda: response, close=lambda: closed.append(True))

    monkeypatch.setattr(native_ocr.http.client, 'HTTPConnection', connection)
    original = {'link_id': 'linkA', 'asset_set_id': 'setA', 'asset_set_revision': 1}
    monkeypatch.setattr(service, '_adapter', lambda workspace, row:
        (adapter, original, service.fixed(workspace, row)))
    args = (ScreenAction(action='pause', request_id='nullControl')
            if kind == 'action' else resolution_args)
    operation = getattr(service, kind)
    # Persist the platform intent before a failed native response. Neither
    # a subsequent lookup nor an explicit absence response may submit again.
    with pytest.raises(OcrUnavailable):
        operation('wtest', 'batchA', args)
    transport.update(status=http_status, raw=raw)
    if absent:
        assert operation('wtest', 'batchA', args, allow_new=False) is None
    else:
        with pytest.raises(OcrUnavailable):
            operation('wtest', 'batchA', args, allow_new=False)
    assert calls == [
        ('POST', f'/api/projects/projectA/screen-batches/batchA/{kind}'),
        ('POST', f'/api/projects/projectA/screen-batches/batchA/{kind}-lookup'),
    ]
    assert len(closed) == 2
    with closing(service._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM screen_batch_refs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM screen_batch_actions').fetchone()[0] == 1


def test_diagnostic_model_page_bounds_text_without_mutating_original_evidence():
    diagnostics = {'items': [{'id': f'issue-{index}', 'evidence': [
        {'observation': {'text': 'original-value'*1000, 'box': [1, 2, 3, 4]}}]*30}
        for index in range(300)], 'summary': {'issues': 300}}
    before = hashlib.sha256(_json(diagnostics).encode()).hexdigest()
    page = diagnostic_page(diagnostics, 20, 10)
    assert page['total'] == 300 and len(page['items']) == 10
    assert page['items'][0]['id'] == 'issue-20'
    assert page['evidence_preview_only'] and page['full_evidence_in_service']
    assert len(_json(page).encode()) < 80000
    assert hashlib.sha256(_json(diagnostics).encode()).hexdigest() == before
