from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import ast
import re
import io
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import uuid
import zipfile

from packages.contracts.forensics import Inspection, InspectionCreate, InspectionReview
from workbench_platform.assets.registry import AssetUnavailable
from workbench_platform.batches.store import encoded, fingerprint, now
from workbench_platform.data_api.service import DataError
from workbench_platform.audit.service import validated

SOURCE_ROOT = Path(__file__).resolve().parents[3]


def f_path(path):
    value = Path(path).absolute()
    if value.drive.upper() != 'F:' or value.resolve().drive.upper() != 'F:':
        raise DataError('图像取证的运行、缓存与输出目录必须位于本机 F 盘', 503)
    return value.resolve()


LEGACY_TOOLBOX_FILES = frozenset({'__init__.py','algorithms.py','catalog.py','core.py','model.py','review.py'})
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+\Z')


def source_identity(provenance):
    """Canonical active-code identity; an explicit derived record never falls back."""
    if not isinstance(provenance,dict):raise DataError('取证来源清单无效')
    derived = 'derived' in provenance
    active = provenance['derived'] if derived else provenance
    if not isinstance(active,dict):raise DataError('派生取证来源清单无效；不能退回上游版本')
    version = active.get('version') if derived else '0.1.0'
    if not isinstance(version,str) or not _VERSION.fullmatch(version):
        raise DataError('取证版本登记无效')
    records = active.get('files')
    if not isinstance(records,list) or not records:raise DataError('取证文件清单缺失')
    files = []
    names = set()
    for record in records:
        if not isinstance(record,dict):raise DataError('取证文件登记无效')
        name,sha = record.get('name'),record.get('sha256')
        if (not isinstance(name,str) or Path(name).name!=name or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py',name)
                or name in names or not isinstance(sha,str) or not _HASH.fullmatch(sha)):
            raise DataError('取证文件名、重复项或哈希登记无效')
        names.add(name);files.append({'name':name,'sha256':sha})
    if not LEGACY_TOOLBOX_FILES.issubset(names):raise DataError('取证必要文件登记不完整')
    identity={'schema_version':'1.0','kind':'derived' if derived else 'legacy',
              'version':version,'files':sorted(files,key=lambda value:value['name'])}
    if derived:
        worker_hash=active.get('worker_sha256')
        if (not isinstance(worker_hash,str) or not _HASH.fullmatch(worker_hash)
                or active.get('base_version')!='0.1.0' or version=='0.1.0'
                or active.get('coordinate_version')!='exif-pixel-edges-v1'):
            raise DataError('派生取证worker、基础版本或坐标版本登记无效')
        identity.update(worker_sha256=worker_hash,base_version=active['base_version'],
                        coordinate_version=active['coordinate_version'])
    elif names!=LEGACY_TOOLBOX_FILES:
        raise DataError('旧版取证文件集合与六文件约定不一致')
    return identity


def verify_toolbox_source(provenance, source_root=None):
    """Verify the selected manifest against the exact code about to execute."""
    source_root=Path(source_root or SOURCE_ROOT)
    toolbox=(source_root/'modules/image_forensics/forensic_toolbox').resolve()
    identity=source_identity(provenance)
    registered={record['name'] for record in identity['files']}
    actual={path.name for path in toolbox.iterdir() if path.is_file() and path.suffix=='.py'}
    if actual!=registered:raise DataError('取证实际文件集合与登记不一致，禁止省略或混入代码')
    for record in identity['files']:
        if hashlib.sha256((toolbox/record['name']).read_bytes()).hexdigest()!=record['sha256']:
            raise DataError('取证源码与登记版本不一致，请更新来源清单后再执行')
    versions=[]
    for statement in ast.parse((toolbox/'__init__.py').read_text('utf-8')).body:
        if isinstance(statement,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='__version__' for target in statement.targets):
            versions.append(ast.literal_eval(statement.value))
    if versions!=[identity['version']]:raise DataError('取证代码版本与登记版本不一致')
    if identity['kind']=='derived':
        worker=source_root/'modules/image_forensics/worker.py'
        if not worker.is_file() or hashlib.sha256(worker.read_bytes()).hexdigest()!=identity['worker_sha256']:
            raise DataError('派生取证worker与登记哈希不一致')
    return {**identity,'source_identity_sha256':fingerprint(identity),'runtime_path':str(toolbox)}


