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
        self.asset_registry = None
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
                CREATE TABLE IF NOT EXISTS operation_requests(request_key TEXT PRIMARY KEY,
                    session_id TEXT,job_id TEXT UNIQUE,fingerprint TEXT,version INTEGER,request TEXT);
                CREATE TABLE IF NOT EXISTS session_requests(request_key TEXT PRIMARY KEY,session_id TEXT);
            ''')
            session_columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
            if 'workspace_id' not in session_columns:
                db.execute('ALTER TABLE sessions ADD COLUMN workspace_id TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS data_session_workspace ON sessions(workspace_id,updated)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(uploads)')}
            for name, kind in (('source_workspace_id', 'TEXT'), ('source_asset_id', 'TEXT'),
                               ('source_asset_revision', 'INTEGER'), ('source_blob_hash', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE uploads ADD COLUMN {name} {kind}')
            db.execute("UPDATE jobs SET status='interrupted',error=?,updated=? "
                       "WHERE status IN ('queued','running','cancelling')",
                       ('上次运行被中断，已保留最后成功版本。可重新发起操作。', now()))
        self._recover_replies()

    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def ready(self):
        return self.python is not None and self.python.is_file() and not self.closing.is_set()

    def session_dir(self, session_id):
        return self.root / 'sessions' / identity(session_id)

    def create(self, name, request_key=None, workspace_id=None):
        if workspace_id is not None:
            identity(workspace_id)
        name = str(name).strip()
        if not name or len(name) > 100:
            raise DataError('请输入 1–100 字的名称')
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT session_id FROM session_requests WHERE request_key=?',
                                  (request_key,)).fetchone() if request_key else None
            if previous:
                existing = self.get(previous['session_id'])
                if existing['workspace_id'] != workspace_id or existing['name'] != name:
                    raise DataError('原创建请求与项目或名称不一致', 409)
                return existing
            key = 's' + uuid.uuid4().hex
            folder = self.session_dir(key)
            (folder / 'uploads').mkdir(parents=True)
            (folder / 'jobs').mkdir()
            state = {'tables': {}, 'recipes': [], 'comparisons': [], 'exports': [], 'result_id': None}
            db.execute('INSERT INTO sessions(id,name,created,updated,version,state,workspace_id) VALUES (?,?,?,?,?,?,?)',
                       (key, name, now(), now(), 1, encode(state), workspace_id))
            if request_key:
                db.execute('INSERT INTO session_requests VALUES(?,?)', (request_key, key))
        return self.get(key)

    def list(self, workspace_id=None):
        return self.list_page(workspace_id)['items']

    def list_page(self, workspace_id=None, offset=0, limit=100):
        if workspace_id is not None:
            identity(workspace_id)
        with closing(self.db()) as db:
            db.execute('BEGIN')
            where = 'WHERE workspace_id=? ' if workspace_id is not None else ''
            params = (workspace_id,) if workspace_id is not None else ()
            total = db.execute('SELECT COUNT(*) FROM sessions ' + where, params).fetchone()[0]
            items = [dict(row) for row in db.execute('SELECT id,name,created,updated,version,workspace_id FROM sessions '
                + where + 'ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?', (*params, limit, offset))]
            return {'items': items, 'total': total, 'offset': offset, 'limit': limit}

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

    def outputs(self, workspace_id):
        return self.output_page(workspace_id)['items']

    def output_page(self, workspace_id, offset=0, limit=100):
        identity(workspace_id)
        with closing(self.db()) as db:
            db.execute('BEGIN')
            source = ("FROM sessions s JOIN jobs j ON j.session_id=s.id "
                      "JOIN json_each(s.state,'$.exports') e ON json_extract(e.value,'$.id')=j.id "
                      "WHERE s.workspace_id=? AND j.status='succeeded' AND j.action IN ('export','export_table') ")
            total = db.execute('SELECT COUNT(*) ' + source, (workspace_id,)).fetchone()[0]
            rows = db.execute("SELECT s.id,s.name,e.value AS export,j.id AS export_id,j.updated " + source
                + "ORDER BY j.updated DESC,j.id DESC LIMIT ? OFFSET ?", (workspace_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                export = json.loads(row['export'])
                if export:
                    items.append({'session_id': row['id'], 'session_name': row['name'], 'export_id': row['export_id'],
                                  'created_at': row['updated'], 'files': export['files']})
            return {'items': items, 'total': total, 'offset': offset, 'limit': limit}

    def upload(self, session_id, upload_id, name, suffix, path, sha256, size, *, source_asset=None):
        session = self.get(session_id)
        if source_asset is None and session['workspace_id'] and self.asset_registry is not None:
            from workbench_platform.application_services.data import register_manual_source
            source_asset = register_manual_source(self.asset_registry, session['workspace_id'], name, path, sha256, size)
        source_asset = source_asset or {}
        with closing(self.db()) as db, db:
            db.execute('INSERT INTO uploads (id,session_id,name,format,relative,sha256,size,'
                       'source_workspace_id,source_asset_id,source_asset_revision,source_blob_hash) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (upload_id, session_id, name, suffix.lstrip('.'),
                        path.relative_to(self.session_dir(session_id)).as_posix(), sha256, size,
                        source_asset.get('workspace_id'), source_asset.get('asset_id'),
                        source_asset.get('revision'), source_asset.get('blob_hash')))
        return {'id': upload_id, 'name': name, 'format': suffix.lstrip('.'), 'sha256': sha256, 'size': size,
                'source_asset': source_asset or None}

    def register_upload_source(self, session_id, upload_id):
        """Map a verified legacy manual upload when the user opens its results."""
        upload = self.upload_source(session_id, upload_id)
        workspace_id = self.get(session_id)['workspace_id']
        if upload['source_asset_id'] or not workspace_id or self.asset_registry is None:
            return upload
        from workbench_platform.application_services.data import register_manual_source
        source = register_manual_source(self.asset_registry, workspace_id, upload['name'],
            self.session_dir(session_id) / upload['relative'], upload['sha256'], upload['size'])
        with closing(self.db()) as db, db:
            db.execute('UPDATE uploads SET source_workspace_id=?,source_asset_id=?,source_asset_revision=?,source_blob_hash=? '
                'WHERE session_id=? AND id=? AND source_asset_id IS NULL',
                (source['workspace_id'], source['asset_id'], source['revision'], source['blob_hash'], session_id, upload_id))
        return self.upload_source(session_id, upload_id)

    def inspect_input(self, path, format, options=None):
        if not self.ready():
            raise DataError('数据分析环境尚未配置', 503)
        path = Path(path).resolve(strict=True)
        from packages.runtime import require_path
        try:
            path = require_path(path, 'Data analysis input', role='read')
        except ValueError as exc:
            raise DataError(str(exc)) from exc
        folder = self.root / 'inspection' / ('p' + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        request, response = folder / 'request.json', folder / 'response.json'
        request.write_text(encode({'path': str(path), 'format': format, 'options': options or {}}), encoding='utf-8')
        environment = {key: value for key, value in os.environ.items() if key.upper() in
                       {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'SYSTEMDRIVE'}}
        for key in ('CLEAR_INSTALL_CONFIG', 'CLEAR_RUNTIME_PACKAGE_ROOT', 'CLEAR_INSTALLATION_ID', 'CLEAR_IO_GUARD'):
            if key in os.environ:
                environment[key] = os.environ[key]
        environment.update({name: str(folder) for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME')})
        environment.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        environment.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
        bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('modules.data_workbench.inspect_input',run_name='__main__')"
        try:
            with (folder / 'process.log').open('wb') as log:
                completed = subprocess.run([str(self.python), '-B', '-c', bootstrap, str(self.project_root),
                    str(request), str(response)], cwd=self.project_root, env=environment,
                    stdout=log, stderr=log, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if not response.is_file() or response.stat().st_size > 256 * 1024:
                raise DataError('资料探查未返回有效摘要')
            result = json.loads(response.read_text(encoding='utf-8'))
            if completed.returncode or not result.get('ok'):
                raise DataError('无法探查资料；请检查格式、编码和工作表大小')
            return result['data']
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise DataError('资料探查未完成或格式不受支持') from None

    def submit(self, session_id, action, payload, version, request_key=None):
        if not self.ready():
            raise DataError('数据分析环境尚未配置，请通过数据工作台启动入口启动平台。', 503)
        identity(session_id)
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(payload, dict) or type(version) is not int:
            raise DataError('操作参数无效')
        job_id = 'j' + uuid.uuid4().hex
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            digest = hashlib.sha256(encode({'session_id': session_id, 'action': action,
                                            'payload': payload}).encode()).hexdigest()
            if request_key:
                prior = db.execute('SELECT * FROM operation_requests WHERE request_key=?',
                                   (request_key,)).fetchone()
                if prior:
                    if prior['fingerprint'] != digest:
                        raise DataError('操作请求标识与已提交参数不一致', 409)
                    return self.job(session_id, prior['job_id'])
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
            if request_key:
                db.execute('INSERT INTO operation_requests VALUES(?,?,?,?,?,?)',
                           (request_key, session_id, job_id, digest, version, encode(request)))
        self.pool.submit(self._run, session_id, job_id, version, request)
        return self.job(session_id, job_id)

    def operation(self, request_key):
        with closing(self.db()) as db:
            row = db.execute('SELECT session_id,job_id FROM operation_requests WHERE request_key=?',
                             (request_key,)).fetchone()
        return self.job(row['session_id'], row['job_id']) if row else None

    def operation_not_submitted(self, request_key, session_id, version, workspace_id):
        """Prove absence in the same authority/transaction used by submit.

        BEGIN IMMEDIATE waits behind an in-flight submit. The unique original
        request key still deduplicates a writer that arrives after this read.
        A changed input version or any surviving request row is not a proof.
        """
        identity(request_key)
        identity(session_id)
        if not self.ready():
            return False
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM operation_requests WHERE request_key=?',
                          (request_key,)).fetchone():
                return False
            row = db.execute('SELECT version,workspace_id FROM sessions WHERE id=?',
                             (session_id,)).fetchone()
            return bool(row and row['version'] == version and row['workspace_id'] == workspace_id)

    def _require_compare_reader(self, action, reply):
        policy = reply.get('state', {}).get('comparison', {}).get('spec', {})
        if action == 'compare' and policy.get('amount_format') == 'financial-v1':
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent, 'data-financial-amount-v1')

    def _require_compare_request_reader(self, request):
        spec = request.get('payload', {}).get('spec', {})
        if (request.get('action') == 'compare' and spec.get('amount_pairs')
                and spec.get('amount_format', 'financial-v1') == 'financial-v1'):
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent, 'data-financial-amount-v1')

    def _recover_replies(self):
        with closing(self.db()) as db:
            pending = db.execute("SELECT r.* FROM operation_requests r JOIN jobs j ON j.id=r.job_id "
                                 "WHERE j.status='interrupted'").fetchall()
        for row in pending:
            response = self.session_dir(row['session_id']) / 'jobs' / row['job_id'] / 'response.json'
            if not response.is_file():
                continue
            try:
                value = json.loads(response.read_text(encoding='utf-8'))
                if not value.get('ok'):
                    continue
                with closing(self.db()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    self._require_compare_reader(json.loads(row['request'])['action'], value)
                    changed = db.execute('UPDATE sessions SET state=?,version=version+1,updated=? '
                                         'WHERE id=? AND version=?',
                                         (encode(value['state']), now(), row['session_id'], row['version'])).rowcount
                    if changed:
                        db.execute("UPDATE jobs SET status='succeeded',result=?,error=NULL,updated=? WHERE id=?",
                                   (encode(value.get('result', {})), now(), row['job_id']))
            except (ValueError, OSError, KeyError):
                continue

    def resume_operation(self, request_key):
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM operation_requests WHERE request_key=?', (request_key,)).fetchone()
            if row is None:
                raise DataError('已提交操作不存在', 404)
            job = self.job(row['session_id'], row['job_id'])
            if job['status'] in ACTIVE or job['status'] == 'succeeded':
                return job
            if job['status'] == 'cancelled':
                raise DataError('已取消的原生操作不能直接恢复', 409)
            request = json.loads(row['request'])
            if request['action'] in {'export', 'export_table'}:
                folder = self.session_dir(row['session_id']) / 'exports' / row['job_id']
                if folder.exists():
                    raise DataError('固定成果生成中断，已保留文件，需核实原成果完整性；不会重新计算替代', 409)
            version = self.get(row['session_id'])['version']
            if version != row['version']:
                raise DataError('原生作业版本已变化，需要核实恢复点', 409)
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=?", (now(), row['job_id']))
        self.pool.submit(self._run, row['session_id'], row['job_id'], version, request)
        return self.job(row['session_id'], row['job_id'])

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
            self.job(session_id, job_id)
            # Compare the persisted state in the UPDATE; a stale polling read
            # must never resurrect a terminal child as "cancelling".
            db.execute("UPDATE jobs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,"
                       "updated=? WHERE id=? AND session_id=? AND status IN ('queued','running','cancelling')",
                       (now(), job_id, session_id))
        return self.job(session_id, job_id)

    def _run(self, session_id, job_id, version, request):
        process = None
        try:
            with closing(self.db()) as db, db:
                changed = db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status='queued'",
                                     (now(), job_id)).rowcount
            if not changed:
                return
            # The child persists result_views and its durable response before
            # the parent publishes a pointer. Claim the reader before opening
            # that child gate, so a parent crash cannot leave unmarked results.
            self._require_compare_request_reader(request)
            folder = self.session_dir(session_id) / 'jobs' / job_id
            folder.mkdir(exist_ok=True)
            request_path, response_path = folder / 'request.json', folder / 'response.json'
            request_path.write_text(encode(request), encoding='utf-8')
            environment = {key: value for key, value in os.environ.items() if key.upper() in
                           {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'TEMP', 'TMP', 'SYSTEMDRIVE'}}
            environment['PYTHONUTF8'] = '1'
            environment['PYTHONNOUSERSITE'] = '1'
            environment['PYTHONPATH'] = str(self.project_root)
            environment.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
            temporary = self.root / 'runtime-temp'
            temporary.mkdir(exist_ok=True)
            for variable in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME'):
                environment[variable] = str(temporary)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            environment['DUCKDB_TMPDIR'] = str(temporary)
            with (folder / 'process.log').open('ab') as log:
                bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('modules.data_workbench.interactive_worker',run_name='__main__')"
                process = subprocess.Popen([str(self.python), '-B', '-c', bootstrap, str(self.project_root),
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
                # The marker must be durable before the new result pointer is
                # published, including a later recovery of a completed reply.
                self._require_compare_reader(request['action'], result)
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
                has_source_rows = db.execute("SELECT 1 FROM table_rows WHERE revision=? "
                    "AND json_type(payload,'$.source_rows') IS NOT NULL LIMIT 1", (revision,)).fetchone() is not None
            else:
                raw = db.execute('SELECT metadata FROM result_views WHERE id=?', (revision,)).fetchone()
                where, args = ('id=? AND category=?', (revision, category)) if category else ('id=?', (revision,))
                count = db.execute('SELECT count(*) FROM result_rows WHERE ' + where, args).fetchone()[0]
                records = db.execute('SELECT ordinal,payload FROM result_rows WHERE ' + where +
                                     ' ORDER BY ordinal LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        if raw is None:
            raise DataError('预览不可用', 404)
        metadata = json.loads(raw[0])
        result = {'metadata': metadata, 'total': count, 'offset': offset,
                  'rows': [{'ordinal': row[0], **json.loads(row[1])} for row in records]}
        if kind == 'table':
            from .table_schema import cleaned_table_schema
            # Keep persisted metadata and its historical reference hash intact.
            result['table_schema'] = cleaned_table_schema(metadata['columns'], metadata['rows'],
                revision=revision, has_source_rows=has_source_rows)
        return result

    def result_page_after(self, session_id, result_id, next_ordinal, limit=1000):
        """Bounded keyset read from the immutable comparison's cached row view."""
        if type(next_ordinal) is not int or next_ordinal < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise DataError('固定结果索引分页参数无效')
        session = self.get(session_id)
        identity(result_id)
        if result_id not in {entry['id'] for entry in session['state']['comparisons']}:
            raise DataError('该结果不属于当前作业', 404)
        with closing(sqlite3.connect(self.session_dir(session_id) / 'views.sqlite3')) as db:
            raw = db.execute('SELECT metadata FROM result_views WHERE id=?', (result_id,)).fetchone()
            if raw is None:
                raise DataError('固定结果预览不可用', 404)
            metadata = json.loads(raw[0])
            records = db.execute('SELECT ordinal,payload FROM result_rows WHERE id=? AND ordinal>=? '
                                 'ORDER BY ordinal LIMIT ?', (result_id, next_ordinal, limit)).fetchall()
        return {'metadata': metadata, 'total': metadata['row_count'], 'offset': next_ordinal,
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

    def export_info(self, session_id, export_id):
        session = self.get(session_id)
        identity(export_id)
        export = next((item for item in session['state']['exports'] if item['id'] == export_id), None)
        if export is None:
            raise DataError('成果不存在', 404)
        if self.job(session_id, export_id)['status'] != 'succeeded':
            raise DataError('成果作业尚未成功完成', 409)
        expected = ({'results.csv', 'results.xlsx', 'results.parquet', 'summary.json', 'provenance.json'}
                    if export['result_id'] else
                    {'cleaned.csv', 'cleaned.xlsx', 'cleaned.parquet', 'summary.json', 'provenance.json'})
        if {item['name'] for item in export['files']} != expected or len(export['files']) != 5:
            raise DataError('成果文件不完整', 409)
        return export

    def upload_source(self, session_id, upload_id):
        self.get(session_id)
        identity(upload_id)
        with closing(self.db()) as db:
            row = db.execute('SELECT * FROM uploads WHERE id=? AND session_id=?',
                             (upload_id, session_id)).fetchone()
        if row is None:
            raise DataError('原始输入不存在', 409)
        item = dict(row)
        root = self.session_dir(session_id)
        source = root / item['relative']
        path = source.resolve()
        if (source.is_symlink() or not path.is_relative_to(root / 'uploads')
                or not path.is_file() or path.stat().st_size != item['size']):
            raise DataError('原始输入不可用', 409)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != item['sha256']:
            raise DataError('原始输入已变化', 409)
        item['registry_path'] = f"sessions/{session_id}/{item['relative']}"
        return item

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
