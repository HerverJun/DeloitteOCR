"""Load, inference, permission and uncertain-response failures stay distinct."""
from pathlib import Path
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ocr_workbench.adapter import EngineAdapter, WORKER_BOOTSTRAP, configured_worker_source, loading_timeout
from ocr_workbench.errors import EngineFailure, EngineTimeout, engine_error_details, friendly_engine_error


def test_loading_budget_is_independent_bounded_and_default_compatible():
    assert loading_timeout({}) == 240
    assert loading_timeout({'CLEAR_OCR_LOAD_TIMEOUT_SECONDS': '600'}) == 600
    for value in ('', '0', '-1', '1801', '600.5', '600 ', 'True', None):
        with pytest.raises(ValueError):
            loading_timeout({'CLEAR_OCR_LOAD_TIMEOUT_SECONDS': value})


def test_private_worker_pin_rejects_changed_missing_and_undeclared_code(tmp_path):
    source = tmp_path / 'worker-source'
    package = source / 'ocr_workbench'
    package.mkdir(parents=True)
    files = []
    for name in ('engine_host.py', 'worker.py', 'adapter.py'):
        (package / name).write_bytes(b'# reviewed source\n')
        files.append({'path': 'ocr_workbench/' + name, 'sha256': hashlib.sha256(b'# reviewed source\n').hexdigest()})
    manifest = tmp_path / 'binding.json'
    manifest.write_text(json.dumps({'app_id': 'ocr', 'worker_source': str(source), 'worker_source_files': files}), encoding='utf-8')
    environment = {'CLEAR_OCR_WORKER_SOURCE': str(source), 'CLEAR_OCR_WORKER_SOURCE_MANIFEST': str(manifest),
                   'CLEAR_OCR_WORKER_BINDING_SHA256': hashlib.sha256(manifest.read_bytes()).hexdigest()}
    assert configured_worker_source({}) is None
    assert configured_worker_source(environment) == source
    adapter = object.__new__(EngineAdapter)
    adapter.engine, adapter.process, adapter.worker_source = 'geometry', None, source
    adapter.configure_geometry('paddle')
    assert adapter.worker_source == source
    (package / 'worker.py').write_bytes(b'# changed\n')
    with pytest.raises(ValueError, match='payload changed'):
        configured_worker_source(environment)
    import sys
    import runpy
    with patch.dict('os.environ', environment), patch.object(sys, 'argv', ['worker-bootstrap', str(source)]), patch.object(runpy, 'run_module') as imported:
        with pytest.raises(ValueError, match='before child import'):
            exec(WORKER_BOOTSTRAP, {})
        imported.assert_not_called()
    (package / 'worker.py').write_bytes(b'# reviewed source\n')
    (package / 'undeclared.py').write_bytes(b'# extra\n')
    with pytest.raises(ValueError, match='undeclared'):
        configured_worker_source(environment)
    with pytest.raises(ValueError, match='complete verified binding'):
        configured_worker_source({'CLEAR_OCR_WORKER_SOURCE': str(source)})


def test_loading_progress_keeps_the_same_pipeline_model_device_and_quality():
    import sys
    from ocr_workbench.worker import load_paddle
    phases, calls = [], []
    fake_paddle = SimpleNamespace(is_compiled_with_cuda=lambda: True,
        device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 1)))
    def pipeline(**parameters):
        calls.append(parameters)
        return 'fixed-pipeline'
    with patch.dict(sys.modules, {'paddle': fake_paddle, 'paddleocr': SimpleNamespace(PaddleOCR=pipeline)}):
        pipe, elapsed = load_paddle('ppocr', Path('F:/fixed-models'), progress=phases.append)
    assert pipe == 'fixed-pipeline' and elapsed >= 0
    assert phases == ['import_paddle', 'cuda_check', 'import_paddleocr', 'initialize_pipeline', 'ready']
    assert calls[0]['device'] == 'gpu:0' and calls[0]['text_recognition_model_name'] == 'PP-OCRv6_medium_rec'
    assert calls[0]['use_textline_orientation'] is True and calls[0]['enable_mkldnn'] is False


def test_wait_timeout_reports_loading_without_resizing_advice(tmp_path):
    adapter = object.__new__(EngineAdapter)
    adapter.ipc = tmp_path
    adapter.cancelled = threading.Event()
    adapter.process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(EngineTimeout) as caught:
        adapter.wait_for(tmp_path / 'status.json', 0, stage='load')
    details = engine_error_details(caught.value, attempt_id='original')
    assert details['code'] == 'engine_load_timeout' and details['stage'] == 'load'
    assert details['retryable'] is False and details['accepted_write'] is False
    assert '缩小' not in friendly_engine_error(caught.value)
    with pytest.raises(EngineTimeout) as inference:
        adapter.wait_for(tmp_path / 'response.json', 0)
    details = engine_error_details(inference.value)
    assert details['code'] == 'engine_inference_timeout' and details['accepted_write'] is True


def test_unknown_response_does_not_repeat_accepted_engine_request(tmp_path):
    adapter = object.__new__(EngineAdapter)
    adapter.ipc = tmp_path
    adapter.engine = 'ppocr'
    adapter.wait_for = lambda *_: {'id': 'another-request', 'status': 'success'}
    with patch('ocr_workbench.adapter.publish') as submit:
        with pytest.raises(EngineFailure) as caught:
            adapter.recognize(tmp_path / 'input.png', tmp_path / 'output')
    assert submit.call_count == 1
    details = engine_error_details(caught.value)
    assert details['code'] == 'engine_response_unknown' and details['accepted_write'] is True
    assert details['retryable'] is False


def test_permission_and_private_diagnostics_do_not_suggest_blind_retry():
    details = engine_error_details(PermissionError('secret-token C:/private/model'), stage='load')
    assert details['code'] == 'engine_permission_denied' and details['stage'] == 'load'
    assert details['retryable'] is False
    assert 'secret-token' not in str(details) and 'C:/' not in str(details)
    assert '重试' not in details['user_action']


def test_queue_persists_failure_stage_and_snapshot_can_read_it(tmp_path):
    from PIL import Image
    from ocr_workbench.store import Store
    from ocr_workbench.imaging import add_image
    from ocr_workbench.task_queue import TaskQueue
    store = Store(tmp_path / 'data')
    project = store.project('error stages')['id']
    image = tmp_path / 'image.png'
    Image.new('RGB', (20, 20), 'white').save(image)
    version = add_image(store, project, 'image.png', image)['active_version']
    task_id = store.enqueue(project, [version], ['ppocr'])[0]
    class FailedLoader:
        def __init__(self, bundle, engine, root):
            self.engine, self.ready, self.process = engine, False, None
        def load(self):
            raise EngineTimeout('load')
        def unload(self):
            pass
    queue = TaskQueue(store, tmp_path, FailedLoader)
    assert queue.step() is True
    task = store.one('tasks', task_id)
    assert task['status'] == 'failed' and task['phase'] == '加载失败'
    assert '缩小' not in task['error']
    details = store.project_snapshot(project)['tasks'][0]['error_details']
    assert details['attempt_id'] == task_id and details['code'] == 'engine_load_timeout'
    assert details['accepted_write'] is True and details['engine_request_accepted'] is False
    assert store.rows('SELECT * FROM results') == []
