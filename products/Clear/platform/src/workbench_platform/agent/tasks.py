"""Audit product projections over the existing Agent execution ledger.

One task belongs to one conversation. Native jobs and Harness retain execution
authority; these objects record business context, plans and human collaboration.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json

from .store import Conflict, canonical, digest, now


class AuditTasks:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS audit_tasks(session_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,body TEXT NOT NULL,updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_project_profiles(workspace_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,body TEXT NOT NULL,updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_product_requests(request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_policy_user_inputs(session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(session_id,request_id));
            ''')

    def _body(self, db, session_id):
        session = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not session:
            raise ValueError('审计任务不存在')
        row = db.execute('SELECT * FROM audit_tasks WHERE session_id=?', (session_id,)).fetchone()
        if not row:
            body = {'title': session['name'], 'goal': '', 'scope_revision': 1,
                    'plan': [], 'plan_revision': 0, 'manual': False, 'pending_scope': None, 'authorized_reference_ids': [], 'delivery_policy': 'auto', 'delivery_policy_version': 2}
            db.execute('INSERT INTO audit_tasks VALUES(?,1,?,?)', (session_id, canonical(body), now()))
            return body, 1
        body = json.loads(row['body'])
        body.setdefault('authorized_reference_ids', [])
        body.setdefault('delivery_policy', 'auto')
        return body, row['revision']

    def _save(self, db, session_id, body, revision):
        from .delivery_policy import effective_policy
        if body.get('delivery_policy', 'auto') != 'auto' or body.get('delivery_policy_inference') or effective_policy(body)['require_review']:
            from workbench_platform.resources.reader_features import require_features
            require_features(self.store.root.parent, 'agent-multimodal-v1')
        db.execute('UPDATE audit_tasks SET body=?,revision=?,updated=? WHERE session_id=?',
                   (canonical(body), revision + 1, now(), session_id))

    def _replay(self, db, request_id, payload):
        row = db.execute('SELECT * FROM audit_product_requests WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if row['fingerprint'] != digest(payload):
                raise Conflict('此请求已绑定另一项操作')
            return json.loads(row['response'])

    def _receipt(self, db, request_id, payload, response):
        db.execute('INSERT INTO audit_product_requests VALUES(?,?,?)',
                   (request_id, digest(payload), canonical(response)))
        return response

    def get(self, session_id):
        session = self.store.session(session_id)
        with self.store.transaction() as db:
            body, revision = self._body(db, session_id)
        runs = self.store.rows('SELECT id,status,error,created,updated FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 20',
                               (session_id,))
        operations = self.store.rows('SELECT o.id,o.tool,o.result,o.updated FROM operations o JOIN runs r ON r.id=o.run_id '
                                    'WHERE r.session_id=? AND o.result IS NOT NULL ORDER BY o.created DESC LIMIT 100', (session_id,))
        references, artifacts, jobs = {}, {}, {}
        for operation in reversed(operations):
            result = json.loads(operation['result'])
            for field, target in [('result_refs', references), ('artifact_refs', artifacts), ('job_refs', jobs)]:
                for ref in result.get(field, []):
                    target[canonical(ref)] = ref
        status = 'manual' if body['manual'] else runs[0]['status'] if runs else 'draft'
        from .delivery_policy import effective_policy
        return {**body, 'delivery_policy_resolution': effective_policy(body), 'id': session_id, 'session_id': session_id, 'workspace_id': session['workspace_id'],
                'revision': revision, 'status': status, 'created': session['created'],
                'selected_assets': session['selected_assets'], 'initial_refs': session['initial_refs'],
                'runs': runs, 'result_refs': list(references.values()), 'artifact_refs': list(artifacts.values()),
                'job_refs': list(jobs.values())}

    def list(self, workspace_id, keyword='', limit=50):
        rows = self.store.rows('SELECT id FROM sessions WHERE workspace_id=? AND name LIKE ? '
                               'ORDER BY created DESC LIMIT ?', (workspace_id, '%' + keyword + '%', limit))
        return {'items': [self.get(row['id']) for row in rows]}

    def record_goal(self, session_id, content, request_id=None):
        """Persist only trusted user goals/messages, never tool-generated summaries."""
        from .delivery_policy import infer_user_delivery_policy
        with self.store.transaction() as db:
            content_hash = digest(content)
            if request_id:
                old = db.execute('SELECT content_hash FROM audit_policy_user_inputs WHERE session_id=? AND request_id=?',
                                 (session_id, request_id)).fetchone()
                if old:
                    if old['content_hash'] != content_hash:
                        raise Conflict('用户消息身份不能替换原交付要求')
                    return
            body, revision = self._body(db, session_id)
            event = db.execute("SELECT created FROM events WHERE session_id=? AND identity=? AND kind='message'",
                               (session_id, request_id)).fetchone() if request_id else None
            received_at = event['created'] if event else now()
            previous_at = body.get('delivery_policy_user_input_at')
            if request_id:
                from workbench_platform.resources.reader_features import require_features
                require_features(self.store.root.parent, 'agent-multimodal-v1')
                db.execute('INSERT INTO audit_policy_user_inputs VALUES(?,?,?)', (session_id, request_id, content_hash))
            if previous_at and datetime.fromisoformat(received_at.replace('Z', '+00:00')) < datetime.fromisoformat(previous_at.replace('Z', '+00:00')):
                return
            from .delivery_policy import adopt_explicit_policy
            changed = adopt_explicit_policy(body)
            inference = infer_user_delivery_policy(content, source='user_goal' if not body['goal'] or body['goal'] == content else 'user_message', request_id=request_id)
            if inference and inference != body.get('delivery_policy_inference'):
                from workbench_platform.resources.reader_features import require_features
                require_features(self.store.root.parent, 'agent-multimodal-v1')
                body['delivery_policy_inference'] = inference
                changed = True
            if not body['goal']:
                body['goal'] = content
                changed = True
            if request_id and received_at != previous_at:
                body['delivery_policy_user_input_at'] = received_at
                changed = True
            if changed:
                self._save(db, session_id, body, revision)

    def update(self, session_id, payload):
        from .delivery_policy import POLICIES, infer_user_delivery_policy
        if payload.get('delivery_policy') is not None and payload['delivery_policy'] not in POLICIES:
            raise ValueError('交付策略无效')
        inference = infer_user_delivery_policy(payload['goal'], request_id=payload['request_id']) if payload.get('goal') is not None else None
        if payload.get('delivery_policy', 'auto') != 'auto' or inference:
            from workbench_platform.resources.reader_features import require_features
            require_features(self.store.root.parent, 'agent-multimodal-v1')
        if 'authorized_reference_ids' in payload:
            from workbench_platform.resources.reader_features import require_features
            require_features(self.store.root.parent, 'agent-multimodal-v1')
        command = {'session_id': session_id, **payload}
        with self.store.transaction() as db:
            old = self._replay(db, payload['request_id'], command)
            if old is not None:
                return old
            body, revision = self._body(db, session_id)
            if revision != payload['expected_revision']:
                raise Conflict('任务已经更新，请刷新后再修改')
            if payload.get('name') is not None:
                body['title'] = payload['name']
                db.execute('UPDATE sessions SET name=? WHERE id=?', (payload['name'], session_id))
            if payload.get('goal') is not None:
                from .delivery_policy import adopt_explicit_policy
                adopt_explicit_policy(body)
                body['goal'] = payload['goal']
                if inference:
                    body['delivery_policy_inference'] = inference
            if payload.get('delivery_policy') is not None:
                body['delivery_policy'] = payload['delivery_policy']
            if payload.get('goal') is not None or payload.get('delivery_policy') is not None:
                body['delivery_policy_user_input_at'] = now()
            session = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if any(payload.get(key) is not None for key in ('selected_assets', 'initial_refs', 'authorized_reference_ids')):
                base = {'selected_assets': json.loads(session['selected_assets']), 'initial_refs': json.loads(session['initial_refs']),
                        'authorized_reference_ids': body.get('authorized_reference_ids', []), **(body.get('pending_scope') or {})}
                scope = {key: payload[key] if payload.get(key) is not None else base[key] for key in base}
                accepted = db.execute("SELECT driver FROM runs WHERE session_id=? AND status IN "
                    "('running','waiting_job','waiting_user','paused','needs_verification')", (session_id,)).fetchone()
                if accepted and accepted['driver'] != 'harness' and scope != base:
                    raise Conflict('当前操作已固定原件与范围。继续原任务可保留当前处理；如需增加或缩小资料范围，'
                                   '请先“停止后续步骤”，再在本任务保存新范围并发送修订要求；原回执和旧成果保留。')
                run = db.execute("SELECT id FROM runs WHERE session_id=? AND status IN ('running','waiting_job','waiting_user')", (session_id,)).fetchone()
                if run:
                    body['pending_scope'] = scope
                    db.execute('INSERT INTO inbox(id,run_id,content,created) VALUES(?,?,?,?)',
                        (payload['request_id'], run['id'], '用户更新了本任务的资料范围；在下一步前读取服务器最新范围。', now()))
                else:
                    body['scope_revision'] += 1
                    body['pending_scope'] = None
                    body['authorized_reference_ids'] = scope['authorized_reference_ids']
                    db.execute('UPDATE sessions SET selected_assets=?,initial_refs=? WHERE id=?',
                               (canonical(scope['selected_assets']), canonical(scope['initial_refs']), session_id))
            self._save(db, session_id, body, revision)
            return self._receipt(db, payload['request_id'], command,
                                 {'revision': revision + 1, 'queued': body['pending_scope'] is not None})

    def apply_scope(self, session_id):
        with self.store.transaction() as db:
            body, revision = self._body(db, session_id)
            scope = body['pending_scope']
            if scope is None:
                return None
            db.execute('UPDATE sessions SET selected_assets=?,initial_refs=? WHERE id=?',
                       (canonical(scope['selected_assets']), canonical(scope['initial_refs']), session_id))
            body['scope_revision'] += 1
            body['authorized_reference_ids'] = scope.get('authorized_reference_ids', body.get('authorized_reference_ids', []))
            body['pending_scope'] = None
            self._save(db, session_id, body, revision)
            return {**scope, 'scope_revision': body['scope_revision']}

    def control(self, session_id, payload):
        command = {'session_id': session_id, **payload}
        # Stop before changing the business task. Native jobs retain their IDs
        # and may finish; a new turn reads those receipts rather than resubmits.
        with self.store.transaction() as db:
            old = self._replay(db, payload['request_id'], command)
            if old is not None:
                return old
            body, revision = self._body(db, session_id)
            if revision != payload['expected_revision']:
                raise Conflict('任务已经更新，请刷新后再操作')
            body['manual'] = payload['action'] == 'take_over'
            cancelled = []
            if body['manual']:
                if body['pending_scope']:
                    scope = body['pending_scope']
                    db.execute('UPDATE sessions SET selected_assets=?,initial_refs=? WHERE id=?',
                               (canonical(scope['selected_assets']), canonical(scope['initial_refs']), session_id))
                    body['scope_revision'] += 1
                    body['authorized_reference_ids'] = scope.get('authorized_reference_ids', body.get('authorized_reference_ids', []))
                    body['pending_scope'] = None
                cancelled = self.store.cancel_in_transaction(db, session_id=session_id)
            self._save(db, session_id, body, revision)
            result = self._receipt(db, payload['request_id'], command,
                                   {'revision': revision + 1, 'manual': body['manual']})
        self.store.event(session_id, None, payload['request_id'], 'task_control', result)
        for run in cancelled:
            self.store.finish_public(run['id'], 'cancelled', '已切换人工处理；原生作业与已有结果保留',
                                     generation=run['generation'])
        return result

    def set_plan(self, session_id, items, operation_id, run_id=None, generation=None):
        with self.store.transaction() as db:
            if run_id:
                run = db.execute('SELECT generation,status FROM runs WHERE id=?', (run_id,)).fetchone()
                if not run or run['generation'] != generation or run['status'] == 'cancelled':
                    from .store import RunSuperseded
                    raise RunSuperseded('运行已停止，不能更新安排')
            old = self._replay(db, operation_id, {'session_id': session_id, 'plan': items})
            if old is not None:
                return old
            body, revision = self._body(db, session_id)
            body['plan'] = items
            body['plan_revision'] += 1
            self._save(db, session_id, body, revision)
            return self._receipt(db, operation_id, {'session_id': session_id, 'plan': items},
                                 {'plan': items, 'plan_revision': body['plan_revision']})

    def return_results(self, session_id, payload):
        command = {'session_id': session_id, **payload}
        if not command.get('include_asset_ids'):
            command.pop('include_asset_ids', None)
        with self.store.transaction() as db:
            old = self._replay(db, payload['request_id'], command)
            if old is not None:
                return old
            body, revision = self._body(db, session_id)
            if revision != payload['expected_revision'] or not body['manual']:
                raise Conflict('请先接管任务并读取当前版本')
            session = db.execute('SELECT initial_refs,selected_assets,workspace_id FROM sessions WHERE id=?', (session_id,)).fetchone()
            included = payload.get('include_asset_ids', [])
            selected = json.loads(session['selected_assets'])
            for identifier in included:
                self.runtime.services.asset_library[0].get(session['workspace_id'], identifier)
            if selected:
                selected = list(dict.fromkeys([*selected, *included]))
                if len(selected) > 100:
                    raise ValueError('资料范围超过100份，请收窄带回的结果')
            refs = {canonical(ref): ref for ref in json.loads(session['initial_refs']) + payload['references']}
            if len(refs) > 100:
                raise ValueError('任务引用过多，请收窄本次带回结果')
            db.execute('UPDATE sessions SET initial_refs=?,selected_assets=? WHERE id=?',
                       (canonical(list(refs.values())), canonical(selected), session_id))
            body['manual'] = False
            from .delivery_policy import infer_user_delivery_policy
            inference = infer_user_delivery_policy(payload['summary'], source='user_manual_return', request_id=payload['request_id'])
            if inference:
                body['delivery_policy_inference'] = inference
            body['delivery_policy_user_input_at'] = now()
            body['scope_revision'] += 1
            body['last_manual_change'] = {'summary': payload['summary'], 'references': payload['references'],
                                          'included_asset_ids': included, 'created_at': now()}
            self._save(db, session_id, body, revision)
            self._receipt(db, payload['request_id'], command, {'revision': revision + 1, 'manual': False})
            db.execute('INSERT INTO events(session_id,run_id,identity,kind,payload,created) VALUES(?,NULL,?,?,?,?)',
                (session_id, payload['request_id'] + ':manual', 'message', canonical({'role': 'user', 'content': '人工处理结果：' + payload['summary'], 'references': payload['references']}), now()))
        return {'revision': revision + 1, 'manual': False}

    def profile(self, workspace_id):
        workspace = self.runtime.services.workspace_store.get_workspace(workspace_id)
        if workspace is None:
            raise ValueError('项目不存在')
        rows = self.store.rows('SELECT * FROM audit_project_profiles WHERE workspace_id=?', (workspace_id,))
        body = json.loads(rows[0]['body']) if rows else {'audit_object': '', 'period_start': '', 'period_end': '', 'objective': '', 'rules': ''}
        return {**body, 'workspace': asdict(workspace), 'revision': rows[0]['revision'] if rows else 0}

    def save_profile(self, workspace_id, payload):
        self.profile(workspace_id)
        command = {'workspace_id': workspace_id, **payload}
        with self.store.transaction() as db:
            old = self._replay(db, payload['request_id'], command)
            if old is not None:
                return old
            row = db.execute('SELECT revision FROM audit_project_profiles WHERE workspace_id=?', (workspace_id,)).fetchone()
            revision = row['revision'] if row else 0
            if revision != payload['expected_revision']:
                raise Conflict('项目背景已更新，请刷新')
            body = {key: value for key, value in payload.items() if key not in {'request_id', 'expected_revision'}}
            db.execute('INSERT OR REPLACE INTO audit_project_profiles VALUES(?,?,?,?)',
                       (workspace_id, revision + 1, canonical(body), now()))
            return self._receipt(db, payload['request_id'], command, {**body, 'revision': revision + 1})
