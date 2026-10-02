from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from workbench_platform.data_api.service import DataError


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class BatchStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'batches.sqlite3'
        self.lock = threading.RLock()
        with closing(self.db()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS templates(template_id TEXT,version INTEGER,
                    workspace_id TEXT,body TEXT,PRIMARY KEY(template_id,version));
                CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY,request_key TEXT UNIQUE,
                    workspace_id TEXT,fingerprint TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS disposition_events(event_id TEXT PRIMARY KEY,
                    batch_id TEXT,item_id TEXT,action TEXT,note TEXT,created_at TEXT);
            ''')

    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def save_template(self, value):
        template_id = value.get('template_id') or 't' + uuid.uuid4().hex
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT workspace_id,max(version) AS version FROM templates '
                                  'WHERE template_id=?', (template_id,)).fetchone()
            if previous['version'] and previous['workspace_id'] != value['workspace_id']:
                raise DataError('模板属于其他工作空间', 409)
            body = {**value, 'template_id': template_id, 'version': (previous['version'] or 0) + 1,
                    'config_hash': fingerprint({'scenario': value['scenario'], 'config': value['config']}),
                    'created_at': now()}
            db.execute('INSERT INTO templates VALUES(?,?,?,?)',
                       (template_id, body['version'], value['workspace_id'], encoded(body)))
        return body

    def template(self, workspace_id, template_id, version):
        with closing(self.db()) as db:
            row = db.execute('SELECT body FROM templates WHERE template_id=? AND version=? AND workspace_id=?',
                             (template_id, version, workspace_id)).fetchone()
        if not row:
            raise DataError('模板版本不存在', 404)
        return json.loads(row['body'])

    def templates(self, workspace_id):
        with closing(self.db()) as db:
            return [json.loads(row['body']) for row in db.execute(
                'SELECT t.body FROM templates t WHERE workspace_id=? AND version='
                '(SELECT max(version) FROM templates p WHERE p.template_id=t.template_id) '
                'ORDER BY rowid DESC', (workspace_id,))]

    def accept(self, request, template):
        digest = fingerprint(request)
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM batches WHERE request_key=?', (request['request_key'],)).fetchone()
            if existing:
                if existing['fingerprint'] != digest:
                    raise DataError('相同运行请求标识不能用于不同资料或模板', 409)
                return json.loads(existing['body']), False
            body = {**request, 'batch_id': 'b' + uuid.uuid4().hex, 'template': template,
                    'status': 'queued', 'stage': 'preflight', 'summary': {}, 'issues': [],
                    'receipts': {}, 'artifact_state': 'not_generated', 'artifacts': [],
                    'capabilities': ['cancel'], 'cancel_requested': False,
                    'created_at': now(), 'updated_at': now()}
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?)', (body['batch_id'], request['request_key'],
                       request['workspace_id'], digest, encoded(body)))
        return body, True

    def get(self, batch_id):
        with closing(self.db()) as db:
            row = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not row:
            raise DataError('批次不存在', 404)
        return json.loads(row['body'])

    def list(self, workspace_id=None, request_key=None):
        with closing(self.db()) as db:
            rows = db.execute('SELECT body FROM batches ORDER BY rowid DESC').fetchall()
        return [body for row in rows if (body := json.loads(row['body']))
                and (not workspace_id or body['workspace_id'] == workspace_id)
                and (not request_key or body['request_key'] == request_key)]

    def update(self, batch_id, **changes):
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if not row:
                raise DataError('批次不存在', 404)
            body = json.loads(row['body'])
            body.update(changes)
            body['updated_at'] = now()
            db.execute('UPDATE batches SET body=? WHERE batch_id=?', (encoded(body), batch_id))
        return body

    def disposition(self, batch_id, item_ids, action, note):
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if not row:
                raise DataError('批次不存在', 404)
            body = json.loads(row['body'])
            known = {issue['item_id']: issue for issue in body['issues']}
            if len(set(item_ids)) != len(item_ids) or any(item_id not in known for item_id in item_ids):
                raise DataError('请选择本批次明确存在的结果范围', 409)
            state = {'confirm': 'confirmed', 'defer': 'deferred', 'reopen': 'pending'}[action]
            for item_id in item_ids:
                known[item_id].update(disposition=state, note=note, updated_at=now())
                db.execute('INSERT INTO disposition_events VALUES(?,?,?,?,?,?)',
                           ('e' + uuid.uuid4().hex, batch_id, item_id, action, note, now()))
            body['updated_at'] = now()
            db.execute('UPDATE batches SET body=? WHERE batch_id=?', (encoded(body), batch_id))
        return body
