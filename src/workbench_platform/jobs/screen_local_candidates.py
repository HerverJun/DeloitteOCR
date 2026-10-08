"""Durable, scoped references to native cell candidates, separate from page results."""
from collections import Counter
from contextlib import closing
import hashlib
import json
import re
from typing import Literal

from pydantic import Field, model_validator

from packages.contracts.agent import AgentModel, Identity
from workbench_platform.assets.sets import AssetSetConflict
from workbench_platform.jobs.native_ocr import OcrUnavailable
from workbench_platform.jobs.ocr_handoff import _json, _utc
from workbench_platform.reader_requirements import mark_reader_feature

VERSION = 'screen-local-candidates-v1'
STATES = {'queued', 'running', 'paused', 'interrupted', 'succeeded', 'failed', 'cancelled'}
ACTIVE = {'queued', 'running', 'paused', 'interrupted'}
NATIVE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$')


class ScreenLocalTarget(AgentModel):
    input_version_id: Identity
    result_id: Identity
    result_revision: int = Field(ge=0, strict=True)
    issue_ids: list[Identity] = Field(min_length=1, max_length=12)

    @model_validator(mode='after')
    def distinct_issues(self):
        if len(set(self.issue_ids)) != len(self.issue_ids):
            raise ValueError('疑点不能重复')
        return self


class ScreenLocalSubmit(AgentModel):
    request_id: Identity
    budget_seconds: int = Field(ge=10, le=900, strict=True)
    target: ScreenLocalTarget


