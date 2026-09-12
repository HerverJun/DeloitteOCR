"""Lazy PDF/TIFF import and bounded, isolated CPU page workers."""

from contextlib import contextmanager
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from PIL import Image
from ocr_workbench.atomic_files import read_json, write_json
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.imaging import add_image, digest, image_publication
from ocr_workbench.store import uid, now, encoded


class WaitingUnlock(ValueError):
    pass


class DocumentCancelled(Exception):
    pass


class PdfCPU:
    gate = threading.BoundedSemaphore(2)

    def __init__(self, bundle, workspace):
        self.runtime = Path(bundle) / 'runtimes/pdf/python.exe'
        self.workspace = Path(workspace)

    def call(self, request, cancelled=lambda: False, timeout=300):
        from ocr_workbench.processes import ProcessJob
        if not self.runtime.is_file():
            raise ValueError('缺少独立 PDF 运行时，请安装完整文档工作流离线包')
        while not self.gate.acquire(timeout=.1):
            if cancelled():
                raise DocumentCancelled()
        try:
            ipc_root = self.workspace / 'pdf-ipc'
            ipc_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=ipc_root) as temporary:
                response = Path(temporary) / 'response.json'
                payload = {**request, 'response_path': str(response)}
                job, process = ProcessJob(), None
                try:
                    process = subprocess.Popen([str(self.runtime), '-B', '-X', 'utf8', '-I', '-c',
                        'import sys;sys.path.insert(0,sys.argv[1]);from ocr_workbench.pdf_worker import main;main()',
                        str(Path(__file__).resolve().parents[1])], stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    job.assign(process)
                    process.stdin.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                    process.stdin.close()
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        if cancelled():
                            raise DocumentCancelled()
                        if time.monotonic() > deadline:
                            raise TimeoutError('PDF 页面处理超时，请降低 DPI 或检查文档')
                        time.sleep(.05)
                    if process.returncode or not response.is_file():
                        raise ValueError('PDF 工作进程异常退出；已完成页面保留，可重试当前页')
                    result = read_json(response)
                    if result['status'] == 'waiting_unlock':
                        raise WaitingUnlock(result['message'])
                    if result['status'] != 'success':
                        raise ValueError(result['message'])
                    return result['result']
                finally:
                    job.close()
                    if process:
                        process.wait(timeout=15)
        finally:
            self.gate.release()


class Documents:
    def __init__(self, store, bundle, gpu_queue=None, *, review_only=False):
        self.store = store
        self.cpu = PdfCPU(bundle, store.root)
        self.gpu = gpu_queue
        self.review_only = review_only
        self.passwords = {}
        self.guard = threading.RLock()
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.threads = []
        self.error = None

    def import_document(self, project_id, name, temporary, dpi=300, password=None):
        store = self.store
        store.one('projects', project_id)
        suffix = Path(name).suffix.lower()
        if suffix not in ('.pdf', '.tif', '.tiff'):
            image = add_image(store, project_id, name, temporary)
            return store.one('documents', image['id'])
        if type(dpi) not in (int, float) or not math.isfinite(dpi) or not 36 <= dpi <= 1200:
            raise ValueError('DPI 必须为 36–1200 的数值')
        locked = False
        if suffix == '.pdf':
            try:
                metadata = self.cpu.call({'operation': 'inspect', 'path': str(temporary), 'dpi': dpi, 'password': password})
            except WaitingUnlock:
                if password:
                    raise
                metadata, locked = {'pages': [], 'encrypted': True}, True
            kind = 'pdf'
        else:
            metadata = self.cpu.call({'operation': 'inspect-tiff', 'path': str(temporary), 'dpi': dpi})
            kind = 'tiff'
        key = uid()
        folder = store.root / 'projects' / project_id / 'documents' / key
        original = folder / ('original' + suffix)
        with image_publication(store, folder, directory=True) as publication:
            staged = publication.temporary / original.name
            shutil.copyfile(temporary, staged)
            source_hash = digest(staged)
            with store.transaction() as db:
                db.execute("""INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,status,metadata,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (key, project_id, name, kind, str(original.relative_to(store.root)),
                    source_hash, len(metadata['pages']), 'waiting_unlock' if locked else 'ready',
                    encoded({**{k: v for k, v in metadata.items() if k != 'pages'}, 'dpi': dpi}), now(), now()))
                self._insert_pages(db, key, metadata['pages'])
                publication.publish()
        if password:
            with self.guard:
                self.passwords[key] = password
        return store.one('documents', key)

    @staticmethod
    def _insert_pages(db, document_id, metadata):
        for page in metadata:
            db.execute("""INSERT INTO pages(id,document_id,page_number,width_points,height_points,crop_box,rotation,
                render_dpi,render_parameters,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (uid(), document_id, page['page_number'], page.get('width_points'), page.get('height_points'),
                 encoded(page['crop_box']) if page.get('crop_box') else None, page.get('rotation', 0),
                 page['render_dpi'], encoded(page), now(), now()))

    def unlock(self, document_id, password):
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise ValueError('请输入有效的临时密码')
        doc = self.store.one('documents', document_id)
        if doc['kind'] != 'pdf':
            raise ValueError('该文档不需要 PDF 密码')
        pages = self.store.document_pages(document_id, limit=1)
        dpi = pages[0]['render_dpi'] if pages else json.loads(doc['metadata']).get('dpi', 300)
        info = self.cpu.call({'operation': 'inspect', 'path': str(self.store.file(doc['original_path'])),
                              'password': password, 'dpi': dpi})
        with self.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT page_count FROM documents WHERE id=?', (document_id,)).fetchone()[0] == 0:
                self._insert_pages(db, document_id, info['pages'])
            db.execute("UPDATE documents SET status='ready',page_count=?,updated=? WHERE id=?", (len(info['pages']), now(), document_id))
        with self.guard:
            self.passwords[document_id] = password
        # Waiting jobs stay waiting until explicitly resumed; success is untouched.
        return self.store.one('documents', document_id)

    def _password(self, document_id):
        with self.guard:
            return self.passwords.get(document_id)

    def ensure_rendered(self, page_id, cancelled=lambda: False):
        store = self.store
        page = store.one('pages', page_id)
        if page['image_id']:
            return store.one('images', page['image_id'])
        doc = store.one('documents', page['document_id'])
        inbox = store.root / 'inbox'
        inbox.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=inbox) as temp:
            target = Path(temp) / 'page.png'
            if doc['kind'] == 'pdf':
                result = self.cpu.call({'operation': 'render', 'path': str(store.file(doc['original_path'])),
                    'page_number': page['page_number'], 'dpi': page['render_dpi'],
                    'password': self._password(doc['id']), 'image_output': str(target)}, cancelled)
            else:
                result = self.cpu.call({'operation': 'render-tiff', 'path': str(store.file(doc['original_path'])),
                    'page_number': page['page_number'], 'image_output': str(target)}, cancelled)
            if cancelled():
                raise DocumentCancelled()
            # Serialize publication with another window rendering the same page.
            with store.file_lock:
                existing = store.one('pages', page_id)
                if existing['image_id']:
                    return store.one('images', existing['image_id'])
                image = add_image(store, doc['project_id'], f"{doc['name']} · {page['page_number']}.png", target,
                                  page_id=page_id, pdf_to_pixel=result['metadata'].get('pdf_to_pixel'), page_payload=result)
                return image

    def process(self, page_id, mode='auto', *, force=False, engine='ppocr'):
        if mode not in ('auto', 'native', 'ocr'):
            raise ValueError('处理模式必须为 auto、native 或 ocr')
        if engine not in ('ppocr', 'paddlevl', 'glm', 'hunyuan'):
            raise ValueError('请选择四种识别引擎之一')
        if self.review_only and mode != 'native':
            raise ValueError('仅校对模式支持原生 PDF 提取；OCR 请使用完整模式')
        key = self.store.enqueue_document_stage(page_id, 'process', {'mode': mode, 'engine': engine}, force=force)
        self.wake.set()
        return key

    def start(self):
        if any(t.is_alive() for t in self.threads):
            return
        self.store.recover_document_stages()
        self.stopping.clear()
        self.threads = [threading.Thread(target=self.run, name=f'PDF CPU {n+1}', daemon=False) for n in range(2)]
        for thread in self.threads:
            thread.start()

    def status(self):
        return {'alive': sum(t.is_alive() for t in self.threads), 'maximum_pages': 2,
                'last_error': self.error, 'healthy': bool(self.threads) and all(t.is_alive() for t in self.threads)}

    def stop(self):
        self.stopping.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=20)
        if any(t.is_alive() for t in self.threads):
            raise RuntimeError('PDF 工作线程未能退出')
        self.passwords.clear()

    def run(self):
        while not self.stopping.is_set():
            try:
                if not self.step():
                    self.wake.wait(.3)
                    self.wake.clear()
            except Exception as error:
                # Retry transient storage failures without dropping the worker.
                self.error = type(error).__name__
                self.stopping.wait(.5)

    def step(self):
        from ocr_workbench.page_processing import process_stage, finalize_waiting_pages
        finalize_waiting_pages(self)
        stage = self.store.claim_document_stage()
        if stage is None:
            return False
        def cancelled():
            return self.stopping.is_set() or self.store.one('document_stages', stage['id'])['status'] != 'running'
        try:
            if stage['kind'] == 'render':
                image = self.ensure_rendered(stage['page_id'], cancelled)
                with self.store.transaction() as db:
                    db.execute('UPDATE document_stages SET version_id=? WHERE id=? AND version_id IS NULL', (image['active_version'],stage['id']))
                self.store.finish_document_stage(stage['id'], {'image_id': image['id']})
            else:
                process_stage(self, stage, cancelled)
        except WaitingUnlock:
            self.store.finish_document_stage(stage['id'], error='PDF 需要临时密码', waiting_unlock=True)
            page = self.store.one('pages', stage['page_id'])
            with self.store.transaction() as db:
                db.execute("UPDATE documents SET status='waiting_unlock' WHERE id=?", (page['document_id'],))
        except DocumentCancelled:
            with self.store.transaction() as db:
                db.execute("UPDATE document_stages SET status='interrupted',phase='等待继续' WHERE id=? AND status='running'", (stage['id'],))
        except Exception as error:
            self.store.finish_document_stage(stage['id'], error=str(error))
        return True

    def action(self, document_id, action):
        self.store.one('documents', document_id)
        transitions = {'pause': ('paused', ('queued', 'waiting_gpu')), 'resume': ('queued', ('paused', 'interrupted', 'waiting_unlock')),
                       'retry': ('queued', ('failed', 'cancelled')), 'cancel': ('cancelled', ('queued', 'running', 'paused', 'interrupted', 'waiting_unlock', 'waiting_gpu'))}
        if action not in transitions:
            raise ValueError('未知文档队列操作')
        target, sources = transitions[action]
        with self.store.transaction() as db:
            stages = db.execute('SELECT s.id,s.status FROM document_stages s JOIN pages p ON p.id=s.page_id WHERE p.document_id=?', (document_id,)).fetchall()
            affected = [s['id'] for s in stages if s['status'] in sources]
            for key in affected:
                db.execute('UPDATE document_stages SET status=?,phase=?,error=NULL WHERE id=?',
                           (target, '已取消' if target == 'cancelled' else '已暂停' if target == 'paused' else '等待处理', key))
        self.wake.set()
        if self.gpu:
            task_rows = self.store.rows('''SELECT t.id,t.project_id FROM tasks t JOIN page_ocr_inputs i ON i.task_id=t.id
                JOIN document_stages s ON s.id=i.stage_id JOIN pages p ON p.id=s.page_id WHERE p.document_id=?
                UNION SELECT t.id,t.project_id FROM tasks t JOIN pages p ON p.image_id=t.image_id
                WHERE p.document_id=? AND t.kind='geometry' ''', (document_id,document_id))
            if task_rows:
                self.gpu.action(task_rows[0]['project_id'], action, [t['id'] for t in task_rows])
        return affected
