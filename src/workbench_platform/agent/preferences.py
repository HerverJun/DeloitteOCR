"""Versioned defaults and immutable run snapshots in the existing Agent ledger."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import Field, StrictBool, StrictInt
from packages.contracts.agent import AgentModel
from .store import Conflict, RunSuperseded, canonical, digest, now
from workbench_platform.reader_requirements import mark_reader_feature


Business = Literal['project', 'screen_excel', 'invoice_reconciliation',
                   'payment_reconciliation', 'classification', 'evidence_workpaper']


class PreferenceValues(AgentModel):
    output_layout: Literal['per_image'] | None = None
    value_mode: Literal['text', 'confirmed_numeric'] | None = None
    include_pending_review: StrictBool | None = None
    require_human_review: Literal[True] | None = None
    strategy_id: Literal['screen-hunyuan-failure-glm-v1'] | None = None
    worksheet: str | None = Field(default=None, min_length=1, max_length=120)
    key_columns: list[str] | None = Field(default=None, min_length=1, max_length=20)
    amount_column: str | None = Field(default=None, min_length=1, max_length=120)
    amount_unit: str | None = Field(default=None, min_length=1, max_length=80)
    decimal_places: StrictInt | None = Field(default=None, ge=0, le=12)


class PreferenceSource(AgentModel):
    mode: Literal['user_event', 'user_form']
    quote: str = Field(min_length=1, max_length=2000)
    event_seq: StrictInt | None = Field(default=None, ge=1)


class PreferenceWrite(AgentModel):
    request_id: str = Field(min_length=1, max_length=128)
    business: Business = 'project'
    expected_revision: StrictInt = Field(ge=0)
    values: PreferenceValues = Field(default_factory=PreferenceValues)
    revoke_keys: list[str] = Field(default_factory=list, max_length=20)
    source: PreferenceSource


def checked_values(values):
    raw = values.model_dump(exclude_unset=True) if isinstance(values, PreferenceValues) else values
    checked = PreferenceValues.model_validate(raw).model_dump(exclude_unset=True)
    if any(value is None for value in checked.values()):
        raise ValueError('撤回偏好请使用revoke_keys，不使用空值')
    if 'key_columns' in checked and (any(not value.strip() or len(value) > 120 for value in checked['key_columns'])
                                   or len(set(checked['key_columns'])) != len(checked['key_columns'])):
        raise ValueError('业务键列无效或重复')
    return checked


def task_overrides(goal, business):
    """Only unambiguous one-task formatting instructions, no inferred defaults.

    confirmed_numeric requests still require per-version human-confirmed columns
    at export. The preference never supplies those columns or a confirmation.
    """
    if business != 'screen_excel' or not isinstance(goal, str):
        return {}
    result = {}
    positive = re.sub(r'(?:不要|无需|不用|不需要)\s*[^，。；;\n]{0,80}', '', goal)
    if re.search(r'每(?:张|个|幅)(?:图片|图|照片)?.{0,8}(?:一个|单独|独立).{0,8}(?:excel|xlsx|文件)', positive, re.I):
        result['output_layout'] = 'per_image'
    if re.search(r'(?:原值|单元格|字段|内容).{0,8}(?:按|作为|保持|保留).{0,4}文本|文本(?:格式|导出)', positive):
        result['value_mode'] = 'text'
    if re.search(r'(?:包含|包括|也导出|一并导出).{0,6}待复核|待复核.{0,6}(?:也导出|一并导出)', positive):
        result['include_pending_review'] = True
    if re.search(r'(?:不包含|不包括|不要包含|不要包括|不要导出|排除).{0,6}待复核|只导出.{0,6}(?:已确认|已复核)', goal):
        result['include_pending_review'] = False
    return result


class TaskPreferences:
    """Defaults may change during a run; its frozen snapshot never changes."""
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS agent_preference_profiles(
                    workspace_id TEXT NOT NULL,business TEXT NOT NULL,revision INTEGER NOT NULL,
                    body TEXT NOT NULL,updated TEXT NOT NULL,PRIMARY KEY(workspace_id,business));
                CREATE TABLE IF NOT EXISTS agent_preference_requests(
                    request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_preference_snapshots(
                    run_id TEXT NOT NULL,business TEXT NOT NULL,workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,body TEXT NOT NULL,sha256 TEXT NOT NULL,created TEXT NOT NULL,
                    PRIMARY KEY(run_id,business));
            ''')

    @staticmethod
    def _business(business):
        if business not in ('project', 'screen_excel', 'invoice_reconciliation',
                            'payment_reconciliation', 'classification', 'evidence_workpaper'):
            raise ValueError('业务偏好作用域无效')
        return business

    @staticmethod
    def _profile(db, workspace_id, business):
        row = db.execute('SELECT * FROM agent_preference_profiles WHERE workspace_id=? AND business=?',
                         (workspace_id, business)).fetchone()
        return {'workspace_id': workspace_id, 'business': business,
                'revision': row['revision'] if row else 0,
                'entries': json.loads(row['body']) if row else {},
                'updated': row['updated'] if row else None}

    def preview(self, workspace_id, business='project'):
        self._business(business)
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError('偏好需要明确项目')
        with self.store.transaction() as db:
            project = self._profile(db, workspace_id, 'project')
            specific = self._profile(db, workspace_id, business)
        effective = {**project['entries'], **specific['entries']}
        return {'profile': specific, 'project_profile': project,
            'effective_values': {key: item['value'] for key, item in effective.items()},
            'effective_sources': effective, 'defaults_apply_to_future_runs': True}

    def _origin(self, db, workspace_id, source, context):
        source = PreferenceSource.model_validate(source).model_dump()
        if context is not None:
            if context.workspace_id != workspace_id:
                raise ValueError('偏好操作不属于当前项目')
            run = db.execute('SELECT * FROM runs WHERE id=?', (context.run_id,)).fetchone()
            if not run or run['generation'] != context.generation or run['status'] not in ('running', 'waiting_job', 'waiting_user'):
                raise RunSuperseded('偏好操作的执行代际已失效')
            if run['workspace_id'] != workspace_id or run['session_id'] != context.session_id:
                raise ValueError('偏好操作会话范围无效')
            if source['mode'] != 'user_event' or source['event_seq'] is None:
                raise ValueError('Agent保存偏好须引用用户明确指令')
            event = db.execute('SELECT * FROM events WHERE seq=? AND session_id=?',
                               (source['event_seq'], context.session_id)).fetchone()
            payload = json.loads(event['payload']) if event else {}
            if payload.get('role') != 'user' or source['quote'] not in str(payload.get('content', '')):
                raise ValueError('偏好来源不是当前会话真实用户原文')
            if not re.search(r'默认|以后|今后|记住|偏好|总是|日常|default|remember|always', source['quote'], re.I):
                raise ValueError('单次指令不自动保存成默认偏好')
            source.update({'actor': 'agent_from_user_instruction', 'session_id': context.session_id})
        elif source['mode'] != 'user_form' or source['event_seq'] is not None:
            raise ValueError('页面偏好修改须显式提交用户表单')
        else:
            source['actor'] = 'user'
        return source

    def update(self, workspace_id, business, expected_revision, values, revoke_keys, source,
               request_id, *, context=None):
        business = self._business(business)
        values = checked_values(values)
        if type(expected_revision) is not int or expected_revision < 0 or not workspace_id:
            raise ValueError('偏好项目或版本无效')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('偏好需要稳定请求身份')
        if not isinstance(revoke_keys, list) or len(set(revoke_keys)) != len(revoke_keys) or len(revoke_keys) > 20:
            raise ValueError('撤回偏好字段无效')
        allowed = set(PreferenceValues.model_fields)
        if set(revoke_keys) - allowed or set(revoke_keys) & set(values) or not (values or revoke_keys):
            raise ValueError('偏好修改字段无效')
        source_value = source.model_dump() if isinstance(source, PreferenceSource) else source
        command = [workspace_id, business, expected_revision, values, revoke_keys, source_value,
                   context.session_id if context else None]
        fingerprint = digest(command)
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM agent_preference_requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('偏好请求身份与原参数不同')
                return json.loads(old['response'])
            origin = self._origin(db, workspace_id, source_value, context)
            profile = self._profile(db, workspace_id, business)
            if profile['revision'] != expected_revision:
                raise Conflict('偏好已更新，请刷新后修改')
            revision = expected_revision + 1
            entries = profile['entries']
            for key in revoke_keys:
                entries.pop(key, None)
            for key, value in values.items():
                entries[key] = {'value': value, 'source': origin, 'revision': revision,
                                'business': business, 'workspace_id': workspace_id}
            updated = now()
            db.execute('INSERT OR REPLACE INTO agent_preference_profiles VALUES(?,?,?,?,?)',
                       (workspace_id, business, revision, canonical(entries), updated))
            response = {'workspace_id': workspace_id, 'business': business, 'revision': revision,
                        'entries': entries, 'updated': updated, 'revoked_keys': revoke_keys,
                        'defaults_apply_to_future_runs': True}
            db.execute('INSERT INTO agent_preference_requests VALUES(?,?,?)',
                       (request_id, fingerprint, canonical(response)))
            if context:
                db.execute('INSERT INTO events(session_id,run_id,identity,kind,payload,created) VALUES(?,?,?,?,?,?)',
                    (context.session_id, context.run_id, 'preference:' + request_id,
                     'preferences_updated', canonical(response), updated))
        mark_reader_feature(self.store.root.parent, 'agent-preferences-v1')
        return response

    def freeze(self, workspace_id, session_id, run_id, business, overrides=None):
        """Persist once per existing run/business, replay without reading new defaults."""
        self._business(business)
        overrides = checked_values(overrides or {})
        with self.store.transaction() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['workspace_id'] != workspace_id or run['session_id'] != session_id:
                raise ValueError('偏好快照任务范围无效')
            prior = db.execute('SELECT * FROM agent_preference_snapshots WHERE run_id=? AND business=?',
                               (run_id, business)).fetchone()
            if prior:
                snapshot = json.loads(prior['body'])
                if digest(snapshot) != prior['sha256']:
                    raise Conflict('任务偏好固定快照校验失败')
                if overrides and overrides != snapshot['overrides']:
                    raise Conflict('当前任务偏好已冻结；不同要求需新任务快照')
                return {**snapshot, 'sha256': prior['sha256']}
            if run['status'] not in ('running', 'waiting_job', 'waiting_user'):
                raise RunSuperseded('结束或停止的任务不能创建新偏好快照')
            project = self._profile(db, workspace_id, 'project')
            specific = self._profile(db, workspace_id, business)
            effective = {**project['entries'], **specific['entries']}
            session = db.execute('SELECT selected_assets,initial_refs FROM sessions WHERE id=?', (session_id,)).fetchone()
            snapshot = {'version': 1, 'workspace_id': workspace_id, 'session_id': session_id,
                'run_id': run_id, 'business': business, 'profile_revision': specific['revision'],
                'project_revision': project['revision'], 'sources': effective, 'overrides': overrides,
                'values': {**{key: item['value'] for key, item in effective.items()}, **overrides},
                'scope_sha256': digest([session['selected_assets'], session['initial_refs']]),
                'created': now()}
            sha256 = digest(snapshot)
            db.execute('INSERT INTO agent_preference_snapshots VALUES(?,?,?,?,?,?,?)',
                (run_id, business, workspace_id, session_id, canonical(snapshot), sha256, snapshot['created']))
        mark_reader_feature(self.store.root.parent, 'agent-preferences-v1')
        return {**snapshot, 'sha256': sha256}

    def snapshots(self, workspace_id, session_id, run_id=None):
        session = self.store.session(session_id)
        if session['workspace_id'] != workspace_id:
            raise ValueError('偏好快照会话不属于当前项目')
        parameters = [workspace_id, session_id]
        condition = ' AND run_id=?' if run_id else ''
        if run_id:
            parameters.append(run_id)
        rows = self.store.rows('SELECT * FROM agent_preference_snapshots WHERE workspace_id=? AND session_id=?' +
                               condition + ' ORDER BY created DESC LIMIT 30', parameters)
        result = []
        for row in rows:
            snapshot = json.loads(row['body'])
            if digest(snapshot) != row['sha256']:
                raise Conflict('任务偏好固定快照校验失败')
            result.append({**snapshot, 'sha256': row['sha256']})
        return {'snapshots': result}
