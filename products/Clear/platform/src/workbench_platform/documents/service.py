"""Durable CPU document runs; scanned content reuses Clear's native OCR queue."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import io
import json
import math
import mimetypes
import os
from pathlib import Path
import re
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
from .pdf_coverage import verified_pages
from .tables import export_xlsx, parse_tables

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


def stop_process(process):
    """Reap the owned CPU worker before releasing an attempt or shutdown."""
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            if process.poll() is None:
                raise
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def engine_identity(original):
    composition = original.get('document') or {}
    engines = list(dict.fromkeys(composition.get('ocr_sources') or [original.get('engine', 'unknown')]))
    return {'id': engines[0] if len(engines) == 1 else original.get('engine', 'document'),
            'composition_engine': original.get('engine', 'unknown'), 'engines': engines,
            'engine_info': original.get('engine_info') or {},
            'model_revisions': original.get('model_revisions') or {},
            'routes': composition.get('engine_routes') or ([original['engine_route']] if original.get('engine_route') else [])}


def public_stage_text(value):
    """Retain native public failure detail without credentials or local paths."""
    if isinstance(value, dict):
        value = value.get('message')
    if not isinstance(value, str):
        return ''
    text = ' '.join(value.split())[:1000]
    text = re.sub(r'(?i)\b(?:api[_-]?key|access[_-]?token|authorization|password|secret|token)\b\s*[:=]\s*'
                  r'(?:"[^"]*"|\'[^\']*\'|[^\s;,，；]+)', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)\b(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,})', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)https?://\S+', '[链接已隐藏]', text)
    text = re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\r\n<>"\']+', '[路径已隐藏]', text)
    return text[:500]


def table_signature(table):
    return (table['rows'], table['columns'], ' '.join(table.get('caption', '').split()),
            tuple(sorted((cell['row'], cell['column'], cell['row_span'], cell['column_span'],
                          ' '.join(cell['text'].split())) for cell in table['cells'])))


def normalized_polygon_box(polygon, image):
    width, height = image.get('width'), image.get('height')
    if (type(width) not in (int, float) or type(height) not in (int, float)
            or not math.isfinite(width) or not math.isfinite(height) or min(width, height) <= 0
            or not isinstance(polygon, list) or len(polygon) < 3
            or any(not isinstance(point, (list, tuple)) or len(point) != 2
                   or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
                   or not (0 <= point[0] <= width and 0 <= point[1] <= height) for point in polygon)):
        return None
    xs, ys = [point[0] / width for point in polygon], [point[1] / height for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def same_table_region(first, second):
    if any(not isinstance(box, (list, tuple)) or len(box) != 4
           or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in box)
           or box[0] >= box[2] or box[1] >= box[3] for box in (first, second)):
        return False
    overlap = max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(0, min(first[3], second[3]) - max(first[1], second[1]))
    area = lambda box: (box[2] - box[0]) * (box[3] - box[1])
    return overlap / (area(first) + area(second) - overlap) >= .90


def reconcile_native_tables(document, number, candidates, image, source, identity):
    """Only merge equal native/OCR tables with strong same-page geometry."""
    native = [table for table in document['tables'] if table['page_number'] == number
              and (table.get('engine_identity') or {}).get('id') == 'docvortex'
              and table['source'].get('coordinate_space') == 'normalized'
              and all(table['source'].get(key) == source.get(key) for key in ('asset_id', 'asset_revision', 'blob_hash'))]
    kept, duplicates = [], []
    for index, table in enumerate(candidates):
        box = normalized_polygon_box(table.get('polygon'), image)
        signature = table_signature(table)
        match = next((item for item in native if table_signature(item) == signature
                      and same_table_region(item['source'].get('box'), box)), None)
        if match is None:
            kept.append((index, table))
            continue
        match.setdefault('ocr_confirmations', []).append({'source': source, 'engine_identity': identity,
            'native_table_index': index, 'normalized_box': box, 'rule': 'same-page-equal-cells-overlap-v1'})
        duplicates.append((signature, box))
    return kept, duplicates


def duplicate_table_block(block, duplicates, image):
    if block.get('kind') != 'table' or not duplicates:
        return False
    text = block.get('text', '').strip()
    try:
        tables = parse_tables(text)
    except ValueError:
        return False
    if len(tables) != 1 or tables[0]['source']['start'] != 0 or tables[0]['source']['end'] != len(text):
        return False
    box, signature = normalized_polygon_box(block.get('polygon'), image), table_signature(tables[0])
    return any(signature == expected and same_table_region(box, region) for expected, region in duplicates)


def without_structured_html(text, tables):
    """Remove only HTML spans already represented by saved structural cells."""
    signatures = {table_signature(table) for table in tables}
    try:
        parsed = parse_tables(text)
    except ValueError:
        return text
    spans = [table['source'] for table in parsed if table['source']['format'] == 'html'
             and table_signature(table) in signatures]
    for span in reversed(spans):
        text = text[:span['start']] + text[span['end']:]
    return text.strip()


def readable_table_text(table):
    """Plain text preserves every literal cell, including later rows."""
    lines = [table['caption']] if table.get('caption') else []
    lines.append(f'{table["rows"]} 行 × {table["columns"]} 列')
    current, values = None, []
    for cell in sorted(table['cells'], key=lambda item: (item['row'], item['column'])):
        if current is not None and current != cell['row']:
            lines.append('\t'.join(values))
            values = []
        current = cell['row']
        values.append(cell['text'])
    if values:
        lines.append('\t'.join(values))
    return '\n'.join(lines)


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
        if command.get('fallback_engine') is None:
            command.pop('fallback_engine', None)
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
        if action == 'cancel':
            stop_process(process)
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
                                stop_process(process)
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
            # Only exact-source, text-only content-stream evidence can avoid
            # native PDF coverage. A native heading alone cannot hide a scan.
            if suffix == '.pdf' and value['ocr_policy'] == 'auto':
                try:
                    coverage = json.loads((output / 'native-coverage.json').read_text('utf-8'))
                except (OSError, ValueError):
                    coverage = None
                complete_pages = verified_pages(document, coverage, value['blob_hash'])
                for page in document['pages']:
                    complete = page['page_number'] in complete_pages
                    page.update(needs_ocr=not complete, coverage_check='complete' if complete else 'pending')
                    if complete:
                        page['coverage_source'] = 'pdf-native-text-operations'
            if value['ocr_policy'] == 'auto' and (any(page['needs_ocr'] for page in document['pages']) or suffix == '.pdf' and not document['pages']):
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
                for name in ('document.json', 'document.md', 'raw-middle.json', 'raw-model.json', 'diagnostics.json', 'native-coverage.json'):
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
            stop_process(process)
            self.execution.generation = None

    def context(self, value):
        from workbench_platform.application_services.context import SessionContext
        # A completed launch receipt belongs to its original service lifetime.
        # Explicit document resume starts a new generation, so cold preparation
        # needs a new idempotency key while repeated work in that attempt dedupes.
        operation = f'{value["document_id"]}g{value["generation"]}'
        return SessionContext(value['workspace_id'], '', '', value['generation'], 0, operation,
                              (value['asset_id'],), self.app.state, None)

    def ocr_pages(self, value, document):
        from workbench_platform.application_services.native import prepare
        ctx = self.context(value)
        selected = prepare(ctx, 'ocr', value['ocr_link_id'])
        self.current(ctx.workspace_id, value['document_id'])
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
        self.current(ctx.workspace_id, value['document_id'])
        engine = 'auto'
        pdf = Path(value['source']['original_name']).suffix.lower() == '.pdf'
        if pdf:
            imported_document = next(item for item in native['items'] if item.get('target_document_id'))
            native_document = imported_document['target_document_id']
            count = imported_document['page_count']
            by_number = {page['page_number']: page for page in document['pages']}
            if set(by_number) != set(range(1, count + 1)):
                document['warnings'].append('原生解析页数与原件不一致，缺少页面由原件页队列补读')
                document['pages'] = [by_number.get(number) or {'page_number': number, 'width': None, 'height': None,
                    'blocks': [], 'source': {**value['source'], 'page_number': number}, 'needs_ocr': True,
                    'coverage_check': 'pending', 'engine': 'unknown'} for number in range(1, count + 1)]
            request = 'r' + attempt
            pages = [page['page_number'] for page in document['pages']]
            receipt = adapter.page_request(native_document, request)
            if not receipt['submitted']:
                parameters = {'page_numbers': pages, 'mode': 'auto', 'engine': engine, 'force': False}
                if value.get('fallback_engine') is not None:
                    parameters['fallback_engine'] = value['fallback_engine']
                receipt = adapter.page_request(native_document, request, body=parameters)
            elif any(stage['status'] in {'paused', 'interrupted', 'cancelled', 'failed'} for stage in receipt['stages']):
                receipt = adapter.page_request(native_document, request, action='resume')
            identity = {'handoff_id': imported['handoff_id'], 'document_id': native_document, 'request_id': request, 'engine': engine}
        else:
            options = {'fallback_engine': value['fallback_engine']} if value.get('fallback_engine') is not None else {}
            submission = handoff.submit(ctx.workspace_id, imported['handoff_id'], [engine], **options)
            identity = {'handoff_id': imported['handoff_id'], 'platform_job_id': submission['job']['platform_job_id'], 'engine': engine}
        deadline = time.monotonic() + 900
        try:
            # Cancellation can be followed immediately by a new generation
            # while this accepted native submission is returning. Its stale
            # metadata write must still cancel the old attempt's owned child.
            self.update(ctx.workspace_id, value['document_id'], ocr=identity)
            while True:
                self.current(ctx.workspace_id, value['document_id'])
                if pdf:
                    receipt = adapter.page_request(native_document, request)
                    pending = [stage for stage in receipt['stages'] if stage['status'] in {'queued', 'running', 'waiting_gpu'}]
                    if not pending:
                        for stage in receipt['stages']:
                            if stage['status'] == 'succeeded':
                                continue
                            page = next(page for page in document['pages'] if page['page_number'] == stage['page_number'])
                            phase = public_stage_text(stage.get('phase')) or '页面处理未完成'
                            message = public_stage_text(stage.get('error')) or '该阶段未完成，请查看文档识别中的原任务后重新启动解析'
                            page.update(needs_ocr=True, coverage_check=stage['status'],
                                        ocr_error={'status': stage['status'], 'phase': phase, 'message': message})
                            document['warnings'].append(f'第 {stage["page_number"]} 页覆盖检查未完成（{phase}）：{message}')
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
                      'precision': 'page_only', 'box': None, 'coordinate_space': None,
                      'native_result_id': result.native_result_id, 'native_result_revision': revision,
                      'native_image_id': result.image_id, 'native_version_id': result.version_id,
                      'native_result_sha256': tables['content_sha256']}
            text = str((snapshot.get('edited') or {}).get('text', ''))
            original = snapshot.get('original') or {}
            composition = original.get('document') or {}
            identity = engine_identity(original)
            unresolved = composition.get('native_only_incomplete') or any(
                conflict.get('reason') == 'region_no_text' for conflict in composition.get('conflicts', []))
            unresolved = unresolved or (not text.strip() and not tables['tables'] and composition.get('page_class') != 'blank')
            page.update(needs_ocr=bool(unresolved), coverage_check='complete',
                        engine=identity['id'], engine_identity=identity,
                        model_revisions=identity['model_revisions'], ocr_source=source)
            prefix = f'{value["document_id"]}g{value["generation"]}p{number}'
            page['ocr_snapshot'] = snapshot
            # Preserve the authoritative raw snapshot. Only the Clear reading
            # projection coalesces an equal table at the same proven location.
            existing = any(table['page_number'] == number for table in document['tables'])
            candidates = tables['tables'] if composition.get('ocr_sources') or not pdf or not existing else []
            saved_tables, duplicates = reconcile_native_tables(document, number, candidates,
                original.get('image') or {}, source, identity)
            has_native = any(block['text'].strip() for block in page['blocks'] if block['type'] not in {'image', 'chart'})
            ocr_blocks = [block for block in original.get('blocks', []) if block.get('source_kind') == 'ocr']
            if not pdf or not has_native:
                readable = without_structured_html(text, candidates)
                ocr_blocks = [{'text': readable}] if readable.strip() else []
            elif not composition.get('ocr_sources'):
                ocr_blocks = []
            for index, block in enumerate(ocr_blocks):
                if duplicate_table_block(block, duplicates, original.get('image') or {}):
                    continue
                readable = without_structured_html(block.get('text', ''), candidates)
                if not readable.strip():
                    continue
                page['blocks'].append({'block_id': prefix + f'ocrb{index + 1}', 'type': 'text', 'text': readable,
                    'bbox': None, 'order': len(page['blocks']), 'source': source,
                    'engine_identity': identity, 'ocr_result_id': result.native_result_id, 'ocr_revision': revision})
            if composition.get('conflicts'):
                document['warnings'].append(f'第 {number} 页存在原生文字与识别区域的待核查项，原始合并记录已保存')
            # Keep DocVortex native tables; a second native extractor is not a
            # second table. Region OCR tables retain their actual model source.
            for index, table in saved_tables:
                identifier = prefix + f'ocr{index + 1}'
                cells = [{**cell, 'source': {**source, 'precision': 'approximate' if cell.get('polygon') else 'page_only',
                         'coordinate_space': 'native_image_pixels' if cell.get('polygon') else None}} for cell in table['cells']]
                document['tables'].append({**table, 'table_id': identifier, 'revision': 1, 'page_number': number,
                    'cells': cells, 'source': source, 'reviewed': False, 'ocr_result_id': result.native_result_id,
                    'ocr_revision': revision, 'engine_identity': identity, 'model_revisions': identity['model_revisions']})
                page['blocks'].append({'block_id': identifier, 'type': 'table', 'table_id': identifier, 'text': readable_table_text(table),
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
        identity = table.get('engine_identity') or {'id': 'docvortex', 'version': '0.5.16'}
        stream = io.BytesIO()
        export_xlsx([table], stream, source_rows=[{'image_name': value['source']['original_name'], 'input_sha256': value['blob_hash'],
            'document_id': document_id, 'page_number': table['page_number'], 'revision': table['revision'], 'engine': identity['id'],
            'engine_name': (identity.get('engine_info') or {}).get('name', identity['id']),
            'model_revisions': encoded(table.get('model_revisions') or {}), 'result_id': table.get('ocr_result_id', document_id),
            'origin': 'ocr' if table.get('ocr_result_id') else 'native',
            'table_index': table_id, 'structure_review': '人工文字校对' if table['reviewed'] else '原始结构'}])
        return stream.getvalue()

    def close(self):
        self.stop.set()
        for process in list(self.processes.values()):
            stop_process(process)
        self.executor.shutdown(wait=True, cancel_futures=True)
