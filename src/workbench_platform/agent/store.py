"""Execution identities and UI projections; model history belongs to Harness."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from workbench_platform.supervisor.processes import require_f_path


from workbench_platform.application_services.identity import now, canonical, digest, Conflict, RunSuperseded


class AgentStore:
    def __init__(self, root: Path):
        self.root = require_f_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'operations.sqlite3'
        self.changed = threading.Event()
        self._watch_guard = threading.Lock()
        self._watchers = {}
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,selected_assets TEXT NOT NULL,created TEXT NOT NULL,
                    request_id TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,status TEXT NOT NULL,generation INTEGER NOT NULL,
                    connection_revision INTEGER NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,
                    request_id TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,error TEXT,
                    rounds INTEGER NOT NULL DEFAULT 0,tool_count INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS active_workspace_run ON runs(workspace_id)
                    WHERE status IN ('running','waiting_job','waiting_user','needs_verification');
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,run_id TEXT,identity TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                    content TEXT NOT NULL,consumed INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,tool TEXT NOT NULL,fingerprint TEXT NOT NULL,
                    arguments TEXT NOT NULL,state TEXT NOT NULL,result TEXT,parent_id TEXT,
                    created TEXT NOT NULL,updated TEXT NOT NULL,UNIQUE(run_id,call_id));
                CREATE TABLE IF NOT EXISTS model_requests(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,
                    response TEXT,created TEXT NOT NULL,UNIQUE(run_id,step));
                CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                    question TEXT NOT NULL,revision INTEGER NOT NULL,reply TEXT,request_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS public_responses(display_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,request_id TEXT NOT NULL,generation INTEGER NOT NULL,
                    source TEXT NOT NULL,call_id TEXT,tool TEXT,revision INTEGER NOT NULL,
                    content TEXT NOT NULL,status TEXT NOT NULL,checkpoint_after INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS derived_inputs(workspace_id TEXT NOT NULL,asset_id TEXT PRIMARY KEY,
                    source_refs TEXT NOT NULL,leaf_assets TEXT NOT NULL,provenance_path TEXT NOT NULL,
                    provenance_hash TEXT NOT NULL,operation_id TEXT NOT NULL);
            ''')
            if 'initial_refs' not in {row[1] for row in db.execute('PRAGMA table_info(sessions)')}:
                db.execute("ALTER TABLE sessions ADD COLUMN initial_refs TEXT NOT NULL DEFAULT '[]'")
            columns = {row[1] for row in db.execute('PRAGMA table_info(runs)')}
            if 'driver' not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN driver TEXT NOT NULL DEFAULT 'harness'")
            if 'driver_state' not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN driver_state TEXT NOT NULL DEFAULT '{}'")
            for name, default in [('round_limit', 20), ('tool_limit', 60)]:
                if name not in columns:
                    db.execute(f'ALTER TABLE runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}')
            db.execute('DROP INDEX IF EXISTS active_workspace_run')
            db.execute("CREATE UNIQUE INDEX active_workspace_run ON runs(workspace_id) WHERE "
                       "driver='harness' AND status IN ('running','waiting_job','waiting_user','needs_verification','paused')")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_session_run ON runs(session_id) WHERE "
                       "status IN ('running','waiting_job','waiting_user','needs_verification','paused')")
            if 'kind' not in {row[1] for row in db.execute('PRAGMA table_info(decisions)')}:
                db.execute("ALTER TABLE decisions ADD COLUMN kind TEXT NOT NULL DEFAULT 'clarification'")
            if 'truncated' not in {row[1] for row in db.execute('PRAGMA table_info(public_responses)')}:
                db.execute('ALTER TABLE public_responses ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0')

    def watch(self, session_id):
        """One independent wakeup per SSE subscriber, owned by its running loop."""
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._watch_guard:
            self._watchers[event] = (session_id, loop)
        return event

    def unwatch(self, event):
        with self._watch_guard:
            self._watchers.pop(event, None)

    def _signal(self, session_id=None):
        """Call after commit from any thread; SQLite seq is the event authority."""
        self.changed.set()
        with self._watch_guard:
            watchers = [(event, loop) for event, (session, loop) in self._watchers.items()
                        if session_id is None or session == session_id]
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for event, loop in watchers:
            try:
                if loop is current_loop:
                    event.set()
                else:
                    loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # A disconnected subscriber may close its loop between the
                # snapshot and callback. It must not break the committing writer.
                self.unwatch(event)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=15000')
        return db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    def rows(self, query, parameters=()):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(query, parameters)]

    def event(self, session_id, run_id, identity, kind, payload):
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO events(session_id,run_id,identity,kind,payload,created) VALUES(?,?,?,?,?,?)',
                       (session_id, run_id, identity, kind, canonical(payload), now()))
        self._signal(session_id)

    @staticmethod
    def _public_run(db, run_id, generation, *, terminal=False):
        run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['generation'] != generation or run['status'] == 'cancelled':
            raise RunSuperseded('运行已停止，公开事件未写入')
        if not terminal and run['status'] not in {'running', 'waiting_job', 'waiting_user'}:
            raise RunSuperseded('运行已结束，公开事件未写入')
        return run

    @staticmethod
    def _public_insert(db, run, identity, kind, payload):
        if db.execute('SELECT 1 FROM events WHERE identity=?', (identity,)).fetchone():
            # INSERT OR IGNORE still advances AUTOINCREMENT on a duplicate,
            # turning repeated activity/progress observations into disk writes.
            return
        db.execute('INSERT OR IGNORE INTO events(session_id,run_id,identity,kind,payload,created) VALUES(?,?,?,?,?,?)',
            (run['session_id'], run['id'], identity, kind,
             canonical({**payload, 'version': 1, 'generation': run['generation']}), now()))

    def public_event(self, run_id, generation, identity, kind, payload):
        with self.transaction() as db:
            run = self._public_run(db, run_id, generation,
                terminal=(kind in {'context_compaction', 'engine_ready', 'result_available'} or
                          kind == 'tool_activity' and payload.get('phase') == 'restored' or
                          kind == 'agent_activity' and payload.get('phase') in
                          {'completed', 'failed', 'needs_verification', 'paused', 'compacting'}))
            self._public_insert(db, run, identity, kind, payload)
        self._signal(run['session_id'])

    def preview_response(self, run_id, generation, request_id, frame):
        """Persist a public copy; append frames and exponential checkpoints stay linear."""
        display_id = request_id + ':' + frame['block']
        content = frame['content']
        if not isinstance(content, str) or len(content) > 256 * 1024:
            raise ValueError('公开响应超过显示容量')
        with self.transaction() as db:
            run = self._public_run(db, run_id, generation)
            request = db.execute('SELECT state,run_id FROM model_requests WHERE id=?', (request_id,)).fetchone()
            if not request or request['run_id'] != run_id or request['state'] != 'sent':
                raise RunSuperseded('请求已结束，公开增量未写入')
            old = db.execute('SELECT * FROM public_responses WHERE display_id=?', (display_id,)).fetchone()
            if old and old['status'] != 'generating':
                return display_id
            previous = old['content'] if old else ''
            truncated = bool(frame.get('truncated', False))
            if old and previous == content and old['call_id'] == frame.get('call_id') and old['truncated'] == truncated:
                return display_id
            revision = old['revision'] + 1 if old else 1
            checkpoint = old['checkpoint_after'] if old else 1024
            if not old or len(content) >= checkpoint or not content.startswith(previous):
                mode, offset, emitted = 'snapshot', 0, content
                while checkpoint <= len(content):
                    checkpoint *= 2
            else:
                mode, offset, emitted = 'append', len(previous), content[len(previous):]
            db.execute('INSERT OR REPLACE INTO public_responses VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (display_id, run_id, request_id, generation, frame['source'], frame.get('call_id'),
                 frame.get('tool'), revision, content, 'generating', checkpoint, truncated))
            self._public_insert(db, run, display_id + ':preview:' + str(revision), 'response_preview',
                {'request_id': request_id, 'display_id': display_id, 'revision': revision,
                 'mode': mode, 'offset': offset, 'content': emitted, 'source': frame['source'],
                 'provisional': True, 'truncated': bool(frame.get('truncated', False)),
                 'call_id': frame.get('call_id'), 'tool': frame.get('tool')})
        self._signal(run['session_id'])
        return display_id

    def response_state(self, run_id, generation, request_id, display_id, status, *,
                       content=None, source='answer', call_id=None, tool=None, references=(), reason=''):
        with self.transaction() as db:
            run = self._public_run(db, run_id, generation)
            return_value = self._response_state(db, run, request_id, display_id, status,
                content=content, source=source, call_id=call_id, tool=tool,
                references=references, reason=reason)
        self._signal(run['session_id'])
        return return_value

    def _response_state(self, db, run, request_id, display_id, status, *, content=None,
                        source='answer', call_id=None, tool=None, references=(), reason=''):
        old = db.execute('SELECT * FROM public_responses WHERE display_id=?', (display_id,)).fetchone()
        if old and old['status'] in {'confirmed', 'interrupted'}:
            return display_id
        if old and old['status'] == status and (content is None or old['content'] == content):
            return display_id
        revision = old['revision'] + 1 if old else 1
        content = content if content is not None else old['content'] if old else ''
        if not isinstance(content, str) or len(content) > 256 * 1024:
            raise ValueError('公开响应超过显示容量')
        source = old['source'] if old else source
        db.execute('INSERT OR REPLACE INTO public_responses VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (display_id, run['id'], request_id, run['generation'], source,
             call_id or (old['call_id'] if old else None), tool or (old['tool'] if old else None),
             revision, content, status, old['checkpoint_after'] if old else 1024, 0))
        self._public_insert(db, run, display_id + ':state:' + status, 'response_state',
            {'request_id': request_id, 'display_id': display_id, 'revision': revision,
             'status': status, 'source': source, 'content': content,
             'references': list(references), 'reason': reason})
        return display_id

    def finish_public(self, run_id, status, reason='', *, generation=None):
        """Terminal projection uses the current fence, including cancelled new generations."""
        with self.transaction() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or (generation is not None and run['generation'] != generation):
                return
            rows = db.execute("SELECT * FROM public_responses WHERE run_id=? AND status NOT IN ('confirmed','interrupted')", (run_id,)).fetchall()
            for row in rows:
                if status in {'answered', 'succeeded', 'partial', 'incomplete'} and row['source'] == 'text' and row['status'] == 'generated':
                    continue
                self._response_state(db, run, row['request_id'], row['display_id'], 'interrupted',
                    reason=reason or '本轮生成已结束，未确认的候选内容保留')
            phase = ('stopped' if status == 'cancelled' else 'needs_verification' if status == 'needs_verification'
                     else 'failed' if status == 'failed' else 'paused' if status == 'paused' else 'completed')
            self._public_insert(db, run, run_id + ':public-terminal:' + str(run['generation']) + ':' + status,
                'agent_activity', {'phase': phase, 'status': status, 'reason': reason})
        self._signal(run['session_id'])

    def events(self, session_id, after=0, limit=50):
        rows = self.rows('SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?',
                         (session_id, after, limit))
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return {'items': rows, 'next_cursor': rows[-1]['seq'] if rows else after}

    def session(self, session_id):
        rows = self.rows('SELECT * FROM sessions WHERE id=?', (session_id,))
        if not rows:
            raise ValueError('会话不存在')
        value = rows[0]
        value['selected_assets'] = json.loads(value['selected_assets'])
        value['initial_refs'] = json.loads(value['initial_refs'])
        return value

    def create_session(self, workspace_id, name, selected_assets, request_id, initial_refs=None, authorized_reference_ids=None, delivery_policy='auto'):
        initial_refs = initial_refs or []
        if delivery_policy not in {'auto', 'draft', 'reviewed'}:
            raise ValueError('交付策略无效')
        fingerprint = digest([workspace_id, name, selected_assets, initial_refs] if initial_refs else [workspace_id, name, selected_assets])
        if authorized_reference_ids:
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent, 'agent-multimodal-v1')
            fingerprint = digest([fingerprint, sorted(set(authorized_reference_ids))])
        if delivery_policy != 'auto':
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent, 'agent-multimodal-v1')
            fingerprint = digest([fingerprint, {'delivery_policy': delivery_policy}])
        with self.transaction() as db:
            old = db.execute('SELECT * FROM sessions WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('请求身份与原会话参数不同')
                identifier = old['id']
            else:
                identifier = 's' + uuid.uuid4().hex
                db.execute('INSERT INTO sessions(id,workspace_id,name,selected_assets,created,request_id,fingerprint,initial_refs) VALUES(?,?,?,?,?,?,?,?)',
                           (identifier, workspace_id, name, canonical(selected_assets), now(), request_id, fingerprint, canonical(initial_refs)))
                if authorized_reference_ids or delivery_policy != 'auto':
                    body = {'title': name, 'goal': '', 'scope_revision': 1, 'plan': [], 'plan_revision': 0,
                            'manual': False, 'pending_scope': None,
                            'authorized_reference_ids': sorted(set(authorized_reference_ids or [])),
                            'delivery_policy': delivery_policy}
                    db.execute('INSERT INTO audit_tasks VALUES(?,1,?,?)', (identifier, canonical(body), now()))
        return self.session(identifier)

    def run(self, run_id):
        rows = self.rows('SELECT * FROM runs WHERE id=?', (run_id,))
        if not rows:
            raise ValueError('运行不存在')
        value = rows[0]
        for name in ('round_limit', 'tool_limit'):
            if value.get(name) == 0:
                value[name] = None
        return value

    def start_run(self, session_id, content, request_id, connection_revision, round_limit=None, tool_limit=None,
                  *, driver='harness', driver_state=None):
        if driver not in {'harness', 'image-text-v1', 'pdf-text-v1', 'data-action-v1', 'classification-predict-v1'}:
            raise ValueError('未知执行驱动')
        session = self.session(session_id)
        fingerprint = digest([session_id, content] if driver == 'harness' else [session_id, content, driver, driver_state])
        if driver != 'harness':
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent, 'direct-' + driver, 'direct-resource-claims-v1')
        elif (driver_state or {}).get('scope_claims'):
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent, 'harness-native-scope-v1')
        with self.transaction() as db:
            old = db.execute('SELECT * FROM runs WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('请求身份与原目标不同')
                return dict(old), False
            identifier = 'r' + uuid.uuid4().hex
            from workbench_platform.application_services.direct_scope import conflicts
            active = db.execute("SELECT * FROM runs WHERE status IN "
                "('running','waiting_job','waiting_user','needs_verification','paused')").fetchall()
            claimed = {**(driver_state or {}), 'scope_workspace_id': session['workspace_id']}
            claimed = json.loads(self._resource_claims({'driver': driver, 'workspace_id': session['workspace_id'],
                'session_id': session_id, 'driver_state': canonical(claimed)})['driver_state'])
            for row in active:
                other = self._resource_claims(dict(row))
                if row['session_id'] == session_id or conflicts(driver, claimed, other):
                    raise Conflict('本会话或同一业务对象已有未结束操作（关联任务 ' + row['id'] + '）；请继续原任务或等待冲突解除')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_tasks'").fetchone():
                task = db.execute('SELECT body FROM audit_tasks WHERE session_id=?', (session_id,)).fetchone()
                if task and json.loads(task['body'])['manual']:
                    raise Conflict('任务正在人工处理，请先返回智能审计')
            try:
                db.execute('INSERT INTO runs(id,session_id,workspace_id,status,generation,connection_revision,'
                           'created,updated,request_id,fingerprint,driver,driver_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier, session_id, session['workspace_id'], 'running', 1,
                            connection_revision, now(), now(), request_id, fingerprint, driver, canonical(claimed)))
                db.execute('INSERT INTO events(session_id,run_id,identity,kind,payload,created) VALUES(?,?,?,?,?,?)',
                           (session_id, identifier, request_id, 'message', canonical({'role': 'user', 'content': content}), now()))
                db.execute('UPDATE runs SET round_limit=?,tool_limit=? WHERE id=?', (round_limit or 0, tool_limit or 0, identifier))
            except sqlite3.IntegrityError:
                raise Conflict('当前项目已有 Agent 运行；可在原会话追加目标或停止') from None
        self._signal(session_id)
        return self.run(identifier), True

    def _resource_claims(self, run):
        from workbench_platform.application_services.direct_scope import merge_scope, FAMILIES
        state = json.loads(run.get('driver_state') or '{}')
        resolver = getattr(self, 'scope_resolver', None)
        if run['driver'] == 'harness' and resolver:
            state['scope_workspace_id'] = run['workspace_id']
            state['scope_claims'] = merge_scope(state.get('scope_claims'), resolver(run))
        elif resolver and run['driver'] in FAMILIES and FAMILIES[run['driver']] != 'data':
            state['scope_native_projects'] = resolver(run).get('projects', {}).get(FAMILIES[run['driver']], [])
        return {**run, 'driver_state': canonical(state)}

    def claim_harness_scope(self, run_id, generation):
        return self.claim_run_scope(run_id, generation)

    def claim_run_scope(self, run_id, generation):
        """Refresh actual accessible objects atomically before a native write."""
        from workbench_platform.application_services.direct_scope import conflicts
        from workbench_platform.application_services.errors import ResourceBusy
        with self.transaction() as db:
            run = self._public_run(db, run_id, generation)
            run = self._resource_claims(dict(run))
            state = json.loads(run['driver_state'])
            for row in db.execute("SELECT * FROM runs WHERE id!=? AND status IN "
                    "('running','waiting_job','waiting_user','needs_verification','paused')", (run_id,)):
                other = self._resource_claims(dict(row))
                if conflicts(run['driver'], state, other):
                    raise ResourceBusy('同一业务对象已有未结束操作（关联任务 ' + row['id'] + '）；本次写入尚未提交')
            db.execute('UPDATE runs SET driver_state=? WHERE id=? AND generation=?',
                       (canonical(state), run_id, generation))

    def save_driver_state(self, run_id, generation, state):
        with self.transaction() as db:
            self._public_run(db, run_id, generation)
            db.execute('UPDATE runs SET driver_state=?,updated=? WHERE id=?', (canonical(state), now(), run_id))

    def resume_harness_run(self, run_id, generation):
        """Reacquire current resource claims and activate in one transaction."""
        from workbench_platform.application_services.direct_scope import conflicts
        with self.transaction() as db:
            run = self._resource_claims(dict(self._public_run(db, run_id, generation, terminal=True)))
            if run['driver'] != 'harness' or run['status'] not in {'paused', 'needs_verification', 'failed', 'partial'}:
                raise Conflict('此运行当前不可继续')
            claims = json.loads(run['driver_state'])
            for row in db.execute("SELECT * FROM runs WHERE id!=? AND status IN "
                    "('running','waiting_job','waiting_user','needs_verification','paused')", (run_id,)):
                other = self._resource_claims(dict(row))
                if row['session_id'] == run['session_id'] or conflicts('harness', claims, other):
                    raise Conflict('本会话或同一业务对象已有未结束操作（关联任务 ' + row['id'] + '）；原成果保留，冲突解除后继续')
            db.execute("UPDATE runs SET driver_state=?,status='running',error=NULL,updated=? WHERE id=? AND generation=?",
                       (canonical(claims), now(), run_id, generation))

    def status(self, run_id, generation, status, error=None):
        with self.transaction() as db:
            changed = db.execute('UPDATE runs SET status=?,error=?,updated=? WHERE id=? AND generation=? '
                                 "AND status!='cancelled'", (status, error, now(), run_id, generation)).rowcount
            if not changed:
                raise RunSuperseded('运行已停止或状态已改变')

    def assert_current(self, run_id, generation):
        run = self.run(run_id)
        if run['generation'] != generation or run['status'] == 'cancelled':
            raise RunSuperseded('运行已停止')
        return run

    @staticmethod
    def cancel_in_transaction(db, *, run_id=None, session_id=None):
        """Fence active runs inside the caller's transaction; signal after commit.

        Task takeover can update manual/scope/revision and the fence atomically.
        Return only changed runs, so retries and terminal runs do not acquire a
        new generation or a misleading stopped projection.
        """
        if (run_id is None) == (session_id is None):
            raise ValueError('指定一个运行或会话身份')
        column, identifier = ('id', run_id) if run_id is not None else ('session_id', session_id)
        active = db.execute(f'SELECT * FROM runs WHERE {column}=? AND '
            "(status IN ('running','waiting_job','waiting_user','needs_verification','paused') "
            "OR (status='partial' AND driver='harness'))", (identifier,)).fetchall()
        changed = []
        for row in active:
            updated = now()
            db.execute("UPDATE runs SET status='cancelled',generation=generation+1,updated=? WHERE id=?",
                       (updated, row['id']))
            changed.append({**dict(row), 'status': 'cancelled',
                            'generation': row['generation'] + 1, 'updated': updated})
        return changed

    def begin_operation(self, run_id, generation, call, parent_id=None):
        self.assert_current(run_id, generation)
        fingerprint = digest([call['tool'], call['arguments']])
        identifier = 'o' + digest([run_id, call['call_id']])[:40]
        with self.transaction() as db:
            # Fencing and operation intent commit in the same transaction.
            current = db.execute('SELECT generation,status FROM runs WHERE id=?', (run_id,)).fetchone()
            if current['generation'] != generation or current['status'] == 'cancelled':
                raise RunSuperseded('运行已停止')
            old = db.execute('SELECT * FROM operations WHERE id=?', (identifier,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('调用身份与原参数不同')
                return dict(old), False
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, run_id, call['call_id'], call['tool'], fingerprint,
                        canonical(call['arguments']), 'intent', None, parent_id, now(), now(),))
        return self.rows('SELECT * FROM operations WHERE id=?', (identifier,))[0], True

    def finish_operation(self, operation_id, result):
        with self.transaction() as db:
            db.execute('UPDATE operations SET state=?,result=?,updated=? WHERE id=?',
                       ('pending' if result['status'] == 'pending' else 'finished', canonical(result), now(), operation_id))

    def model_record(self, run_id, generation, step):
        """Read a request identity behind the same run fence used by writes."""
        with self.transaction() as db:
            run = db.execute('SELECT generation,status FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['generation'] != generation or run['status'] == 'cancelled':
                raise RunSuperseded('运行已停止')
            row = db.execute('SELECT * FROM model_requests WHERE run_id=? AND step=?', (run_id, step)).fetchone()
            return dict(row) if row else None

    def begin_model(self, run_id, generation, step, fingerprint):
        with self.transaction() as db:
            row = db.execute('SELECT generation,status FROM runs WHERE id=?', (run_id,)).fetchone()
            if row['generation'] != generation or row['status'] == 'cancelled':
                raise RunSuperseded('运行已停止')
            old = db.execute('SELECT * FROM model_requests WHERE run_id=? AND step=?', (run_id, step)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('模型请求与原上下文不同')
                return dict(old), False
            identifier = 'm' + uuid.uuid4().hex
            db.execute('INSERT INTO model_requests VALUES(?,?,?,?,?,?,?)',
                       (identifier, run_id, step, fingerprint, 'sent', None, now()))
        return self.rows('SELECT * FROM model_requests WHERE id=?', (identifier,))[0], True

    def finish_model(self, identifier, response):
        with self.transaction() as db:
            db.execute("UPDATE model_requests SET state='received',response=? WHERE id=?",
                       (canonical(response), identifier))
