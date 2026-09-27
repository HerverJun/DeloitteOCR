from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import uuid


IDENTITY = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,80}\Z')
ACTIONS = {'import', 'step', 'undo', 'select_revision', 'save_recipe', 'replay', 'compare', 'export', 'export_table'}
ACTIVE = ('queued', 'running', 'cancelling')


class DataError(ValueError):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise DataError('记录标识无效')
    return value


class DataWorkbenchService:
    def __init__(self, root: Path, python: Path | None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.python = python.resolve() if python else None
        self.db_path = self.root / 'sessions.sqlite3'
        self.project_root = Path(__file__).resolve().parents[3]
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='data-workbench')
        self.closing = threading.Event()
        self.processes = {}
        self.lock = threading.RLock()
        with closing(self.db()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,name TEXT,created TEXT,
                    updated TEXT,version INTEGER,state TEXT);
                CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,session_id TEXT,name TEXT,
                    format TEXT,relative TEXT,sha256 TEXT,size INTEGER);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,session_id TEXT,action TEXT,
                    status TEXT,error TEXT,result TEXT,created TEXT,updated TEXT);
                CREATE INDEX IF NOT EXISTS session_jobs ON jobs(session_id,created);
            ''')
            db.execute("UPDATE jobs SET status='interrupted',error=?,updated=? "
                       "WHERE status IN ('queued','running','cancelling')",
                       ('上次运行被中断，已保留最后成功版本。可重新发起操作。', now()))

    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def ready(self):
        return self.python is not None and self.python.is_file() and not self.closing.is_set()

    def session_dir(self, session_id):
        return self.root / 'sessions' / identity(session_id)

    def create(self, name):
        name = str(name).strip()
        if not name or len(name) > 100:
            raise DataError('请输入 1–100 字的名称')
        key = 's' + uuid.uuid4().hex
        folder = self.session_dir(key)
        (folder / 'uploads').mkdir(parents=True)
        (folder / 'jobs').mkdir()
        state = {'tables': {}, 'recipes': [], 'comparisons': [], 'exports': [], 'result_id': None}
        with closing(self.db()) as db, db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)', (key, name, now(), now(), 1, encode(state)))
        return self.get(key)

    def list(self):
        with closing(self.db()) as db:
            return [dict(row) for row in db.execute('SELECT id,name,created,updated,version FROM sessions '
                                                    'ORDER BY updated DESC LIMIT 100')]

    def get(self, session_id):
        identity(session_id)
        with closing(self.db()) as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is None:
                raise DataError('作业不存在', 404)
            value = dict(row)
            value['state'] = json.loads(value['state'])
            value['jobs'] = [dict(item) for item in db.execute(
                'SELECT id,action,status,error,created,updated FROM jobs WHERE session_id=? '
                'ORDER BY created DESC LIMIT 20', (session_id,))]
            value['uploads'] = [dict(item) for item in db.execute(
                'SELECT id,name,format,sha256,size FROM uploads WHERE session_id=?', (session_id,))]
            return value

    def upload(self, session_id, upload_id, name, suffix, path, sha256, size):
        self.get(session_id)
        with closing(self.db()) as db, db:
            db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?,?)',
                       (upload_id, session_id, name, suffix.lstrip('.'),
                        path.relative_to(self.session_dir(session_id)).as_posix(), sha256, size))
        return {'id': upload_id, 'name': name, 'format': suffix.lstrip('.'), 'sha256': sha256, 'size': size}

    def submit(self, session_id, action, payload, version):
        if not self.ready():
            raise DataError('数据分析环境尚未配置，请通过数据工作台启动入口启动平台。', 503)
        identity(session_id)
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(payload, dict) or type(version) is not int:
            raise DataError('操作参数无效')
        job_id = 'j' + uuid.uuid4().hex
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is None:
                raise DataError('作业不存在', 404)
            if row['version'] != version:
                raise DataError('作业已更新，请刷新后操作', 409)
            if db.execute("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running','cancelling')",
                          (session_id,)).fetchone():
                raise DataError('当前作业已有操作正在执行', 409)
            request = {'job_id': job_id, 'session_dir': str(self.session_dir(session_id)),
                       'action': action, 'payload': payload, 'state': json.loads(row['state'])}
            if action == 'import':
                upload = db.execute('SELECT * FROM uploads WHERE id=? AND session_id=?',
                                    (payload.get('upload_id'), session_id)).fetchone()
                if upload is None:
                    raise DataError('请先上传该文件')
                request['upload'] = dict(upload)
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                       (job_id, session_id, action, 'queued', None, None, now(), now()))
        self.pool.submit(self._run, session_id, job_id, version, request)
        return self.job(session_id, job_id)

    def job(self, session_id, job_id):
        identity(session_id)
        identity(job_id)
        with closing(self.db()) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND session_id=?', (job_id, session_id)).fetchone()
        if row is None:
            raise DataError('操作不存在', 404)
        value = dict(row)
        value['result'] = json.loads(value['result']) if value['result'] else None
        return value

    def cancel(self, session_id, job_id):
        with closing(self.db()) as db, db:
            item = self.job(session_id, job_id)
            if item['status'] in ACTIVE:
                status = 'cancelled' if item['status'] == 'queued' else 'cancelling'
                db.execute('UPDATE jobs SET status=?,updated=? WHERE id=?', (status, now(), job_id))
        return self.job(session_id, job_id)

    def _run(self, session_id, job_id, version, request):
        process = None
        try:
            with closing(self.db()) as db, db:
                changed = db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status='queued'",
                                     (now(), job_id)).rowcount
            if not changed:
                return
            folder = self.session_dir(session_id) / 'jobs' / job_id
            folder.mkdir()
            request_path, response_path = folder / 'request.json', folder / 'response.json'
            request_path.write_text(encode(request), encoding='utf-8')
            environment = {key: value for key, value in os.environ.items() if key.upper() in
                           {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'TEMP', 'TMP', 'SYSTEMDRIVE'}}
            environment['PYTHONUTF8'] = '1'
            environment['PYTHONNOUSERSITE'] = '1'
            environment['PYTHONPATH'] = str(self.project_root)
            with (folder / 'process.log').open('ab') as log:
                process = subprocess.Popen([str(self.python), '-B', '-m', 'modules.data_workbench.interactive_worker',
                                            str(request_path), str(response_path)], cwd=self.project_root,
                                           env=environment, stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            with self.lock:
                self.processes[job_id] = process
            started = time.monotonic()
            while process.poll() is None:
                cancelled = self.job(session_id, job_id)['status'] == 'cancelling'
                if self.closing.is_set() or cancelled or time.monotonic() - started > 180:
                    process.terminate()
                    process.wait(timeout=10)
                    raise DataError('操作已取消，保留上一个成功版本。' if cancelled else '操作中断或超时，保留上一个成功版本。')
                time.sleep(0.1)
            if not response_path.is_file() or response_path.stat().st_size > 4 * 1024 * 1024:
                raise DataError('分析进程未返回结果，请检查文件与分析环境。')
            result = json.loads(response_path.read_text(encoding='utf-8'))
            if process.returncode != 0 or not result.get('ok'):
                raise DataError(result.get('error', '处理失败，已保留上一个成功版本。'))
            with closing(self.db()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                status = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
                if status == 'cancelling' or self.closing.is_set():
                    db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?", (now(), job_id))
                    return
                changed = db.execute('UPDATE sessions SET state=?,version=version+1,updated=? WHERE id=? AND version=?',
                                     (encode(result['state']), now(), session_id, version)).rowcount
                if changed != 1:
                    raise DataError('作业版本已变化，结果未覆盖当前数据。', 409)
                db.execute("UPDATE jobs SET status='succeeded',result=?,updated=? WHERE id=?",
                           (encode(result.get('result', {})), now(), job_id))
        except Exception as error:
            message = str(error) if isinstance(error, DataError) else '处理未完成，请检查文件或重试。'
            with closing(self.db()) as db, db:
                db.execute("UPDATE jobs SET status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE 'failed' END,"
                           'error=?,updated=? WHERE id=?', (message[:300], now(), job_id))
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            with self.lock:
                self.processes.pop(job_id, None)

    def page(self, session_id, kind, revision, offset, limit, category=None):
        session = self.get(session_id)
        identity(revision)
        if kind == 'table':
            allowed = {entry['revision'] for table in session['state']['tables'].values()
                       for entry in table.get('versions', table['history'])}
        else:
            allowed = {entry['id'] for entry in session['state']['comparisons']}
        if revision not in allowed:
            raise DataError('该版本不属于当前作业', 404)
        path = self.session_dir(session_id) / 'views.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            if kind == 'table':
                raw = db.execute('SELECT metadata FROM table_views WHERE revision=?', (revision,)).fetchone()
                records = db.execute('SELECT ordinal,payload FROM table_rows WHERE revision=? '
                                     'ORDER BY ordinal LIMIT ? OFFSET ?', (revision, limit, offset)).fetchall()
                count = json.loads(raw[0])['rows'] if raw else 0
            else:
                raw = db.execute('SELECT metadata FROM result_views WHERE id=?', (revision,)).fetchone()
                where, args = ('id=? AND category=?', (revision, category)) if category else ('id=?', (revision,))
                count = db.execute('SELECT count(*) FROM result_rows WHERE ' + where, args).fetchone()[0]
                records = db.execute('SELECT ordinal,payload FROM result_rows WHERE ' + where +
                                     ' ORDER BY ordinal LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        if raw is None:
            raise DataError('预览不可用', 404)
        return {'metadata': json.loads(raw[0]), 'total': count, 'offset': offset,
                'rows': [{'ordinal': row[0], **json.loads(row[1])} for row in records]}

    def download(self, session_id, export_id, name):
        session = self.get(session_id)
        for export in session['state']['exports']:
            if export['id'] == export_id:
                for item in export['files']:
                    if item['name'] == name:
                        root = self.session_dir(session_id)
                        path = (root / item['relative']).resolve()
                        if not path.is_relative_to(root / 'exports') or not path.is_file():
                            break
                        with path.open('rb') as stream:
                            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                        if digest != item['sha256']:
                            raise DataError('成果内容已改变，无法下载', 409)
                        return path
        raise DataError('成果不存在', 404)

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