def validate_report_toolbox(report, execution):
    """Reject a successful worker that imported another toolbox or version."""
    runtime_record=report.get('runtime')
    runtime=runtime_record.get('toolbox') if isinstance(runtime_record,dict) else None
    if runtime is None and execution['kind']=='legacy':return
    if (not isinstance(runtime,dict) or runtime.get('version')!=execution['version']
            or not isinstance(runtime.get('path'),str)
            or Path(runtime['path']).resolve()!=Path(execution['runtime_path']).resolve()):
        raise DataError('取证worker实际来源或版本与核准代码不一致')
    if any(row.get('version')!=execution['version'] for row in report.get('evidence',[])):
        raise DataError('取证工具记录版本与实际运行版本不一致')


class ForensicService:
    def __init__(self, app, root):
        self.app, self.root = app, f_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'forensics.sqlite3'
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='image-forensics')
        self.registry = app.state.asset_library[0]
        config = json.loads((SOURCE_ROOT / 'config/forensics.json').read_text(encoding='utf-8-sig'))
        configured_python = Path(os.environ.get('WORKBENCH_FORENSICS_PYTHON', config['python']))
        self.python = f_path(configured_python if configured_python.is_absolute() else SOURCE_ROOT / configured_python)
        self.provenance = json.loads((SOURCE_ROOT / 'modules/image_forensics/source-manifest.json').read_text(encoding='utf-8-sig'))
        with closing(self.db()) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version not in {0, 1} or version == 0 and tables:
                raise DataError('图像取证数据库版本未知，原文件已保留', 503)
            db.executescript('''CREATE TABLE IF NOT EXISTS inspections(
                inspection_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(workspace_id,request_id));
                CREATE INDEX IF NOT EXISTS inspection_workspace ON inspections(workspace_id,inspection_id);
                CREATE TABLE IF NOT EXISTS review_requests(workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,
                    inspection_id TEXT NOT NULL,payload_hash TEXT NOT NULL,receipt TEXT NOT NULL,
                    PRIMARY KEY(workspace_id,request_id)); PRAGMA user_version=1;''')
            for row in db.execute('SELECT inspection_id,body FROM inspections').fetchall():
                item = json.loads(row['body'])
                if item['status'] in {'queued', 'running'}:
                    item.update(status='needs_verification', error='上次执行中断，保留原任务；请明确恢复执行', updated_at=now())
                    self.save(db, item)
        from .artifacts import configure
        configure(app.state.artifact_center)

    def db(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        return db

    @contextmanager
    def transaction(self):
        with self.lock, closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    @staticmethod
    def save(db, item):
        db.execute('UPDATE inspections SET body=? WHERE inspection_id=?', (encoded(item), item['inspection_id']))

    def workspace(self, workspace_id):
        if self.app.state.workspace_store.get_workspace(workspace_id) is None:
            raise DataError('工作空间不存在', 404)

    @staticmethod
    def row(db, workspace_id, inspection_id):
        row = db.execute('SELECT body FROM inspections WHERE workspace_id=? AND inspection_id=?',
                         (workspace_id, inspection_id)).fetchone()
        if row is None:
            raise DataError('取证任务不属于当前工作空间', 404)
        return json.loads(row[0])

    def get(self, workspace_id, inspection_id):
        self.workspace(workspace_id)
        with closing(self.db()) as db:
            return Inspection.model_validate(self.row(db, workspace_id, inspection_id)).model_dump(mode='json')

    def list(self, workspace_id, offset, limit):
        self.workspace(workspace_id)
        with closing(self.db()) as db:
            total = db.execute('SELECT COUNT(*) FROM inspections WHERE workspace_id=?', (workspace_id,)).fetchone()[0]
            rows = db.execute('SELECT body FROM inspections WHERE workspace_id=? ORDER BY inspection_id DESC LIMIT ? OFFSET ?',
                              (workspace_id, limit, offset)).fetchall()
        return {'schema_version': '1.0', 'items': [Inspection.model_validate_json(row[0]).model_dump(mode='json') for row in rows],
                'total': total, 'next_offset': offset + limit if offset + limit < total else None}

    def create(self, workspace_id, payload):
        command = validated(InspectionCreate, payload)
        self.workspace(workspace_id)
        digest = fingerprint(command)
        with self.transaction() as db:
            row = db.execute('SELECT payload_hash,body FROM inspections WHERE workspace_id=? AND request_id=?',
                             (workspace_id, command['request_id'])).fetchone()
            if row:
                if row['payload_hash'] != digest:
                    raise DataError('此取证请求已绑定其他图片或工具，不能替换', 409)
                return json.loads(row['body'])
            if not self.python.is_file():
                raise DataError('本地图像取证运行时不可用', 503)
            try:
                asset = self.registry.get(workspace_id, command['asset_id'])
            except AssetUnavailable:
                raise DataError('图片资料不属于当前工作空间或已不可用', 404) from None
            if (asset.revision, asset.blob_hash) != (command['asset_revision'], command['blob_hash']):
                raise DataError('图片来源版本已变化，请刷新后选择', 409)
            if not asset.media_type.startswith('image/') or asset.size_bytes > 32 * 1024 * 1024:
                raise DataError('请选择不超过 32 MiB 的单帧图片')
            item = {**command, 'inspection_id': 'i' + uuid.uuid4().hex, 'workspace_id': workspace_id,
                    'revision': 1, 'original_name': asset.original_name, 'status': 'queued',
                    'error': None, 'report_hash': None, 'report': None, 'files': [], 'review': None,
                    'review_history': [], 'created_at': now(), 'updated_at': now()}
            db.execute('INSERT INTO inspections VALUES(?,?,?,?,?)',
                       (item['inspection_id'], workspace_id, command['request_id'], digest, encoded(item)))
        self.executor.submit(self.execute, workspace_id, item['inspection_id'])
        return item

    def resume(self, workspace_id, inspection_id):
        with self.transaction() as db:
            item = self.row(db, workspace_id, inspection_id)
            if item['status'] != 'needs_verification':
                raise DataError('仅恢复已中断且待核实的任务；失败任务请另建新请求', 409)
            item.update(status='queued', error=None, updated_at=now())
            self.save(db, item)
        self.executor.submit(self.execute, workspace_id, inspection_id)
        return item

    def execute(self, workspace_id, inspection_id):
        output = f_path(self.root / inspection_id / ('attempt-' + uuid.uuid4().hex))
        try:
            with self.transaction() as db:
                item = self.row(db, workspace_id, inspection_id)
                item.update(status='running', updated_at=now())
                self.save(db, item)
            output.mkdir(parents=True)
            execution = verify_toolbox_source(self.provenance)
            env = dict(os.environ)
            for key in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA'):
                f_path(env[key])
            env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
            with self.registry.stage_verified(workspace_id, item['asset_id'], revision=item['asset_revision'], blob_hash=item['blob_hash']) as source:
                result = subprocess.run([str(self.python), '-B', str(SOURCE_ROOT / 'modules/image_forensics/worker.py'),
                    str(source), str(output), json.dumps(item['tools']), item['original_name']],
                    cwd=output, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (output / 'worker.log').write_bytes(result.stdout + result.stderr)
            if result.returncode:
                raise DataError('图片无法解码、超出支持范围或工具进程失败；详细记录保留在 worker.log')
            report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
            if report['input']['sha256'] != item['blob_hash'] or report['verdict'] is not None:
                raise DataError('取证输出身份或结果语义不匹配')
            validate_report_toolbox(report, execution)
            report['toolbox_source'] = self.provenance
            report['toolbox_execution'] = execution
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            files = [{'schema_version': '1.0', 'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'size_bytes': path.stat().st_size, 'media_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}
                     for path in sorted(output.iterdir()) if path.is_file()]
            report_hash = next(file['sha256'] for file in files if file['name'] == 'report.json')
            status = 'partial' if any(e['status'] == 'error' for e in report['evidence']) else 'succeeded'
            # File bytes are immutable. Results become visible only with a complete manifest.
            with self.transaction() as db:
                item = self.row(db, workspace_id, inspection_id)
                item.update(status=status, report=report, report_hash=report_hash, files=files, updated_at=now())
                self.save(db, item)
                db.execute('CREATE TABLE IF NOT EXISTS output_locations(inspection_id TEXT PRIMARY KEY,path TEXT NOT NULL)')
                db.execute('INSERT OR REPLACE INTO output_locations VALUES(?,?)', (inspection_id, output.relative_to(self.root).as_posix()))
            try:
                from .artifacts import publish
                publish(self, workspace_id, inspection_id)
            except Exception:
                with self.transaction() as db:
                    item = self.row(db, workspace_id, inspection_id)
                    item.update(error='报告已生成，但成果归集尚未完成；可原样继续归集', updated_at=now())
                    self.save(db, item)
        except Exception as error:
            if output.is_dir() and not (output / 'worker.log').exists():
                (output / 'worker.log').write_text(type(error).__name__ + ': ' + str(error), encoding='utf-8')
            message = str(error) if isinstance(error, DataError) else '取证执行失败或超时；原任务与原件已保留'
            with self.transaction() as db:
                item = self.row(db, workspace_id, inspection_id)
                item.update(status='failed', error=message, updated_at=now())
                self.save(db, item)

    def file(self, workspace_id, inspection_id, name):
        item = self.get(workspace_id, inspection_id)
        record = next((value for value in item['files'] if value['name'] == name), None)
        if record is None:
            raise DataError('取证文件不存在或尚未完整生成', 404)
        with closing(self.db()) as db:
            row = db.execute('SELECT path FROM output_locations WHERE inspection_id=?', (inspection_id,)).fetchone()
        path = f_path(self.root / row[0] / name)
        if not path.is_relative_to(self.root) or path.is_symlink():
            raise DataError('取证文件路径无效', 409)
        content = path.read_bytes()
        if len(content) != record['size_bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
            raise DataError('取证文件与固定身份不一致，禁止采用', 409)
        return content, record['media_type']

    def review(self, workspace_id, inspection_id, payload):
        command = validated(InspectionReview, payload)
        digest = fingerprint({'inspection_id': inspection_id, 'command': command})
        with self.transaction() as db:
            old = db.execute('SELECT payload_hash,receipt FROM review_requests WHERE workspace_id=? AND request_id=?',
                             (workspace_id, command['request_id'])).fetchone()
            if old:
                if old['payload_hash'] != digest:
                    raise DataError('此审阅请求已用于其他内容', 409)
                return json.loads(old['receipt'])
            item = self.row(db, workspace_id, inspection_id)
            if item['status'] not in {'succeeded', 'partial'}:
                raise DataError('尚无完整取证报告，不能写业务审阅', 409)
            if item['revision'] != command['expected_revision']:
                raise DataError('审阅版本冲突，请刷新当前说明', 409)
            self.file(workspace_id, inspection_id, 'report.json')
            item['revision'] += 1
            item['review'] = {**command, 'revision': item['revision'], 'created_at': now()}
            item['review_history'].append(item['review'])
            item['updated_at'] = now()
            self.save(db, item)
            db.execute('INSERT INTO review_requests VALUES(?,?,?,?,?)',
                       (workspace_id, command['request_id'], inspection_id, digest, encoded(item)))
            return item

    def archive(self, workspace_id, inspection_id):
        """Build a portable copy from verified immutable files, including historic reports."""
        item = self.get(workspace_id, inspection_id)
        if item['status'] not in {'succeeded', 'partial'}:
            raise DataError('报告尚未完整生成', 409)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for record in item['files']:
                name = record['name']
                if not name or any(char in name for char in ('/', '\\', ':', '\0')) or name in {'.', '..'}:
                    raise DataError('报告成员路径无效', 409)
                content, _ = self.file(workspace_id, inspection_id, name)
                archive.writestr(name, content)
            archive.writestr('download-manifest.json', encoded({
                'inspection_id': inspection_id, 'report_hash': item['report_hash'], 'files': item['files']}))
            archive.writestr('阅读说明.txt', '请先完整解压本压缩包，再打开 review.html。报告与图片保留原始固定文件及哈希。\n'.encode('utf-8'))
        return output.getvalue()

    def evidence(self, workspace_id, choice):
        with self.lock:
            return self._evidence_snapshot(workspace_id, choice)

    def _evidence_snapshot(self, workspace_id, choice):
        item = self.get(workspace_id, choice['inspection_id'])
        if item['revision'] != choice['inspection_revision'] or item['report_hash'] != choice['report_hash']:
            raise DataError('取证证据版本已变化，请刷新后明确选择', 409)
        if item['status'] not in {'succeeded', 'partial'} or not item['review']:
            raise DataError('请先完成图像取证的人工审阅说明', 409)
        try:
            with self.registry.stage_verified(workspace_id, item['asset_id'], revision=item['asset_revision'], blob_hash=item['blob_hash']):
                pass
        except AssetUnavailable:
            raise DataError('原件版本或内容已变化，不能固定此证据', 409) from None
        for file in item['files']:
            self.file(workspace_id, item['inspection_id'], file['name'])
        source = {'schema_version': '1.0', 'kind': 'file', 'asset_id': item['asset_id'],
                  'asset_revision': item['asset_revision'], 'blob_hash': item['blob_hash'], 'precision': 'exact'}
        snapshot = {'schema_version': '1.0', 'scenario': 'image_forensics', 'workspace_id': workspace_id, 'inspection': item,
                    'sources': [source], 'source_count': 1}
        snapshot['snapshot_hash'] = fingerprint(snapshot)
        return snapshot

    def close(self):
        self.executor.shutdown(wait=True)
