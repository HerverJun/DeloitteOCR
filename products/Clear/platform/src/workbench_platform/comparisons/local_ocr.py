"""Whole-page local OCR, reusing pinned engines and Clear's shared GPU ownership."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid


class LocalOcrParser:
    def __init__(self, app):
        from workbench_platform.resources.recovery import local_path
        self.manual = getattr(app.state, 'multimodal_service', None)
        if self.manual is None:
            raise ValueError('平台共享 GPU 服务未配置，本地版面解析不可用')
        from packages.runtime import resource_path
        self.program = resource_path('program_assets', environment='WORKBENCH_PROGRAM_ROOT')
        self.bundle = local_path(self.program / 'bundles/ocr')
        self.source = resource_path('ocr_source', environment='WORKBENCH_OCR_SOURCE')
        self.pro_python = resource_path('mineru_python', environment='CLEAR_MINERU_PRO_PYTHON')
        self.pro_model = resource_path('mineru_model', environment='CLEAR_MINERU_PRO_MODEL')
        paths = [Path(__file__), self.source / 'src/ocr_workbench/worker.py', self.source / 'src/ocr_workbench/engine_host.py',
                 self.source / 'src/ocr_workbench/mineru_pro.py',
                 self.bundle / 'models/PP-DocLayoutV3/source-manifest.json',
                 self.bundle / 'models/PaddleOCR-VL-1.6/source-manifest.json', self.pro_model / 'source-manifest.json']
        self.cache_identity = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in paths if path.is_file()}

    def __call__(self, page, config, output_dir, cancel=None):
        return self.parse_batch([page], config, output_dir, cancel=cancel)[page['page_id']]

    def parse_batch(self, pages, config, output_dir, cancel=None, committed=None):
        """Reuse one resident model for at most 20 pages, committing each before continuing.

        ``committed(page, raw_result)`` runs for every verified page receipt, including
        receipts recovered after an interrupted parent. Raw failures have status=failed.
        The existing GPU runner yields between pages when another owner is queued.
        """
        from workbench_platform.multimodal.runner import worker_environment, PROTOCOL
        from workbench_platform.resources.recovery import local_path, atomic_json
        from .layout import cancelled, LayoutCancelled, file_hash, fingerprint
        if not isinstance(pages, list) or not 1 <= len(pages) <= 20 or len({p['page_id'] for p in pages}) != len(pages):
            raise ValueError('本地版面批次必须包含 1 至 20 个不同页面')
        output = local_path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        engine = config['engine']
        if engine not in {'paddlevl', 'mineru_pro'}:
            raise ValueError('Unsupported local layout engine')
        runtime = self.pro_python if engine == 'mineru_pro' else self.bundle / 'runtimes/paddlevl/python.exe'
        if not runtime.is_file() or not (self.source / 'src/ocr_workbench/worker.py').is_file():
            raise ValueError('已登记的本地 OCR 程序或运行库缺失；不会自动下载')
        models = [self.pro_model] if engine == 'mineru_pro' else [self.bundle / 'models/PP-DocLayoutV3',
                                                                self.bundle / 'models/PaddleOCR-VL-1.6']
        if any(not (model / 'source-manifest.json').is_file() for model in models):
            raise ValueError('本地模型或来源凭据缺失；不会自动下载')
        checkpoints = output / 'checkpoints'
        checkpoints.mkdir(exist_ok=True)
        results, entries = {}, []
        for page in pages:
            cancelled(cancel)
            image = local_path(page['image_path'])
            if file_hash(image) != page['image_sha256']:
                raise ValueError('页面图在本地推理前发生变化')
            key = fingerprint({'format': 'clear-local-batch-v1', 'page': page['page_id'],
                               'image': page['image_sha256'], 'engine': engine, 'identity': self.cache_identity})
            entries.append({'page_id': page['page_id'], 'image': str(image), 'image_sha256': page['image_sha256'],
                            'cache_key': key, 'checkpoint': str(checkpoints / (key + '.json'))})
        by_id = {page['page_id']: page for page in pages}
        def accept(entry):
            checkpoint = local_path(entry['checkpoint'])
            receipt = json.loads(checkpoint.read_text('utf-8'))
            if (receipt.get('format') != 'clear-local-page-v1' or receipt.get('cache_key') != entry['cache_key']
                    or receipt.get('page_id') != entry['page_id'] or receipt.get('image_sha256') != entry['image_sha256']
                    or receipt.get('result_sha256') != fingerprint(receipt.get('result'))):
                raise ValueError('本地页面检查点身份或内容校验失败')
            results[entry['page_id']] = receipt['result']
            if committed:
                committed(by_id[entry['page_id']], receipt['result'])
        for entry in entries:
            if Path(entry['checkpoint']).is_file():
                saved = json.loads(Path(entry['checkpoint']).read_text('utf-8'))
                if (saved.get('result') or {}).get('status') == 'success':
                    accept(entry)
                else:
                    history = output / 'previous-failed'
                    history.mkdir(exist_ok=True)
                    Path(entry['checkpoint']).replace(history / (entry['cache_key'] + '-' + uuid.uuid4().hex + '.json'))
        def is_cancelled():
            try:
                cancelled(cancel)
                return False
            except Exception:
                return True
        while len(results) < len(pages):
            pending = [entry for entry in entries if entry['page_id'] not in results]
            epoch, request_id = 'e' + uuid.uuid4().hex, 'compare' + uuid.uuid4().hex
            attempt = local_path(output / ('attempt-' + uuid.uuid4().hex))
            attempt.mkdir()
            request = {'engine': engine, 'pages': pending, 'output': str(attempt), 'batch_root': str(output),
                       'bundle': str(self.bundle), 'source': str(self.source), 'pro_model': str(self.pro_model),
                       'options': {'device': 'cuda'}, 'runner': {'protocol': PROTOCOL, 'worker_epoch': epoch,
                         'launch_id': self.manual.launch_id, 'instance_id': self.manual.instance_id,
                         'job_id': pages[0]['page_id'], 'generation': 1, 'checkpoint_root': str(output)}}
            atomic_json(attempt / 'request.json', request)
            env = worker_environment(attempt, runtime, gpu=True)
            env.update(CLEAR_MINERU_PRO_PYTHON=str(self.pro_python), CLEAR_MINERU_PRO_MODEL=str(self.pro_model))
            callback_errors = []
            def on_commit(event):
                index = event['chunk_index']
                if not 0 <= index < len(pending):
                    raise ValueError('本地页面检查点序号无效')
                expected = local_path(pending[index]['checkpoint'])
                if local_path(attempt / event['checkpoint']['path']) != expected:
                    raise ValueError('本地页面检查点目标无效')
                try:
                    accept(pending[index])
                except Exception as error:
                    callback_errors.append(error)
                    raise
            self.manual.coordinator.request(request_id, 'manual')
            try:
                while True:
                    cancelled(cancel)
                    waiting = self.manual.coordinator.snapshot()
                    owner = getattr(waiting, 'owner_request_id', None)
                    if isinstance(owner, str) or waiting.pending and waiting.pending[0] != request_id:
                        # The shared dispatcher already exposes queue ownership;
                        # do not repeatedly spawn gated Python processes while waiting.
                        time.sleep(.25)
                        continue
                    outcome = self.manual.runner.run([str(runtime), '-B', str(Path(__file__)), '--worker',
                           str(attempt / 'request.json')], attempt, env, request, request_id=request_id,
                           cancelled=is_cancelled, committed=on_commit, timeout=1800)
                    if outcome.status != 'waiting':
                        break
                    time.sleep(.25)
                if callback_errors:
                    raise callback_errors[0]
                cancelled(cancel)
                if outcome.status == 'cancelled':
                    raise LayoutCancelled('本地 OCR 已取消')
                if outcome.status not in {'succeeded', 'yielded'}:
                    error = ValueError(outcome.error or '本地 OCR 未完成：' + outcome.status)
                    if outcome.status == 'needs_verification':
                        error.unknown_result = True
                    raise error
                # The final event can race process exit. Recover its verified receipt.
                for entry in pending:
                    if entry['page_id'] not in results and Path(entry['checkpoint']).is_file():
                        accept(entry)
                if outcome.status == 'succeeded' and any(entry['page_id'] not in results for entry in pending):
                    raise ValueError('本地批次退出但缺少完整页面检查点')
            finally:
                if request_id in self.manual.coordinator.snapshot().pending:
                    self.manual.coordinator.cancel_pending(request_id, 'manual')
        return results


def worker_main(request_path):
    """No model imports or GPU initialization before the trusted runner starts us."""
    request = json.loads(Path(request_path).read_text('utf-8'))
    output, source, bundle = map(Path, (request['output'], request['source'], request['bundle']))
    pages = request['pages']
    if not 1 <= len(pages) <= 20:
        raise ValueError('Local batch exceeds 20 pages')
    for path in (output, source, bundle, Path(request['batch_root']), Path(request['pro_model']),
                 *(Path(page[key]) for page in pages for key in ('image', 'checkpoint'))):
        from packages.runtime import require_path
        require_path(path, 'Local OCR path', role='read')
    frame = json.loads(sys.stdin.buffer.readline())
    if frame.get('command') != 'start' or frame.get('worker_epoch') != request['runner']['worker_epoch']:
        raise ValueError('Local OCR worker was not granted a GPU lease')
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(120, repeat=True)
    sys.path.insert(0, str(source / 'src'))
    import tempfile
    tempfile.tempdir = os.environ['TEMP']
    from ocr_workbench.offline import configure, install_guard
    configure(output)
    install_guard(output / 'network-blocked.log')
    from PIL import Image
    from ocr_workbench.worker import json_safe
    from ocr_workbench.tables import parse_tables
    from ocr_workbench.engine_host import Resident
    from ocr_workbench.atomic_files import write_json
    engine = request['engine']
    progress_path = output / 'progress.json'
    def progress(stage):
        write_json(progress_path, {'stage': stage, 'worker_epoch': request['runner']['worker_epoch']})
    progress('正在加载本地版面模型')
    model_paths = ([bundle / 'models/PP-DocLayoutV3', bundle / 'models/PaddleOCR-VL-1.6'] if engine == 'paddlevl'
                   else [Path(request['pro_model'])])
    revisions = {path.name: json.loads((path / 'source-manifest.json').read_text('utf-8'))['revision']
                 for path in model_paths}
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    resident = Resident(bundle, engine, output)
    try:
        for index, entry in enumerate(pages):
            started = time.monotonic()
            progress(f'正在分析当前批次第 {index+1}/{len(pages)} 页')
            image_path = Path(entry['image'])
            with image_path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['image_sha256']:
                    raise ValueError('Page image changed before resident inference')
            try:
                raw, blocks, loaded = resident.recognize(image_path, output)
                with Image.open(image_path) as image:
                    dimensions = {'width': image.width, 'height': image.height}
                text = '\n'.join(block['text'] for block in blocks)
                result = {'engine': engine, 'status': 'success', 'blocks': blocks, 'tables': parse_tables(text),
                          'raw': raw, 'image': dimensions, 'text': text, 'model_revisions': revisions,
                          'elapsed_seconds': time.monotonic()-started, 'load_seconds': loaded,
                          'resident_batch_index': index, 'resident_batch_size': len(pages),
                          'route': {'requested': engine, 'actual': engine, 'mode': 'whole_page'}}
            except Exception as error:
                result = {'engine': engine, 'status': 'failed', 'blocks': [], 'tables': [],
                          'diagnostics': [str(error)[:2000]], 'error_type': type(error).__name__,
                          'model_revisions': revisions, 'elapsed_seconds': time.monotonic()-started}
            result = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False, default=json_safe))
            checkpoint = Path(entry['checkpoint'])
            write_json(checkpoint, {'format': 'clear-local-page-v1', 'page_id': entry['page_id'],
                       'cache_key': entry['cache_key'], 'image_sha256': entry['image_sha256'],
                       'result': result, 'result_sha256': digest(result)})
            with checkpoint.open('rb') as stream:
                checkpoint_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            write_json(output / 'runner-event.json', {'event': 'chunk_committed',
                'worker_epoch': request['runner']['worker_epoch'], 'chunk_index': index,
                'checkpoint': {'path': os.path.relpath(checkpoint, output), 'sha256': checkpoint_hash}})
            # The shared runner acknowledges persistence, cancellation and GPU fairness.
            frame = json.loads(sys.stdin.buffer.readline())
            if frame.get('worker_epoch') != request['runner']['worker_epoch']:
                raise ValueError('Resident continuation has the wrong worker epoch')
            if frame.get('command') == 'stop':
                raise SystemExit(75)
            if frame.get('command') != 'continue':
                raise ValueError('Resident page continuation was not authorized')
            if result['status'] == 'failed' and 'out of memory' in str(result.get('diagnostics')).lower():
                raise RuntimeError('Local model exhausted GPU memory; remaining pages were not attempted')
        write_json(output / 'result.json', {'status': 'success', 'pages': len(pages), 'engine': engine})
    finally:
        resident.close()
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    worker_main(sys.argv[2])
