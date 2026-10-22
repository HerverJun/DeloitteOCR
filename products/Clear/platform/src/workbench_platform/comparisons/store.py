"""Workspace-scoped durable comparison records and immutable call receipts."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from workbench_platform.data_api.service import DataError
from workbench_platform.supervisor.processes import require_f_path


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}', value):
        raise DataError('标识符无效', 400)
    return value


def write_json(path, value):
    path = require_f_path(Path(path), 'Comparison output')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ComparisonStore:
    def __init__(self, root):
        self.root = require_f_path(Path(root), 'Comparison store')
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'comparisons.sqlite3'
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA busy_timeout=30000')
        self.connection.execute('PRAGMA synchronous=FULL')
        with self.transaction() as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise DataError('文档比较数据库版本未知', 503)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS comparison_jobs(
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, body TEXT NOT NULL,
                    UNIQUE(workspace_id,request_id));
                CREATE TABLE IF NOT EXISTS comparison_versions(
                    job_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(job_id,revision));
                CREATE TABLE IF NOT EXISTS comparison_configs(
                    workspace_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    version INTEGER NOT NULL, body TEXT NOT NULL, key_ref TEXT,
                    PRIMARY KEY(workspace_id,kind,id,version));
                CREATE TABLE IF NOT EXISTS comparison_calls(
                    job_id TEXT NOT NULL, cache_key TEXT NOT NULL, status TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(job_id,cache_key));
                PRAGMA user_version=1;
            ''')
            for row in db.execute("SELECT body FROM comparison_jobs").fetchall():
                value = json.loads(row['body'])
                if value['status'] in {'queued', 'running', 'cancelling'}:
                    value.update(status='interrupted', stage='interrupted', error='上次处理已中断，可继续未完成的页面', updated_at=now())
                    self.save_job(db, value)
            db.execute("UPDATE comparison_calls SET status='unknown' WHERE status='inflight'")

    @contextmanager
    def transaction(self):
        with self.lock:
            db = self.connection
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def close(self):
        with self.lock:
            self.connection.close()

    def job(self, workspace_id, job_id, revision=None):
        identifier(job_id)
        with self.transaction() as db:
            row = db.execute('SELECT body FROM comparison_jobs WHERE id=? AND workspace_id=?', (job_id, workspace_id)).fetchone()
            if row is None:
                raise DataError('比较任务不属于当前项目', 404)
            value = json.loads(row['body'])
            if revision is not None and revision != value['revision']:
                row = db.execute('SELECT body FROM comparison_versions WHERE job_id=? AND revision=?', (job_id, revision)).fetchone()
                if row is None:
                    raise DataError('比较结果版本不存在', 404)
                value = json.loads(row['body'])
                value['outdated'] = True
            return value

    @staticmethod
    def save_job(db, value):
        db.execute('UPDATE comparison_jobs SET body=? WHERE id=?', (canonical(value), value['comparison_id']))

    def update(self, workspace_id, job_id, mutate):
        with self.transaction() as db:
            row = db.execute('SELECT body FROM comparison_jobs WHERE id=? AND workspace_id=?', (job_id, workspace_id)).fetchone()
            if row is None:
                raise DataError('比较任务不存在', 404)
            value = json.loads(row['body'])
            mutate(value)
            value['updated_at'] = now()
            self.save_job(db, value)
            return value

    def archive(self, value):
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO comparison_versions VALUES(?,?,?)',
                       (value['comparison_id'], value['revision'], canonical(value)))

    def jobs(self, workspace_id, offset=0, limit=20):
        with self.transaction() as db:
            total = db.execute('SELECT COUNT(*) FROM comparison_jobs WHERE workspace_id=?', (workspace_id,)).fetchone()[0]
            rows = db.execute('SELECT body FROM comparison_jobs WHERE workspace_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?',
                              (workspace_id, limit, offset)).fetchall()
        return [json.loads(row['body']) for row in rows], total

    def config(self, workspace_id, kind, config_id, version=None, secret=False):
        identifier(config_id)
        with self.transaction() as db:
            query = 'SELECT body,key_ref FROM comparison_configs WHERE workspace_id=? AND kind=? AND id=?'
            args = [workspace_id, kind, config_id]
            if version is not None:
                query += ' AND version=?'
                args.append(version)
            row = db.execute(query + ' ORDER BY version DESC LIMIT 1', args).fetchone()
        if row is None:
            raise DataError('配置版本不存在，请刷新后选择', 404)
        value = json.loads(row['body'])
        if kind == 'connection':
            value['has_key'] = bool(row['key_ref'])
            if secret:
                value['_key_ref'] = row['key_ref']
        return value

    def configs(self, workspace_id, kind):
        with self.transaction() as db:
            rows = db.execute('''SELECT c.body,c.key_ref FROM comparison_configs c
                JOIN (SELECT id,MAX(version) version FROM comparison_configs
                      WHERE workspace_id=? AND kind=? GROUP BY id) latest
                ON c.id=latest.id AND c.version=latest.version
                WHERE c.workspace_id=? AND c.kind=? ORDER BY c.rowid DESC''',
                (workspace_id, kind, workspace_id, kind)).fetchall()
        values = []
        for row in rows:
            value = json.loads(row['body'])
            if kind == 'connection':
                value['has_key'] = bool(row['key_ref'])
            values.append(value)
        return values

    def save_config(self, workspace_id, kind, value, key_ref=None, expected_version=None):
        with self.transaction() as db:
            version = db.execute('SELECT MAX(version) FROM comparison_configs WHERE workspace_id=? AND kind=? AND id=?',
                                 (workspace_id, kind, value['id'])).fetchone()[0] or 0
            if expected_version is not None and expected_version != version:
                raise DataError('配置已经更新，请刷新后保存', 409)
            value = {**value, 'version': version + 1}
            db.execute('INSERT INTO comparison_configs VALUES(?,?,?,?,?,?)',
                       (workspace_id, kind, value['id'], value['version'], canonical(value), key_ref))
        return {**value, **({'has_key': bool(key_ref)} if kind == 'connection' else {})}

    def call(self, job_id, key):
        with self.transaction() as db:
            row = db.execute('SELECT status,body FROM comparison_calls WHERE job_id=? AND cache_key=?', (job_id, key)).fetchone()
        return None if row is None else {'status': row['status'], **json.loads(row['body'])}

    def save_call(self, job_id, key, status, body):
        with self.transaction() as db:
            db.execute('INSERT INTO comparison_calls VALUES(?,?,?,?) ON CONFLICT(job_id,cache_key) DO UPDATE SET status=excluded.status,body=excluded.body',
                       (job_id, key, status, canonical(body)))
