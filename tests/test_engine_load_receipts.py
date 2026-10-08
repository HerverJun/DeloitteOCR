"""CPU checks for real startup boundaries; no engine or subprocess is launched."""

import builtins
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from ocr_workbench import adapter as adapter_module
from ocr_workbench import engine_host
from ocr_workbench.adapter import Cancelled, EngineAdapter, EngineLoadTimeout
from ocr_workbench.atomic_files import read_json, write_json


@pytest.fixture
def isolated_adapter(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    (bundle / 'config').mkdir(parents=True)
    write_json(bundle / 'config/engines.json', {'paddlevl': {'capabilities': {'table': True}}})
    adapter = EngineAdapter(bundle, 'paddlevl', tmp_path / 'sessions')
    process = Mock(pid=101)
    process.poll.return_value = 0
    job = Mock(drained=True)
    reservation = Mock()
    monkeypatch.setattr(adapter_module.subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setattr(adapter_module, 'ProcessJob', Mock(return_value=job))
    monkeypatch.setattr(adapter, '_lock_legacy_gpu', Mock())
    monkeypatch.setattr('ocr_workbench.platform_resources.reserve', Mock(return_value=reservation))
    return adapter, job, reservation


def test_startup_timeout_has_phase_stage_original_cause_and_unchanged_budget(isolated_adapter, monkeypatch):
    adapter, job, reservation = isolated_adapter
    write_json(adapter.ipc / 'loading.json', {'stage': 'paddle_pipeline_initialization'})
    original = TimeoutError('old generic timeout')
    wait = Mock(side_effect=original)
    monkeypatch.setattr(adapter, 'wait_for', wait)
    with pytest.raises(EngineLoadTimeout) as caught:
        adapter.load()
    error = caught.value
    assert error.phase == 'engine_load'
    assert error.engine == 'paddlevl'
    assert error.timeout_seconds == 240
    assert error.ipc == adapter.ipc
    assert error.last_stage == 'paddle_pipeline_initialization'
    assert error.__cause__ is original
    assert '缩小图片' not in str(error)
    wait.assert_called_once_with(adapter.ipc / 'status.json', 240)
    assert not adapter.ready
    assert adapter.log is None
    job.close.assert_called_once()
    reservation.close.assert_called_once()


@pytest.mark.parametrize('contents', [None, '{broken', '[]'])
def test_absent_or_broken_loading_receipt_does_not_mask_timeout(isolated_adapter, monkeypatch, contents):
    adapter, _, _ = isolated_adapter
    if contents is not None:
        (adapter.ipc / 'loading.json').write_text(contents, encoding='utf-8')
    original = TimeoutError('startup wait expired')
    monkeypatch.setattr(adapter, 'wait_for', Mock(side_effect=original))
    with pytest.raises(EngineLoadTimeout) as caught:
        adapter.load()
    assert caught.value.last_stage is None
    assert caught.value.__cause__ is original


@pytest.mark.parametrize('original', [Cancelled('cancelled'), RuntimeError('worker exited')])
def test_cancellation_and_worker_exit_are_not_startup_timeouts(isolated_adapter, monkeypatch, original):
    adapter, _, _ = isolated_adapter
    monkeypatch.setattr(adapter, 'wait_for', Mock(side_effect=original))
    with pytest.raises(type(original)) as caught:
        adapter.load()
    assert caught.value is original
    assert not isinstance(caught.value, EngineLoadTimeout)


def test_explicit_startup_failure_is_not_timeout(isolated_adapter, monkeypatch):
    adapter, _, _ = isolated_adapter
    monkeypatch.setattr(adapter, 'wait_for', Mock(return_value={'status': 'failed', 'message': 'bad model'}))
    with pytest.raises(RuntimeError, match='bad model') as caught:
        adapter.load()
    assert not isinstance(caught.value, EngineLoadTimeout)


def test_recognition_timeout_retains_its_original_type(isolated_adapter, monkeypatch, tmp_path):
    adapter, _, _ = isolated_adapter
    original = TimeoutError('recognition deadline')
    monkeypatch.setattr(adapter, 'wait_for', Mock(side_effect=original))
    with pytest.raises(TimeoutError) as caught:
        adapter.recognize(tmp_path / 'input.png', tmp_path / 'output')
    assert caught.value is original
    assert not isinstance(caught.value, EngineLoadTimeout)


def test_slow_cpu_loader_has_import_and_pipeline_heartbeats_without_invented_substages(tmp_path, monkeypatch):
    progress = engine_host.LoadProgress(tmp_path, 'paddlevl', interval=.005)
    pipeline_heartbeat = threading.Event()
    receipts = []

    def recording_write(path, value, *, durable=False):
        assert durable is True
        receipts.append(dict(value))
        write_json(path, value, durable=durable)
        if value['stage'] == 'paddle_pipeline_initialization' and value['stage_elapsed_seconds'] >= .01:
            pipeline_heartbeat.set()

    monkeypatch.setattr('ocr_workbench.atomic_files.write_json', recording_write)
    paddle = ModuleType('paddle')
    worker = ModuleType('ocr_workbench.worker')
    windows_paths = ModuleType('ocr_workbench.windows_paths')
    windows_paths.ascii_model_directory = lambda path: nullcontext(path)
    imported = []
    original_import = builtins.__import__

    def observed_import(name, *args, **kwargs):
        if name == 'paddle':
            imported.append(read_json(progress.path)['stage'])
        return original_import(name, *args, **kwargs)

    def slow_load(engine, models):
        assert engine == 'paddlevl'
        assert read_json(progress.path)['stage'] == 'paddle_pipeline_initialization'
        assert pipeline_heartbeat.wait(2), 'The slow initializer did not receive a real heartbeat'
        assert read_json(progress.path)['status'] == 'loading'
        assert not (tmp_path / 'status.json').exists()
        return object(), 7.5

    worker.load_paddle = slow_load
    monkeypatch.setitem(sys.modules, 'paddle', paddle)
    monkeypatch.setitem(sys.modules, 'ocr_workbench.worker', worker)
    monkeypatch.setitem(sys.modules, 'ocr_workbench.windows_paths', windows_paths)
    monkeypatch.setattr(builtins, '__import__', observed_import)
    resident = None
    try:
        resident = engine_host.Resident(tmp_path, 'paddlevl', tmp_path, progress=progress)
        assert resident.loaded == 7.5  # Original loader timing is retained.
        assert imported == ['paddle_import']
        assert progress.status == 'loading'  # Only main can publish readiness.
        stages = {row['stage'] for row in receipts}
        assert {'dependency_import', 'model_path_preparation', 'paddle_import', 'paddle_pipeline_initialization'} <= stages
        assert all('weight' not in stage and 'jit' not in stage for stage in stages)
        measured = [row['stage_elapsed_seconds'] for row in receipts if row['stage'] == 'paddle_pipeline_initialization']
        assert max(measured) >= .01
    finally:
        progress.finish('failed', 'CPU test ended without publishing readiness')
        if resident is not None:
            resident.close()
    assert not progress._thread.is_alive()


def main_environment(tmp_path, monkeypatch, constructor):
    observed = []
    original_progress = engine_host.LoadProgress

    def capture_progress(ipc, engine):
        progress = original_progress(ipc, engine, interval=.005)
        observed.append(progress)
        return progress

    monkeypatch.setattr(engine_host, 'LoadProgress', capture_progress)
    monkeypatch.setattr(engine_host, 'Resident', constructor)
    monkeypatch.setattr('ocr_workbench.offline.configure', Mock())
    monkeypatch.setattr('ocr_workbench.offline.install_guard', Mock())
    monkeypatch.setattr(sys, 'argv', ['engine_host', '--bundle', str(tmp_path), '--engine', 'paddlevl', '--ipc', str(tmp_path)])
    return observed


def test_ready_is_recorded_only_after_real_status_publication_and_original_timing_is_preserved(tmp_path, monkeypatch):
    closed = []

    def construct(bundle, engine, ipc, provider, *, progress):
        progress.enter('model_initialization', 'Controlled CPU initializer')
        time.sleep(.02)
        assert read_json(progress.path)['status'] == 'loading'
        assert not (tmp_path / 'status.json').exists()
        return SimpleNamespace(loaded=7.5, close=lambda: closed.append(True))

    observed = main_environment(tmp_path, monkeypatch, construct)
    original_publish = engine_host.publish

    def stop_after_ready(path, value):
        if path.name == 'status.json' and value['status'] == 'ready':
            assert read_json(tmp_path / 'loading.json')['status'] == 'loading'
            assert read_json(tmp_path / 'loading.json')['stage'] == 'readiness_publish'
            original_publish(path, value)
            write_json(tmp_path / 'stop.json', {})
        else:
            original_publish(path, value)

    monkeypatch.setattr(engine_host, 'publish', stop_after_ready)
    assert engine_host.main() == 0
    status = read_json(tmp_path / 'status.json')
    receipt = read_json(tmp_path / 'loading.json')
    assert status['load_seconds'] == 7.5
    assert status['initialization_seconds'] >= .02
    assert receipt['elapsed_seconds'] >= status['initialization_seconds']
    assert receipt['clock_origin'] == 'after_argument_parsing'
    assert receipt['status'] == receipt['stage'] == 'ready'
    assert closed == [True]
    assert not observed[0]._thread.is_alive()


def test_initializer_failure_preserves_actual_stage_elapsed_and_stops_heartbeat(tmp_path, monkeypatch):
    original = RuntimeError('controlled constructor failure')

    def construct(bundle, engine, ipc, provider, *, progress):
        progress.enter('paddle_pipeline_initialization', 'Controlled CPU initializer')
        time.sleep(.02)
        raise original

    observed = main_environment(tmp_path, monkeypatch, construct)
    with pytest.raises(RuntimeError) as caught:
        engine_host.main()
    assert caught.value is original
    receipt = read_json(tmp_path / 'loading.json')
    assert receipt['status'] == 'failed'
    assert receipt['stage'] == 'paddle_pipeline_initialization'
    assert receipt['stage_elapsed_seconds'] >= .02
    assert receipt['message'] == str(original)
    assert read_json(tmp_path / 'status.json')['status'] == 'failed'
    assert not observed[0]._thread.is_alive()


def test_readiness_publication_failure_never_marks_loading_receipt_ready(tmp_path, monkeypatch):
    close = Mock()
    observed = main_environment(tmp_path, monkeypatch, lambda *args, **kwargs: SimpleNamespace(loaded=1, close=close))
    original_publish = engine_host.publish

    def fail_ready(path, value):
        if value.get('status') == 'ready':
            raise PermissionError('controlled status publication failure')
        original_publish(path, value)

    monkeypatch.setattr(engine_host, 'publish', fail_ready)
    with pytest.raises(PermissionError, match='controlled status'):
        engine_host.main()
    receipt = read_json(tmp_path / 'loading.json')
    assert receipt['status'] == 'failed'
    assert receipt['stage'] == 'readiness_publish'
    assert read_json(tmp_path / 'status.json')['status'] == 'failed'
    assert not observed[0]._thread.is_alive()
    close.assert_called_once()


def test_offline_configuration_failure_is_observed_before_any_model_load(tmp_path, monkeypatch):
    constructor = Mock()
    observed = main_environment(tmp_path, monkeypatch, constructor)
    monkeypatch.setattr('ocr_workbench.offline.configure', Mock(side_effect=RuntimeError('bad cache path')))
    with pytest.raises(RuntimeError, match='bad cache path'):
        engine_host.main()
    receipt = read_json(tmp_path / 'loading.json')
    assert receipt['status'] == 'failed'
    assert receipt['stage'] == 'offline_configuration'
    assert not observed[0]._thread.is_alive()
    constructor.assert_not_called()


def test_diagnostic_write_failure_keeps_original_model_failure_and_closes_heartbeat(tmp_path, monkeypatch):
    def construct(*args, **kwargs):
        raise RuntimeError('original model failure')

    observed = main_environment(tmp_path, monkeypatch, construct)

    def broken_diagnostic(path, value, **kwargs):
        if Path(path).name == 'loading.json':
            raise OSError('controlled diagnostic write failure')
        write_json(path, value, **kwargs)

    monkeypatch.setattr('ocr_workbench.atomic_files.write_json', broken_diagnostic)
    with pytest.raises(RuntimeError, match='original model failure'):
        engine_host.main()
    assert read_json(tmp_path / 'status.json')['message'] == 'original model failure'
    assert not observed[0]._thread.is_alive()


def test_non_f_ipc_path_is_rejected_before_any_write(monkeypatch):
    write = Mock()
    monkeypatch.setattr('ocr_workbench.atomic_files.write_json', write)
    with pytest.raises(ValueError, match='local F drive'):
        engine_host.LoadProgress(Path('C:/forbidden-engine-load-receipt'), 'paddlevl')
    write.assert_not_called()


def test_f_requested_path_resolving_outside_f_is_rejected_before_any_write(tmp_path, monkeypatch):
    write = Mock()
    monkeypatch.setattr('ocr_workbench.atomic_files.write_json', write)
    monkeypatch.setattr(Path, 'resolve', lambda self: Path('C:/outside-junction-target'))
    with pytest.raises(ValueError, match='local F drive'):
        engine_host.LoadProgress(tmp_path, 'paddlevl')
    write.assert_not_called()
