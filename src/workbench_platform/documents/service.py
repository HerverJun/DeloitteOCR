"""Durable CPU document runs; scanned content reuses Clear's native OCR queue."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile

from packages.contracts.documents import DocumentParse, TableReview
from workbench_platform.audit.service import validated
from workbench_platform.batches.store import encoded, fingerprint, now
from workbench_platform.data_api.service import DataError
from .normalize import normalize
from .tables import export_xlsx

SOURCE_ROOT = Path(__file__).resolve().parents[3]
SUPPORTED = {'.pdf', '.docx', '.pptx', '.ofd', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}
TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled', 'needs_verification'}


def fpath(path):
    value = Path(path).absolute()
    if value.drive.upper() != 'F:' or value.resolve().drive.upper() != 'F:':
        raise DataError('文档运行、输出、模型与缓存必须位于本机 F 盘', 503)
    return value.resolve()


class Cancelled(Exception):
    pass


class DocumentService:
    def __init__(self, app, root):
        self.app, self.root = app, fpath(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'documents.sqlite3'
        self.registry = app.state.asset_library[0]
        self.python = fpath(os.environ.get('WORKBENCH_DOCUMENTS_PYTHON',
            'F:/OCR/output/clear-mineru-integration-20261021/dependencies/docvortex/python.exe'))
        self.lock, self.processes, self.stop = threading.RLock(), {}, threading.Event()
        self.execution = threading.local()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='clear-documents')
        with closing(self.db()) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in {0, 1}:
                raise DataError('文档数据库版本未知，原文件保留', 503)
            db.executescript('''CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,body TEXT NOT NULL,
                UNIQUE(workspace_id,request_id));
                CREATE TABLE IF NOT EXISTS document_reviews(document_id TEXT NOT NULL,table_id TEXT NOT NULL,
                revision INTEGER NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(document_id,table_id,revision));''')
            db.execute('''CREATE TABLE IF NOT EXISTS document_results(document_id TEXT NOT NULL,
                revision INTEGER NOT NULL,result_hash TEXT NOT NULL,body TEXT NOT NULL,
                PRIMARY KEY(document_id,revision))''')
            db.execute('PRAGMA user_version=1')
            for row in db.execute('SELECT body FROM documents').fetchall():
                value = json.loads(row[0])
                if value['status'] not in TERMINAL:
                    value.update(status='needs_verification', stage='interrupted',
                                 error='上次运行中断，原始输出保留；可继续解析', updated_at=now())
                    self.save(db, value)

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
    def save(db, value):
        db.execute('UPDATE documents SET body=? WHERE document_id=?', (encoded(value), value['document_id']))

    def row(self, db, workspace_id, document_id):
        row = db.execute('SELECT body FROM documents WHERE workspace_id=? AND document_id=?', (workspace_id, document_id)).fetchone()
        if row is None:
            raise DataError('文档任务不属于当前项目', 404)
        return json.loads(row[0])

    def get(self, workspace_id, document_id, revision=None):
        with closing(self.db()) as db:
            current = self.row(db, workspace_id, document_id)
            if revision is None:
                return current
            row = db.execute('SELECT body FROM document_results WHERE document_id=? AND revision=?', (document_id, revision)).fetchone()
            if row is None:
                raise DataError('该文档固定版本不存在', 404)
            return json.loads(row[0])

    def list(self, workspace_id, offset=0, limit=20):
        with closing(self.db()) as db:
            count = db.execute('SELECT COUNT(*) FROM documents WHERE workspace_id=?', (workspace_id,)).fetchone()[0]
            rows = db.execute('SELECT body FROM documents WHERE workspace_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?', (workspace_id, limit, offset)).fetchall()
        return {'schema_version': '1.0', 'items': [json.loads(row[0]) for row in rows], 'total': count,
                'next_offset': offset + limit if offset + limit < count else None}

    def create(self, workspace_id, payload):
        command = validated(DocumentParse, payload)
        from workbench_platform.resources.reader_features import require_features
        require_features(self.root.parent, 'structured-documents-v1')
        if self.app.state.workspace_store.get_workspace(workspace_id) is None:
            raise DataError('项目不存在', 404)
        digest = fingerprint(command)
        with self.transaction() as db:
            row = db.execute('SELECT payload_hash,body FROM documents WHERE workspace_id=? AND request_id=?', (workspace_id, command['request_id'])).fetchone()
            if row:
                if row[0] != digest:
                    raise DataError('该解析请求已固定其他输入或参数', 409)
                return json.loads(row[1])
            asset = self.registry.get(workspace_id, command['asset_id'])
            if (asset.revision, asset.blob_hash) != (command['asset_revision'], command['blob_hash']):
                raise DataError('原件版本变化，请刷新后解析', 409)
            suffix = Path(asset.original_name).suffix.lower()
            if suffix not in SUPPORTED or asset.size_bytes > 128 * 1024 * 1024:
                raise DataError('支持不超过 128 MiB 的 PDF、图片、DOCX、PPTX 和 OFD；数据计算请用 ClearData')
            if suffix in {'.pdf', '.docx', '.pptx', '.ofd'} and not self.python.is_file():
                raise DataError('固定 DocVortex 0.5.16 CPU 运行时不可用', 503)
            source = {'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash,
                      'original_name': asset.original_name, 'media_type': asset.media_type}
            value = {**command, 'document_id': 'd' + uuid.uuid4().hex, 'workspace_id': workspace_id, 'source': source,
                     'revision': 1, 'generation': 1, 'status': 'queued', 'stage': 'queued', 'error': None,
                     'document': None, 'result_hash': None, 'files': [], 'output_path': None, 'ocr': None,
                     'created_at': now(), 'updated_at': now(), 'capabilities': ['cancel']}
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?)', (value['document_id'], workspace_id, command['request_id'], digest, encoded(value)))
        self.executor.submit(self.execute, workspace_id, value['document_id'], value['generation'])
        return value

    def update(self, workspace_id, document_id, **changes):
        with self.transaction() as db:
            value = self.row(db, workspace_id, document_id)
            generation = getattr(self.execution, 'generation', None)
            if generation is not None and value['generation'] != generation:
                raise Cancelled()
            if value['status'] == 'cancelled' and changes.get('status') not in {None, 'cancelled'}:
                raise Cancelled()
            value.update(changes, updated_at=now())
            self.save(db, value)
        return value

    def current(self, workspace_id, document_id):
        value = self.get(workspace_id, document_id)
        if getattr(self.execution, 'generation', value['generation']) != value['generation']:
            raise Cancelled()
        if self.stop.is_set() or value['status'] == 'cancelled':
            raise Cancelled()
        return value

    def control(self, workspace_id, document_id, action):
        with self.transaction() as db:
            value = self.row(db, workspace_id, document_id)
            if action == 'cancel':
                if value['status'] not in TERMINAL:
                    value.update(status='cancelled', stage='cancelled', error=None, capabilities=['resume'], updated_at=now())
            elif action == 'resume':
                if value['status'] not in {'needs_verification', 'cancelled', 'failed', 'partial'}:
                    raise DataError('只有中断、取消、失败或部分完成的任务可继续', 409)
                value.update(status='queued', stage='queued', error=None, generation=value['generation'] + 1,
                             capabilities=['cancel'], updated_at=now())
            else:
                raise DataError('未知文档控制操作')
            self.save(db, value)
        process = self.processes.get(document_id)
        if action == 'cancel' and process and process.poll() is None:
            process.terminate()
        if action == 'resume':
            self.executor.submit(self.execute, workspace_id, document_id, value['generation'])
        return value

    def execute(self, workspace_id, document_id, generation):
        output = None
        self.execution.generation = generation
        try:
            value = self.current(workspace_id, document_id)
            value = self.update(workspace_id, document_id, status='running', stage='native_parse')
            output = fpath(self.root / document_id / f'attempt-{value["generation"]}')
            output.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            cache = output / 'cache'
            for name in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'HF_HOME', 'HUGGINGFACE_HUB_CACHE', 'TORCH_HOME', 'XDG_CACHE_HOME', 'PIP_CACHE_DIR'):
                folder = cache / name.lower()
                folder.mkdir(parents=True, exist_ok=True)
                env[name] = str(folder)
            env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', CUDA_VISIBLE_DEVICES='')
            suffix = Path(value['source']['original_name']).suffix.lower()
            if suffix in {'.pdf', '.docx', '.pptx', '.ofd'}:
                with self.registry.stage_verified(workspace_id, value['asset_id'], revision=value['asset_revision'], blob_hash=value['blob_hash']) as source:
                    with (output / 'worker.log').open('wb') as log:
                        process = subprocess.Popen([str(self.python), '-B', str(Path(__file__).with_name('worker.py')),
                            str(source), str(output), suffix[1:]], cwd=output, env=env, stdout=log, stderr=log,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        self.processes[document_id] = process
                        deadline = time.monotonic() + 600
                        while process.poll() is None:
                            self.current(workspace_id, document_id)
                            if time.monotonic() > deadline:
                                process.terminate()
                                raise DataError('文档解析超过 10 分钟，已停止；日志保留')
                            self.stop.wait(.2)
                        self.current(workspace_id, document_id)
                        if process.returncode:
                            detail = (output / 'worker.log').read_text('utf-8', errors='replace')[-1800:]
                            raise DataError('文档 CPU 解析失败，原件保留；' + detail)
                middle = json.loads((output / 'raw-middle.json').read_text('utf-8'))
                document = normalize(middle, value['source'], document_id, json.loads((output / 'diagnostics.json').read_text('utf-8')), generation=generation)
            else:
                document = normalize({'pages': [{'page_idx': 0, 'blocks': []}]}, value['source'], document_id, generation=generation)
            self.current(workspace_id, document_id)
            if value['ocr_policy'] == 'auto' and any(page['needs_ocr'] for page in document['pages']):
                self.update(workspace_id, document_id, stage='ocr')
                try:
                    self.ocr_pages(value, document)
                except Cancelled:
                    raise
                except Exception as error:
                    document['warnings'].append('扫描内容未完成识别：' + str(error)[:500])
            missing = [page['page_number'] for page in document['pages'] if page['needs_ocr']]
            if missing:
                document['warnings'].append('以下页面仍需识别，不能当作完整结果：' + ', '.join(map(str, missing)))
            document['warnings'] = [str(warning) for warning in document['warnings']]
            document['coverage'] = {'total_pages': len(document['pages']), 'readable_pages': len(document['pages']) - len(missing), 'missing_pages': missing}
            document['revision'] = generation
            document['runtime'] = {'python': str(self.python), 'parser': 'docvortex', 'version': '0.5.16',
                'worker_sha256': hashlib.sha256(Path(__file__).with_name('worker.py').read_bytes()).hexdigest(),
                'pdf_parse_mode': 'txt', 'network': 'disabled', 'device': 'cpu'}
            payload = encoded(document).encode('utf-8')
            (output / 'document.json').write_bytes(payload)
            markdown = '\n\n'.join(f'<!-- 原件第 {page["page_number"]} 页 -->\n' + '\n\n'.join(block.get('html') or block['text'] for block in page['blocks']) for page in document['pages'])
            (output / 'document.md').write_text(markdown, encoding='utf-8')
            with zipfile.ZipFile(output / 'raw-bundle.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for bundle in sorted(output.glob('page-*.bundle')):
                    for asset in sorted(bundle.rglob('*')):
                        if asset.is_file() and not asset.is_symlink() and asset.resolve().is_relative_to(output):
                            archive.write(asset, asset.relative_to(output).as_posix())
                for name in ('document.json', 'document.md', 'raw-middle.json', 'raw-model.json', 'diagnostics.json'):
                    if (output / name).is_file():
                        archive.write(output / name, name)
            files = [{'name': name, 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest(),
                      'size_bytes': (output / name).stat().st_size} for name in ('document.json', 'document.md', 'raw-middle.json', 'raw-model.json', 'diagnostics.json', 'raw-bundle.zip') if (output / name).is_file()]
            completed = self.update(workspace_id, document_id, status='partial' if missing else 'succeeded', stage='complete',
                        document=document, result_hash=hashlib.sha256(payload).hexdigest(), files=files,
                        output_path=str(output.relative_to(self.root)), error=None, capabilities=['resume'] if missing else [])
            with self.transaction() as db:
                db.execute('INSERT INTO document_results VALUES(?,?,?,?)',
                    (document_id, generation, completed['result_hash'], encoded(completed)))
        except Cancelled:
            if self.stop.is_set():
                try:
                    self.update(workspace_id, document_id, status='needs_verification', stage='interrupted', error='服务关闭，原任务已保留，可继续')
                except Cancelled:
                    pass
        except Exception as error:
            try:
                self.update(workspace_id, document_id, status='failed', stage='failed', error=str(error)[:2400], capabilities=['resume'])
            except Cancelled:
                pass
        finally:
            process = self.processes.pop(document_id, None)
            if process and process.poll() is None:
                process.terminate()
            self.execution.generation = None

    def context(self, value):
        from workbench_platform.application_services.context import SessionContext
        return SessionContext(value['workspace_id'], '', '', 0, 0, value['document_id'], (value['asset_id'],), self.app.state, None)

    def ocr_pages(self, value, document):
        from workbench_platform.application_services.native import prepare
        ctx = self.context(value)
        selected = prepare(ctx, 'ocr', value['ocr_link_id'])
        if selected['requires_project_selection']:
            raise DataError('存在多个 OCR 项目，请选择关联项目后解析')
        sets = self.app.state.asset_library[1]
        attempt = hashlib.sha256(f'{value["document_id"]}:{value["generation"]}'.encode()).hexdigest()[:32]
        source = {key: value[key] for key in ('asset_id', 'asset_revision', 'blob_hash')}
        member = {'schema_version': '1.0', 'item_id': 'i' + attempt, **source,
                  'source_locator': {'schema_version': '1.0', 'kind': 'file', **source, 'precision': 'unknown'}}
        asset_set = sets.create(ctx.workspace_id, [member], asset_set_id='s' + attempt)
        handoff = self.app.state.ocr_handoff
        imported = handoff.begin(ctx.workspace_id, selected['link_id'], asset_set.asset_set_id, asset_set.revision)
        adapter = handoff._adapter(handoff._context(ctx.workspace_id, selected['link_id']))
        native = adapter.import_status(handoff._row(ctx.workspace_id, imported['handoff_id'])['request_id'])
        engine = 'auto'
        pdf = Path(value['source']['original_name']).suffix.lower() == '.pdf'
        if pdf:
            native_document = next(item['target_document_id'] for item in native['items'] if item.get('target_document_id'))
            request = 'r' + attempt
            pages = [page['page_number'] for page in document['pages'] if page['needs_ocr']]
            receipt = adapter.page_request(native_document, request)
            if not receipt['submitted']:
                receipt = adapter.page_request(native_document, request, body={'page_numbers': pages, 'mode': 'auto', 'engine': engine, 'force': False})
            elif any(stage['status'] in {'paused', 'interrupted', 'cancelled', 'failed'} for stage in receipt['stages']):
                receipt = adapter.page_request(native_document, request, action='resume')
            identity = {'handoff_id': imported['handoff_id'], 'document_id': native_document, 'request_id': request, 'engine': engine}
        else:
            submission = handoff.submit(ctx.workspace_id, imported['handoff_id'], [engine])
            identity = {'handoff_id': imported['handoff_id'], 'platform_job_id': submission['job']['platform_job_id'], 'engine': engine}
        self.update(ctx.workspace_id, value['document_id'], ocr=identity)
        deadline = time.monotonic() + 900
        try:
            while True:
                self.current(ctx.workspace_id, value['document_id'])
                if pdf:
                    receipt = adapter.page_request(native_document, request)
                    pending = [stage for stage in receipt['stages'] if stage['status'] in {'queued', 'running', 'waiting_gpu'}]
                    if not pending:
                        snapshot = adapter.snapshot()
                        result_ids = {(stage.get('output') or {}).get('result_id'): stage['page_number'] for stage in receipt['stages'] if stage['status'] == 'succeeded'}
                        task_ids = [task['id'] for task in snapshot['tasks'] if task.get('result_id') in result_ids and task['status'] == 'succeeded']
                        break
                else:
                    service = self.app.state.job_service
                    job = service.refresh(ctx.workspace_id, service.ledger.get(ctx.workspace_id, identity['platform_job_id']))
                    batch = adapter.batch(job['native_job_ids'])
                    if not batch.pending:
                        task_ids, result_ids = job['native_job_ids'], None
                        break
                if time.monotonic() > deadline:
                    raise DataError('OCR 等待超过 15 分钟，请在文档识别中查看原任务')
                self.stop.wait(.5)
        except Cancelled:
            if pdf:
                adapter.page_request(native_document, request, action='cancel')
            else:
                self.app.state.job_service.action(ctx.workspace_id, identity['platform_job_id'], 'cancel')
            raise
        for result in adapter.results(task_ids) if task_ids else []:
            revision = adapter.result_revision(result)
            snapshot = adapter.result_snapshot(result, revision)
            tables = adapter.result_tables(result, revision)
            number = result_ids[result.native_result_id] if pdf else 1
            page = next(page for page in document['pages'] if page['page_number'] == number)
            source = {**source, 'page_number': number, 'parse_run_id': value['document_id'], 'generation': value['generation'],
                      'precision': 'page_only', 'box': None, 'coordinate_space': None}
            text = str((snapshot.get('edited') or {}).get('text', ''))
            actual = snapshot.get('original', {}).get('engine', engine)
            page.update(needs_ocr=not bool(text.strip() or tables['tables']), engine=actual)
            prefix = f'{value["document_id"]}g{value["generation"]}p{number}'
            page['blocks'].append({'block_id': prefix + 'ocr', 'type': 'text', 'text': text, 'bbox': None,
                'order': len(page['blocks']), 'source': source, 'ocr_result_id': result.native_result_id, 'ocr_revision': revision})
            page['ocr_snapshot'] = snapshot
            for index, table in enumerate(tables['tables']):
                identifier = prefix + f'ocr{index + 1}'
                document['tables'].append({**table, 'table_id': identifier, 'revision': 1, 'page_number': number,
                    'source': source, 'reviewed': False, 'ocr_result_id': result.native_result_id, 'ocr_revision': revision})
                page['blocks'].append({'block_id': identifier, 'type': 'table', 'table_id': identifier, 'text': '',
                    'bbox': None, 'order': len(page['blocks']), 'source': source})

    def parsed(self, workspace_id, document_id, revision=None):
        value = self.get(workspace_id, document_id, revision)
        if not value['document']:
            raise DataError('文档尚无保存结果', 409)
        # Verify saved canonical bytes; current metadata alone is not evidence.
        content, _ = self.file(workspace_id, document_id, 'document.json', revision)
        if hashlib.sha256(content).hexdigest() != value['result_hash']:
            raise DataError('文档固定结果哈希变化', 409)
        return json.loads(content)

    def result_snapshot(self, workspace_id, document_id, revision, expected_hash):
        self.get(workspace_id, document_id)
        with closing(self.db()) as db:
            row = db.execute('SELECT body FROM document_results WHERE document_id=? AND revision=? AND result_hash=?',
                             (document_id, revision, expected_hash)).fetchone()
        if row is None:
            raise DataError('该文档固定版本不存在', 409)
        value = json.loads(row[0])
        path = fpath(self.root / value['output_path'] / 'document.json')
        if not path.is_relative_to(self.root / document_id):
            raise DataError('文档固定版本路径无效', 409)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise DataError('文档固定版本哈希变化', 409)
        return {**value, 'document': json.loads(content)}

    def file(self, workspace_id, document_id, name, revision=None):
        value = self.get(workspace_id, document_id, revision)
        record = next((item for item in value['files'] if item['name'] == name), None)
        if not record or Path(name).name != name:
            raise DataError('文档输出不存在', 404)
        path = fpath(self.root / value['output_path'] / name)
        if not path.is_relative_to(self.root / document_id):
            raise DataError('文档输出路径无效', 409)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != record['sha256']:
            raise DataError('文档输出哈希变化，原记录保留', 409)
        return payload, mimetypes.guess_type(name)[0] or 'application/octet-stream'

    def page(self, workspace_id, document_id, number, offset=0, limit=100, revision=None):
        document = self.parsed(workspace_id, document_id, revision)
        page = next((item for item in document['pages'] if item['page_number'] == number), None)
        if page is None:
            raise DataError('文档页码不存在', 404)
        total = len(page['blocks'])
        return {**page, 'blocks': page['blocks'][offset:offset + limit], 'total': total, 'next_offset': offset + limit if offset + limit < total else None}

    def search(self, workspace_id, document_id, query, offset=0, limit=50, revision=None):
        document = self.parsed(workspace_id, document_id, revision)
        matches = [{'block_id': block['block_id'], 'page_number': page['page_number'], 'text': block['text'], 'source': block['source']}
                   for page in document['pages'] for block in page['blocks'] if query.casefold() in block['text'].casefold()]
        return {'items': matches[offset:offset + limit], 'total': len(matches), 'next_offset': offset + limit if offset + limit < len(matches) else None}

    def table(self, workspace_id, document_id, table_id, revision=None):
        document = self.parsed(workspace_id, document_id, revision)
        original = next((table for table in document['tables'] if table['table_id'] == table_id), None)
        if original is None:
            raise DataError('保存结果没有该表', 404)
        with closing(self.db()) as db:
            row = db.execute('SELECT body FROM document_reviews WHERE document_id=? AND table_id=? ORDER BY revision DESC LIMIT 1', (document_id, table_id)).fetchone()
        return {**original, **(json.loads(row[0]) if row else {}), 'original_cells': original['cells']}

    def review(self, workspace_id, document_id, table_id, payload):
        command = validated(TableReview, payload)
        with self.transaction() as db:
            table = self.table(workspace_id, document_id, table_id)
            if command['expected_revision'] != table['revision']:
                raise DataError('表格校对版本变化，请刷新后重试', 409)
            expected = {(cell['row'], cell['column']): cell for cell in table['cells']}
            cells = command['cells']
            if len(cells) != len(expected) or {(cell['row'], cell['column']) for cell in cells} != set(expected):
                raise DataError('首轮校对保留既有单元格结构，仅修改文字')
            for cell in cells:
                old = expected[cell['row'], cell['column']]
                if (cell['row_span'], cell['column_span']) != (old['row_span'], old['column_span']):
                    raise DataError('首轮校对不改变合并单元格')
                cell.update(polygon=old.get('polygon'), confidence=old.get('confidence'), source=old.get('source', table['source']))
            saved = {'revision': table['revision'] + 1, 'cells': cells, 'reviewed': True, 'reviewed_at': now()}
            db.execute('INSERT INTO document_reviews VALUES(?,?,?,?,?)', (document_id, table_id, saved['revision'], encoded(saved), now()))
        return self.table(workspace_id, document_id, table_id)

    def xlsx(self, workspace_id, document_id, table_id, revision=None):
        value = self.get(workspace_id, document_id, revision)
        table = self.table(workspace_id, document_id, table_id, revision)
        stream = io.BytesIO()
        export_xlsx([table], stream, source_rows=[{'image_name': value['source']['original_name'], 'input_sha256': value['blob_hash'],
            'document_id': document_id, 'page_number': table['page_number'], 'revision': table['revision'], 'engine': 'docvortex/clear-ocr',
            'table_index': table_id, 'structure_review': '人工文字校对' if table['reviewed'] else '原始结构'}])
        return stream.getvalue()

    def close(self):
        self.stop.set()
        for process in list(self.processes.values()):
            if process.poll() is None:
                process.terminate()
        self.executor.shutdown(wait=True, cancel_futures=True)