class ScreenLocalControl(AgentModel):
    request_id: Identity
    action: Literal['pause', 'resume', 'cancel', 'retry_failed']


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS screen_local_request_refs(
        workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,batch_id TEXT NOT NULL,
        body_json TEXT NOT NULL,task_ids_json TEXT,owned_task_ids_json TEXT,created TEXT NOT NULL,
        PRIMARY KEY(workspace_id,request_id))''')


def saved(service, workspace, batch_id, request_id):
    service._row(workspace, batch_id)
    with closing(service._connect()) as db:
        row = db.execute('SELECT * FROM screen_local_request_refs WHERE workspace_id=? AND request_id=?',
                         (workspace, request_id)).fetchone()
    if row is None or row['batch_id'] != batch_id:
        raise KeyError('局部候选请求不属于当前批次')
    return row


def _endpoint(row, batch_id):
    return f'/api/projects/{row["project_id"]}/screen-batches/{batch_id}/local-candidates'


def _identifiers(value):
    return (isinstance(value, list) and len(value) <= 12
            and all(isinstance(item, str) and NATIVE_ID.fullmatch(item) for item in value)
            and len(set(value)) == len(value))


def validate_receipt(value, body, batch_id, *, offset=0, limit=12, previous=None):
    if (not isinstance(value, dict) or value.get('version') != VERSION
            or value.get('request_id') != body['request_id'] or value.get('batch_id') != batch_id
            or (value.get('task_ids') and (not isinstance(value.get('root_batch_id'), str)
                or not NATIVE_ID.fullmatch(value['root_batch_id'])))
            or value.get('target') != body['target']
            or value.get('status') not in {'submitted', 'reused', 'blocked'}
            or value.get('values_modified') is not False or value.get('selection_applied') is not False
            or value.get('human_review') != 'not_performed' or value.get('automatic_apply') is not False
            or not _identifiers(value.get('task_ids')) or not _identifiers(value.get('owned_task_ids'))
            or not set(value['owned_task_ids']) <= set(value['task_ids'])):
        raise OcrUnavailable()
    if previous is not None and previous['task_ids_json'] is not None:
        if (json.loads(previous['task_ids_json']) != value['task_ids']
                or json.loads(previous['owned_task_ids_json']) != value['owned_task_ids']):
            raise OcrUnavailable()
    counts, items = value.get('counts'), value.get('items')
    total = len(value['task_ids'])
    if (not isinstance(counts, dict) or not set(counts) <= STATES
            or any(type(number) is not int or number < 0 for number in counts.values())
            or sum(counts.values()) != total or type(value.get('total')) is not int or value['total'] != total
            or type(value.get('offset')) is not int or value['offset'] != offset
            or type(value.get('limit')) is not int or value['limit'] != limit
            or not isinstance(items, list) or len(items) != max(0, min(limit, total-offset))
            or [item.get('task_id') for item in items if isinstance(item, dict)] != value['task_ids'][offset:offset+limit]):
        raise OcrUnavailable()
    if ((value.get('next_offset') is not None and type(value['next_offset']) is not int)
            or value.get('next_offset') != (offset+limit if offset+limit < total else None)):
        raise OcrUnavailable()
    execution = ('pending' if any(counts.get(state, 0) for state in ACTIVE)
                 else 'succeeded' if total and counts.get('succeeded') == total
                 else value['status'] if not total else 'partial' if counts.get('succeeded') else 'failed')
    if value.get('execution_status') != execution:
        raise OcrUnavailable()
    for item in items:
        binding = item.get('binding', {})
        artifact = item.get('artifact')
        if (not isinstance(binding, dict)
                or (artifact is not None and (not isinstance(artifact, dict)
                    or any(not isinstance(name, str) or not isinstance(entry, dict)
                           for name, entry in artifact.items())))
                or item.get('batch_id') != batch_id or binding.get('batch_id') != batch_id
                or item.get('root_batch_id') != value['root_batch_id']
                or not isinstance(item.get('request_id'), str) or not NATIVE_ID.fullmatch(item['request_id'])
                or (item['task_id'] in value['owned_task_ids'] and item['request_id'] != body['request_id'])
                or item.get('input_version_id') != body['target']['input_version_id']
                or item.get('parent_result_id') != binding.get('result_id')
                or not isinstance(binding.get('result_id'), str) or not NATIVE_ID.fullmatch(binding['result_id'])
                or type(binding.get('result_revision')) is not int or binding['result_revision'] < 0
                or item.get('status') not in STATES or type(item.get('current_binding')) is not bool
                or binding.get('input_version_id') != body['target']['input_version_id']
                or item.get('values_modified') is not False or item.get('selection_applied') is not False
                or item.get('human_review') != 'not_performed'
                or item.get('candidate_state') not in {'pending', 'available', 'stale', 'failed'}
                or (item.get('text') is not None and not isinstance(item['text'], str))):
            raise OcrUnavailable()
        if (binding.get('result_id') != body['target']['result_id']
                or binding.get('result_revision') != body['target']['result_revision']):
            # A deduplicated pixel attempt can be a historical result. It is
            # evidence, never a candidate for silently editing the new result.
            item['candidate_state'] = 'stale'
            item['current_binding'] = False
        if item.get('candidate_state') == 'available' and not item['current_binding']:
            raise OcrUnavailable()
    page_counts = Counter(item['status'] for item in items)
    if (any(number > counts.get(state, 0) for state, number in page_counts.items())
            or (offset == 0 and len(items) == total
                and any(page_counts.get(state, 0) != counts.get(state, 0) for state in STATES))):
        raise OcrUnavailable()
    return value


def remember(service, workspace, batch_id, value):
    with service.lock, closing(service._connect()) as db:
        row = saved(service, workspace, batch_id, value['request_id'])
        if row['task_ids_json'] is not None:
            if (json.loads(row['task_ids_json']) != value['task_ids']
                    or json.loads(row['owned_task_ids_json']) != value['owned_task_ids']):
                raise AssetSetConflict('局部候选任务身份已变化')
        else:
            db.execute('UPDATE screen_local_request_refs SET task_ids_json=?,owned_task_ids_json=? '
                       'WHERE workspace_id=? AND request_id=?',
                       (_json(value['task_ids']), _json(value['owned_task_ids']), workspace, value['request_id']))
    return value


def submit(service, workspace, batch_id, args: ScreenLocalSubmit, *, allow_new=True):
    row = service._row(workspace, batch_id)
    adapter, _, _ = service._adapter(workspace, row)
    body = args.model_dump(mode='json')
    sources = json.loads(row['source_json'])
    if args.target.input_version_id not in {item['input_version_id'] for item in sources}:
        raise ValueError('疑点图片不属于当前批次')
    identity = hashlib.sha256(_json({'batch_id': batch_id, 'local_candidates': body}).encode()).hexdigest()
    with service.lock, closing(service._connect()) as db:
        old = db.execute('SELECT * FROM screen_batch_actions WHERE workspace_id=? AND request_id=?',
                         (workspace, args.request_id)).fetchone()
        if old and (old['batch_id'] != batch_id or old['payload_hash'] != identity):
            raise AssetSetConflict('请求编号已绑定其他参数或批次')
        if old is None:
            if not allow_new:
                return None
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT INTO screen_batch_actions VALUES(?,?,?,?)',
                           (workspace, args.request_id, batch_id, identity))
                db.execute('INSERT INTO screen_local_request_refs VALUES(?,?,?,?,NULL,NULL,?)',
                           (workspace, args.request_id, batch_id, _json(body), _utc()))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        previous = saved(service, workspace, batch_id, args.request_id)
        mark_reader_feature(service.path.parent, VERSION)
    endpoint = _endpoint(row, batch_id)
    if old is not None or not allow_new:
        value = adapter._request('POST', endpoint+'/lookup', body, max_body=128000,
                                 result_conflict=True, allow_null=True)
        if value is None and allow_new:
            # Only a confirmed absent native receipt permits submission of
            # this same frozen identity. An unreachable lookup never does.
            value = adapter._request('POST', endpoint, body, max_body=128000, result_conflict=True)
    else:
        value = adapter._request('POST', endpoint, body, max_body=128000, result_conflict=True)
    if value is None:
        return None
    return remember(service, workspace, batch_id, validate_receipt(value, body, batch_id, previous=previous))


def read(service, workspace, batch_id, request_id, *, offset=0, limit=12):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12:
        raise ValueError('局部候选分页范围无效')
    previous = saved(service, workspace, batch_id, request_id)
    row = service._row(workspace, batch_id)
    adapter, _, _ = service._adapter(workspace, row)
    value = adapter._request('GET', _endpoint(row, batch_id) + f'/{request_id}?offset={offset}&limit={limit}',
                             result_conflict=True)
    return remember(service, workspace, batch_id,
                    validate_receipt(value, json.loads(previous['body_json']), batch_id,
                                     offset=offset, limit=limit, previous=previous))


def control(service, workspace, batch_id, request_id, args: ScreenLocalControl, *, allow_new=True):
    original = saved(service, workspace, batch_id, request_id)
    row = service._row(workspace, batch_id)
    adapter, _, _ = service._adapter(workspace, row)
    body = args.model_dump(mode='json')
    identity = hashlib.sha256(_json({'batch_id': batch_id, 'local_request_id': request_id,
                                   'local_control': body}).encode()).hexdigest()
    with service.lock, closing(service._connect()) as db:
        previous = db.execute('SELECT * FROM screen_batch_actions WHERE workspace_id=? AND request_id=?',
                              (workspace, args.request_id)).fetchone()
        if previous and (previous['batch_id'] != batch_id or previous['payload_hash'] != identity):
            raise AssetSetConflict('控制编号已绑定其他操作')
        if previous is None:
            if not allow_new:
                return None
            db.execute('INSERT INTO screen_batch_actions VALUES(?,?,?,?)',
                       (workspace, args.request_id, batch_id, identity))
    endpoint = _endpoint(row, batch_id) + f'/{request_id}/'
    if previous is not None or not allow_new:
        value = adapter._request('POST', endpoint+'control-lookup', body, result_conflict=True, allow_null=True)
        if value is None and allow_new:
            value = adapter._request('POST', endpoint+'control', body, result_conflict=True)
    else:
        value = adapter._request('POST', endpoint+'control', body, result_conflict=True)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise OcrUnavailable()
    operation = value.get('control', {})
    expected = {'version': 'screen-local-control-v1', 'request_id': args.request_id,
                'submission_request_id': request_id, 'batch_id': batch_id, 'action': args.action,
                'status': 'applied', 'committed': True, 'values_modified': False,
                'selection_applied': False, 'human_review': 'not_performed'}
    if (not isinstance(operation, dict) or any(operation.get(key) != item for key, item in expected.items())
            or operation.get('committed') is not True or operation.get('values_modified') is not False
            or operation.get('selection_applied') is not False
            or not _identifiers(operation.get('affected_task_ids'))
            or not set(operation['affected_task_ids']) <= set(json.loads(original['owned_task_ids_json'] or '[]'))):
        raise OcrUnavailable()
    return remember(service, workspace, batch_id,
                    validate_receipt(value, json.loads(original['body_json']), batch_id, previous=original))


def artifact_file(service, workspace, batch_id, request_id, task_id, file_id):
    if file_id not in {'raw.json', 'result.json', 'error.json', 'input.png', 'result.txt',
                       'result.md', 'crop.png', 'crop-binding.json'}:
        raise KeyError('局部候选文件不存在')
    value = read(service, workspace, batch_id, request_id)
    item = next((item for item in value['items'] if item['task_id'] == task_id), None)
    if item is None:
        raise KeyError('局部候选任务不属于此请求')
    expected = (item.get('artifact') or {}).get(file_id)
    if not isinstance(expected, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(expected.get('sha256'))):
        raise KeyError('尚无此候选文件的实际产物清单')
    row = service._row(workspace, batch_id)
    adapter, _, _ = service._adapter(workspace, row)
    data, media = adapter.screen_local_file(row['project_id'], batch_id, request_id, task_id, file_id)
    if hashlib.sha256(data).hexdigest() != expected['sha256']:
        raise OcrUnavailable()
    return data, media
