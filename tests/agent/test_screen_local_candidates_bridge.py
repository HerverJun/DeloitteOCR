"""Real SQLite intents with simulated native receipts; no inference or adoption."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from workbench_platform.agent.graph import screen_pending, screen_waiting, screen_controls, terminal_screen_delivery
from workbench_platform.agent.registry import ToolRegistry
from workbench_platform.agent.tools import screen_batches, screen_local_candidates
from workbench_platform.assets.sets import AssetSetConflict
from workbench_platform.jobs.native_ocr import OcrAdapter, OcrUnavailable
from workbench_platform.jobs.ocr_handoff import _json
from workbench_platform.jobs.screen_batches import ScreenBatches
from workbench_platform.jobs.screen_local_candidates import ScreenLocalSubmit, ScreenLocalControl
from workbench_platform.reader_requirements import infer_reader_requirements
from scripts.product_compatibility import DATA_COMPATIBILITY, check


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    service = ScreenBatches(tmp_path/'screen-batches.sqlite3', None, None)
    source = [{'input_version_id': 'versionA', 'image_id': 'imageA', 'asset_id': 'assetA'}]
    with closing(service._connect()) as db:
        db.execute('INSERT INTO screen_batch_refs VALUES(?,?,?,?,?,?,?,?,?)',
            ('wtest', 'original1', 'a'*64, 'handoffA', 'projectA', 'batchA', _json(source), '{}', 'now'))
    args = ScreenLocalSubmit(request_id='local001', budget_seconds=30,
        target={'input_version_id': 'versionA', 'result_id': 'resultA', 'result_revision': 2,
                'issue_ids': ['issueA', 'issueB']})

    class Native:
        def __init__(self):
            self.posts = self.lookups = self.controls = self.control_lookups = 0
            self.value = None
            self.control_value = None
            self.lose_post = self.lose_control = False
            self.fail_before_post = self.fail_before_control = False
            self.unreachable_lookup = self.unreachable_control_lookup = False
            self.file_calls = 0
            self.file_bytes = b'actual crop bytes'

        def receipt(self, body):
            return {'version': 'screen-local-candidates-v1', 'request_id': body['request_id'],
                'batch_id': 'batchA', 'root_batch_id': 'batchA', 'target': deepcopy(body['target']),
                'status': 'submitted', 'reason': 'candidate_requires_review',
                'task_ids': ['taskA', 'taskB'], 'owned_task_ids': ['taskA', 'taskB'],
                'values_modified': False, 'selection_applied': False, 'human_review': 'not_performed',
                'automatic_apply': False, 'execution_status': 'pending', 'counts': {'queued': 2},
                'total': 2, 'offset': 0, 'limit': 12, 'next_offset': None,
                'items': [{'task_id': identifier, 'status': 'queued', 'current_binding': True,
                    'batch_id': 'batchA', 'root_batch_id': 'batchA', 'request_id': body['request_id'],
                    'input_version_id': 'versionA', 'parent_result_id': 'resultA',
                    'candidate_state': 'pending', 'text': None, 'text_preview_only': False,
                    'content_status': None, 'artifact': None,
                    'values_modified': False, 'selection_applied': False, 'human_review': 'not_performed',
                    'binding': {'batch_id': 'batchA', 'input_version_id': 'versionA', 'result_id': 'resultA', 'result_revision': 2,
                                'source_sha256': 'a'*64, 'cell': {'location': {'level': 'cell'}}}}
                    for identifier in ('taskA', 'taskB')]}

        def _request(self, method, path, body=None, **kwargs):
            if path.endswith('/control-lookup'):
                self.control_lookups += 1
                if self.unreachable_control_lookup:
                    raise OcrUnavailable()
                return deepcopy(self.control_value)
            if path.endswith('/control'):
                if self.fail_before_control:
                    self.fail_before_control = False
                    raise OcrUnavailable()
                self.controls += 1
                self.control_value = deepcopy(self.value)
                self.control_value['counts'] = {'paused': 2}
                for item in self.control_value['items']:
                    item['status'] = 'paused'
                self.control_value['control'] = {'version': 'screen-local-control-v1',
                    'request_id': body['request_id'], 'submission_request_id': 'local001', 'batch_id': 'batchA',
                    'action': body['action'], 'affected_task_ids': ['taskA', 'taskB'], 'status': 'applied',
                    'committed': True, 'values_modified': False, 'selection_applied': False,
                    'human_review': 'not_performed'}
                if self.lose_control:
                    self.lose_control = False
                    raise OcrUnavailable()
                return deepcopy(self.control_value)
            if path.endswith('/lookup'):
                self.lookups += 1
                if self.unreachable_lookup:
                    raise OcrUnavailable()
                return deepcopy(self.value)
            if method == 'POST':
                if self.fail_before_post:
                    self.fail_before_post = False
                    raise OcrUnavailable()
                self.posts += 1
                self.value = self.receipt(body)
                if self.lose_post:
                    self.lose_post = False
                    raise OcrUnavailable()
                return deepcopy(self.value)
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(path).query)
            offset, limit = int(query['offset'][0]), int(query['limit'][0])
            value = deepcopy(self.value)
            value.update(offset=offset, limit=limit, items=value['items'][offset:offset+limit],
                         next_offset=offset+limit if offset+limit < value['total'] else None)
            return value

        def screen_local_file(self, *args):
            self.file_calls += 1
            return self.file_bytes, 'image/png'

    native = Native()
    monkeypatch.setattr(service, '_adapter', lambda *a: (native, {}, SimpleNamespace()))
    return service, native, args


def test_lost_submission_recovers_original_tasks_without_resubmitting(bridge):
    service, native, args = bridge
    native.lose_post = True
    with pytest.raises(OcrUnavailable):
        service.local_candidates('wtest', 'batchA', args)
    value = service.local_candidates('wtest', 'batchA', args, allow_new=False)
    assert value['task_ids'] == ['taskA', 'taskB']
    assert (native.posts, native.lookups) == (1, 1)
    service.local_candidates('wtest', 'batchA', args)
    assert (native.posts, native.lookups) == (1, 2)


def test_unknown_lookup_and_foreign_source_never_submit(bridge):
    service, native, args = bridge
    assert service.local_candidates('wtest', 'batchA', args, allow_new=False) is None
    with pytest.raises(ValueError, match='不属于'):
        service.local_candidates('wtest', 'batchA', args.model_copy(update={'target':
            args.target.model_copy(update={'input_version_id': 'foreignVersion'})}))
    assert native.posts == native.lookups == 0
    with pytest.raises(KeyError):
        service.local_candidates('otherWorkspace', 'batchA', args)


def test_changed_request_parameters_and_control_identity_conflict(bridge):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    with pytest.raises(AssetSetConflict):
        service.local_candidates('wtest', 'batchA', args.model_copy(update={'budget_seconds': 60}))
    with pytest.raises(AssetSetConflict):
        service.local_candidates_control('wtest', 'batchA', args.request_id,
            ScreenLocalControl(request_id=args.request_id, action='cancel'))
    assert native.posts == 1 and native.controls == 0


@pytest.mark.parametrize(('field', 'value'), [
    ('request_id', 'foreign'), ('batch_id', 'foreign'), ('values_modified', True),
    ('selection_applied', True), ('human_review', 'confirmed'), ('automatic_apply', True),
    ('task_ids', ['taskA', 'taskA']), ('owned_task_ids', ['foreignTask']),
    ('counts', {'queued': True}), ('execution_status', 'succeeded'),
])
def test_corrupt_native_identity_or_success_claim_is_rejected(bridge, monkeypatch, field, value):
    service, native, args = bridge
    original = native.receipt
    def corrupt(body):
        result = original(body)
        result[field] = value
        return result
    monkeypatch.setattr(native, 'receipt', corrupt)
    with pytest.raises(OcrUnavailable):
        service.local_candidates('wtest', 'batchA', args)
    with closing(service._connect()) as db:
        saved = db.execute('SELECT * FROM screen_local_request_refs').fetchone()
    assert saved['task_ids_json'] is None


def test_read_preserves_historical_revision_and_checks_pagination(bridge):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    native.value['items'][0]['binding']['result_revision'] = 1
    native.value['items'][0].update(text='old candidate', candidate_state='available')
    value = service.local_candidates_status('wtest', 'batchA', 'local001', offset=0, limit=1)
    assert value['items'][0]['candidate_state'] == 'stale'
    assert value['items'][0]['current_binding'] is False
    assert value['next_offset'] == 1
    assert value['items'][0]['binding']['result_revision'] == 1
    page = service.local_candidates_status('wtest', 'batchA', 'local001', offset=1, limit=1)
    assert page['items'][0]['task_id'] == 'taskB' and page['next_offset'] is None
    native.value['task_ids'][0] = 'otherTask'
    with pytest.raises(OcrUnavailable):
        service.local_candidates_status('wtest', 'batchA', 'local001')


def test_control_response_loss_uses_original_operation_and_task_set(bridge):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    command = ScreenLocalControl(request_id='pause001', action='pause')
    assert service.local_candidates_control('wtest', 'batchA', 'local001', command, allow_new=False) is None
    native.lose_control = True
    with pytest.raises(OcrUnavailable):
        service.local_candidates_control('wtest', 'batchA', 'local001', command)
    value = service.local_candidates_control('wtest', 'batchA', 'local001', command, allow_new=False)
    assert value['counts'] == {'paused': 2}
    assert value['control']['request_id'] == 'pause001'
    assert native.controls == native.control_lookups == 1
    assert screen_local_candidates.compact(value)['status'] == 'paused'


def test_strict_local_request_cannot_inject_geometry_model_or_duplicate_issues(bridge):
    _, _, args = bridge
    body = args.model_dump()
    for extra in ({'bbox': [0, 0, 10, 10]}, {'engine': 'hunyuan'}, {'scale': 8}):
        with pytest.raises(ValidationError):
            ScreenLocalSubmit.model_validate({**body, **extra})
    body['target']['issue_ids'] = ['same', 'same']
    with pytest.raises(ValidationError):
        ScreenLocalSubmit.model_validate(body)


def test_actual_local_intent_requires_new_reader_even_without_marker(bridge):
    service, _, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    marker = service.path.parent/'product-reader-requirements.json'
    marker.unlink()  # deliberate loss fixture; inference must protect stored data
    requirements = infer_reader_requirements(service.path.parent)
    assert 'screen-local-candidates-v1' in requirements['reader_features']
    old = deepcopy(DATA_COMPATIBILITY)
    old['reader_features'].remove('screen-local-candidates-v1')
    with pytest.raises(ValueError, match='不能直接降级'):
        check(service.path.parent, old)
    assert check(service.path.parent, DATA_COMPATIBILITY)['compatible']
    assert not marker.exists()


def test_local_job_waits_and_controls_but_cannot_satisfy_excel_delivery():
    ref = {'application': 'ocr', 'coverage': {'kind': 'screen_local_candidates'}, 'object_id': 'localJob'}
    result = {'status': 'success', 'job_refs': [ref], 'coverage': {'total': 1, 'pending': 0}}
    assert screen_waiting(result) and not screen_pending(result)
    assert screen_controls([result]) == ['ocr_screen_local_candidates_control']
    delivery = {'data': {'verified': True, 'screen_batch_coverage': [{'batch_id': 'localJob',
        'current_verified': True, 'pending': 0, 'total': 1, 'file_count': 1, 'settled_without_file': 0}]}}
    assert not terminal_screen_delivery({'messages': [{'role': 'tool', 'result': result}]}, delivery)


def test_registered_local_tools_have_recovery_and_bounded_inputs():
    registry = ToolRegistry()
    screen_batches.register(registry)
    names = {'ocr_screen_local_candidates', 'ocr_screen_local_candidates_read', 'ocr_screen_local_candidates_control'}
    assert names <= set(registry.tools)
    assert registry.tools['ocr_screen_local_candidates'].lookup_operation is not None
    assert registry.tools['ocr_screen_local_candidates_control'].lookup_operation is not None
    with pytest.raises(ValidationError):
        screen_local_candidates.Run.model_validate({'batch_id': 'batchA', 'budget_seconds': 901,
            'target': {'input_version_id': 'vA', 'result_id': 'rA', 'result_revision': 0, 'issue_ids': ['iA']}})


def test_submission_before_send_failure_requires_confirmed_absence_to_continue(bridge):
    service, native, args = bridge
    native.fail_before_post = True
    with pytest.raises(OcrUnavailable):
        service.local_candidates('wtest', 'batchA', args)
    assert native.posts == 0
    assert service.local_candidates('wtest', 'batchA', args, allow_new=False) is None
    assert native.posts == 0
    native.unreachable_lookup = True
    with pytest.raises(OcrUnavailable):
        service.local_candidates('wtest', 'batchA', args)
    assert native.posts == 0
    native.unreachable_lookup = False
    value = service.local_candidates('wtest', 'batchA', args)
    assert value['request_id'] == args.request_id and value['target'] == args.target.model_dump()
    assert (native.posts, native.lookups) == (1, 3)
    service.local_candidates('wtest', 'batchA', args)
    assert native.posts == 1


def test_control_before_send_failure_recovers_same_operation_only_after_null_lookup(bridge):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    command = ScreenLocalControl(request_id='pause001', action='pause')
    native.fail_before_control = True
    with pytest.raises(OcrUnavailable):
        service.local_candidates_control('wtest', 'batchA', args.request_id, command)
    assert native.controls == 0
    assert service.local_candidates_control('wtest', 'batchA', args.request_id, command, allow_new=False) is None
    native.unreachable_control_lookup = True
    with pytest.raises(OcrUnavailable):
        service.local_candidates_control('wtest', 'batchA', args.request_id, command)
    assert native.controls == 0
    native.unreachable_control_lookup = False
    value = service.local_candidates_control('wtest', 'batchA', args.request_id, command)
    assert value['control']['request_id'] == command.request_id and value['control']['committed'] is True
    assert (native.controls, native.control_lookups) == (1, 3)
    service.local_candidates_control('wtest', 'batchA', args.request_id, command)
    assert native.controls == 1


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(counts={'succeeded': 2}, execution_status='succeeded'),
    lambda v: v['items'][0].update(batch_id='foreignBatch'),
    lambda v: v['items'][0]['binding'].update(batch_id='foreignBatch'),
    lambda v: v['items'][0].update(root_batch_id='foreignRoot'),
    lambda v: v['items'][0].update(request_id='foreignRequest'),
    lambda v: v['items'][0].update(input_version_id='foreignVersion'),
    lambda v: v['items'][0].update(parent_result_id='foreignResult'),
    lambda v: v['items'][0].update(binding=None),
])
def test_contradictory_item_state_or_foreign_binding_cannot_settle_local_job(bridge, mutation):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    mutation(native.value)
    with pytest.raises(OcrUnavailable):
        service.local_candidates_status('wtest', 'batchA', args.request_id)


def test_partial_page_states_cannot_exceed_global_counts(bridge):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    native.value['items'][0]['status'] = 'succeeded'
    with pytest.raises(OcrUnavailable):
        service.local_candidates_status('wtest', 'batchA', args.request_id, limit=1)
    native.value['counts'] = {'queued': 1, 'succeeded': 1}
    assert service.local_candidates_status('wtest', 'batchA', args.request_id, limit=1)['next_offset'] == 1


@pytest.mark.parametrize('artifact', [None, {}, {'crop.png': {'sha256': 'invalid'}}])
def test_candidate_file_requires_actual_sha_manifest_before_native_download(bridge, artifact):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    native.value['items'][0]['artifact'] = artifact
    with pytest.raises(KeyError, match='产物清单'):
        service.local_candidate_file('wtest', 'batchA', args.request_id, 'taskA', 'crop.png')
    assert native.file_calls == 0


def test_candidate_file_rejects_changed_bytes_and_accepts_verified_crop(bridge):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    native.value['items'][0]['artifact'] = {'crop.png': {'sha256': hashlib.sha256(native.file_bytes).hexdigest()}}
    data, media = service.local_candidate_file('wtest', 'batchA', args.request_id, 'taskA', 'crop.png')
    assert data == native.file_bytes and media == 'image/png'
    native.file_bytes = b'changed crop bytes'
    with pytest.raises(OcrUnavailable):
        service.local_candidate_file('wtest', 'batchA', args.request_id, 'taskA', 'crop.png')
    assert native.file_calls == 2


def test_lifecycle_checks_all_candidates_before_ending_wait(bridge, monkeypatch):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    native.value.update(counts={'succeeded': 2}, execution_status='succeeded')
    native.value['items'][0].update(status='succeeded', text='value', candidate_state='available')
    ref = {'object_id': 'localJob', 'parent_id': 'projectA', 'native_id': args.request_id,
           'coverage': {'batch_id': 'batchA'}}
    monkeypatch.setattr(screen_local_candidates, 'owned', lambda *_: ref)
    ctx = SimpleNamespace(workspace_id='wtest', services=SimpleNamespace(screen_batches=service))
    with pytest.raises(OcrUnavailable):
        screen_local_candidates.settle(ref, ctx)
    native.value['items'][1].update(status='succeeded', text='other value', candidate_state='available')
    result = screen_local_candidates.settle(ref, ctx)
    assert result['status'] == 'success' and result['coverage']['needs_review'] == 2
    assert result['coverage']['pending'] == 0 and result['data']['values_modified'] is False


def test_reused_candidate_reports_readable_status_without_foreign_task_controls(bridge):
    service, native, args = bridge
    service.local_candidates('wtest', 'batchA', args)
    native.value.update(status='reused', owned_task_ids=[])
    for item in native.value['items']:
        item['request_id'] = 'previousRequest'
    # The real request identity is established separately, before the reused
    # receipt arrives; no current operation obtains ownership of old tasks.
    from workbench_platform.jobs.screen_local_candidates import validate_receipt
    value = validate_receipt(native.value, args.model_dump(), 'batchA')
    data = screen_local_candidates.compact(value)
    assert data['counts']['pending'] == 2 and data['owned_task_count'] == 0
    assert data['capabilities'] == [] and data['values_modified'] is False


@pytest.mark.parametrize(('http_status', 'raw'), [(503, b'null'), (200, b'invalid json'), (200, b'[]')])
def test_adapter_null_permission_does_not_turn_invalid_responses_into_absence(monkeypatch, http_status, raw):
    from workbench_platform.jobs import native_ocr
    closed = []
    response = SimpleNamespace(status=http_status, getheader=lambda *_: '0', read=lambda *_: raw)
    connection = SimpleNamespace(request=lambda *a, **kw: None, getresponse=lambda: response,
                                 close=lambda: closed.append(True))
    monkeypatch.setattr(native_ocr.http.client, 'HTTPConnection', lambda *a, **kw: connection)
    adapter = OcrAdapter.__new__(OcrAdapter)
    adapter.port, adapter.timeout = 8765, 3.0
    adapter._credential = lambda: 'test-credential'
    with pytest.raises(OcrUnavailable):
        adapter._request('POST', '/lookup', {}, allow_null=True)
    assert closed == [True]


def test_adapter_only_explicit_null_lookup_permission_accepts_json_null(monkeypatch):
    from workbench_platform.jobs import native_ocr
    response = SimpleNamespace(status=200, getheader=lambda *_: '4', read=lambda *_: b'null')
    connection = SimpleNamespace(request=lambda *a, **kw: None, getresponse=lambda: response, close=lambda: None)
    monkeypatch.setattr(native_ocr.http.client, 'HTTPConnection', lambda *a, **kw: connection)
    adapter = OcrAdapter.__new__(OcrAdapter)
    adapter.port, adapter.timeout = 8765, 3.0
    adapter._credential = lambda: 'test-credential'
    assert adapter._request('POST', '/lookup', {}, allow_null=True) is None
    with pytest.raises(OcrUnavailable):
        adapter._request('POST', '/submit', {})
