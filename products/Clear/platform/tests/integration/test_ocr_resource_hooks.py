"""Simulated OCR entry gates; real model/GPU memory release is a later gate."""

import os
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

OCR_SRC = Path(__file__).resolve().parents[3] / 'ocr' / 'src'
sys.path.insert(0, str(OCR_SRC))

from ocr_workbench import adapter, cli, multimodal_runtime, platform_resources  # noqa: E402
from workbench_platform.resources.gpu import (  # noqa: E402
    GpuCoordinator, Owner, ResourceBusy, ResourceEvidence,
)


@pytest.fixture
def platform_env(tmp_path, monkeypatch):
    ledger = tmp_path / 'gpu.sqlite'
    local = tmp_path / 'local'
    local.mkdir()
    for key, value in {
        'WORKBENCH_APP_ID': 'ocr', 'WORKBENCH_INSTANCE_ID': 'instance-ocr',
        'WORKBENCH_LAUNCH_ID': 'launch-ocr', 'WORKBENCH_GPU_LEDGER': str(ledger),
        'LOCALAPPDATA': str(local),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(platform_resources, '_identity', lambda: '123456789012345678')
    return ledger


def test_platform_reservation_competes_with_other_app(platform_env):
    reservation = platform_resources.reserve('recognition')
    coordinator = GpuCoordinator(platform_env)
    assert coordinator.snapshot().owner == Owner('ocr', 'launch-ocr', 'instance-ocr',
                                                  os.getpid(), '123456789012345678')
    coordinator.request('classification-job', 'classification')
    other = Owner('classification', 'launch-other', 'instance-other', os.getpid(),
                  '123456789012345678')
    with pytest.raises(ResourceBusy):
        coordinator.grant('classification-job', other, owned=lambda _: True)
    reservation.close()
    assert coordinator.grant('classification-job', other, owned=lambda _: True).generation == 2


def test_child_and_job_must_be_proven_drained(platform_env):
    reservation = platform_resources.reserve('visual-review')

    class Child:
        alive = True

        def poll(self):
            return None if self.alive else 0

    class Job:
        handle = 123
        drained = False

    child, job = Child(), Job()
    reservation.track(child, job)
    with pytest.raises(platform_resources.ResourceUnavailable):
        reservation.close()
    assert GpuCoordinator(platform_env).snapshot().owner_request_id is not None
    child.alive, job.handle, job.drained = False, None, True
    reservation.close()
    assert GpuCoordinator(platform_env).snapshot().owner_request_id is None


def test_waiting_ocr_can_cancel_without_releasing_other_owner(platform_env):
    coordinator = GpuCoordinator(platform_env)
    other = Owner('classification', 'launch-other', 'instance-other', os.getpid(),
                  '123456789012345678')
    coordinator.request('classification-job', 'classification')
    lease = coordinator.grant('classification-job', other, owned=lambda _: True)
    cancelled, outcome = threading.Event(), []

    def wait_for_gpu():
        try:
            platform_resources.reserve('recognition', cancelled)
        except platform_resources.ResourceUnavailable as error:
            outcome.append(str(error))

    thread = threading.Thread(target=wait_for_gpu)
    thread.start()
    deadline = time.monotonic() + 5
    while not coordinator.snapshot().pending and time.monotonic() < deadline:
        time.sleep(.01)
    assert coordinator.snapshot().pending
    cancelled.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and outcome
    assert coordinator.snapshot().pending == ()
    assert coordinator.snapshot().owner == other
    coordinator.release(lease, witness=lambda _: ResourceEvidence(True, True, False, True))


def test_partial_platform_configuration_fails_closed(monkeypatch, tmp_path):
    for key in platform_resources._PLATFORM_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert isinstance(platform_resources.reserve('recognition'), platform_resources._Standalone)
    monkeypatch.setenv('WORKBENCH_INSTANCE_ID', 'instance-ocr')
    with pytest.raises(platform_resources.ResourceUnavailable):
        platform_resources.reserve('recognition')
    monkeypatch.setenv('WORKBENCH_APP_ID', 'ocr')
    monkeypatch.setenv('WORKBENCH_LAUNCH_ID', 'launch-ocr')
    monkeypatch.setenv('WORKBENCH_GPU_LEDGER', str(tmp_path / 'gpu.sqlite'))
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    with pytest.raises(platform_resources.ResourceUnavailable):
        platform_resources.reserve('recognition')


def test_native_health_and_submission_do_not_advertise_unconfigured_gpu(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    (bundle / 'config').mkdir(parents=True)
    (bundle / 'config/engines.json').write_text(json.dumps({
        'glm': {'name': 'Synthetic', 'models': [], 'capabilities': {}}
    }), encoding='utf-8')
    for key in platform_resources._PLATFORM_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('WORKBENCH_APP_ID', 'ocr')
    script = (
        "import sys;from pathlib import Path;from ocr_workbench.service import create_app;"
        "bundle=Path(sys.argv[1]);root=Path(sys.argv[2]);"
        "app=create_app(bundle,root/'data','t'*32,start_queue=False,agent_enabled=False);"
        "health=next(r.endpoint for r in app.routes if r.path=='/api/health')();"
        "assert health['gpu_resource_mode']=='unavailable' and health['status']=='degraded';"
        "\ntry: app.state.application_services.require_recognition()\n"
        "except ValueError as error: assert 'GPU' in str(error)\n"
        "else: raise AssertionError('GPU submission offered')\n"
        "review=create_app(bundle,root/'review','t'*32,start_queue=False,review_only=True,agent_enabled=False);"
        "assert next(r.endpoint for r in review.routes if r.path=='/api/health')()['status']=='ready'\n"
    )
    ocr_python = OCR_SRC.parents[2] / 'program/bundles/ocr/runtimes/control/python.exe'
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(OCR_SRC)
    environment['PYTHONDONTWRITEBYTECODE']='1'
    result = subprocess.run([str(ocr_python), '-c', script, str(bundle), str(tmp_path)],
                            capture_output=True, text=True, env=environment, cwd=OCR_SRC.parent,
                            stdin=subprocess.DEVNULL,timeout=60)
    assert result.returncode == 0, result.stderr


def test_engine_resident_all_gpu_variants_but_not_cpu_rapidtable(tmp_path, monkeypatch):
    calls = []
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/engines.json').write_text(json.dumps({
        engine: {'capabilities': {}} for engine in ('ppocr', 'paddlevl', 'glm', 'hunyuan')
    }), encoding='utf-8')

    class Fake:
        def track(self, process, job):
            calls.append('track')

        def close(self):
            calls.append('close')

    monkeypatch.setattr(platform_resources, 'reserve', lambda kind, cancelled: (calls.append(kind), Fake())[1])
    monkeypatch.setattr(adapter.EngineAdapter, '_load', lambda self: calls.append('native-load'))
    variants = [('ppocr', None), ('paddlevl', None), ('glm', None), ('hunyuan', None),
                ('dewarp', None), ('geometry', 'paddle'), ('geometry', 'context'),
                ('geometry', 'tableformer-raw')]
    for engine, provider in variants:
        calls.clear()
        instance = adapter.EngineAdapter(tmp_path, engine, tmp_path / 'sessions')
        if provider:
            instance.configure_geometry(provider)
        instance.load()
        instance.unload()
        assert calls == [engine, 'native-load', 'close']
    calls.clear()
    instance = adapter.EngineAdapter(tmp_path, 'geometry', tmp_path / 'sessions')
    instance.configure_geometry('rapidtable')
    instance.load()
    instance.unload()
    assert calls == ['native-load']


def test_engine_maintenance_smoke_uses_resident_gate(tmp_path, monkeypatch):
    from ocr_workbench.engine_packages import EnginePackages

    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/engines.json').write_text(
        json.dumps({'ppocr': {'capabilities': {}}}), encoding='utf-8')
    calls = []

    class Fake:
        def close(self):
            calls.append('release')

    monkeypatch.setattr(platform_resources, 'reserve',
                        lambda kind, event: (calls.append('grant'), Fake())[1])
    monkeypatch.setattr(adapter.EngineAdapter, '_load', lambda self: calls.append('load'))
    monkeypatch.setattr(adapter.EngineAdapter, 'recognize',
                        lambda self, image, output: {'status': 'success', 'text': 'fixture'})
    packages = EnginePackages.__new__(EnginePackages)
    packages.root = tmp_path
    packages.bundle = tmp_path
    packages.smoke(tmp_path, 'ppocr')
    assert calls == ['grant', 'load', 'release']


def test_visual_review_gate_before_server_and_cpu_opt_out(tmp_path, monkeypatch):
    calls = []

    class Fake:
        def close(self):
            calls.append('close')

    monkeypatch.setattr(platform_resources, 'reserve', lambda kind, event: (calls.append(kind), Fake())[1])
    monkeypatch.setattr(multimodal_runtime, 'review_readiness', lambda *a, **k: {'ready': True})
    monkeypatch.setattr(multimodal_runtime, '_relative', lambda *a: tmp_path / 'fake')
    monkeypatch.setattr(multimodal_runtime.ReviewSession, '_lock_legacy_gpu', lambda self: None)

    def reject_server(*args, **kwargs):
        calls.append('server')
        raise RuntimeError('simulated launch failure')

    monkeypatch.setattr(multimodal_runtime.subprocess, 'Popen', reject_server)
    for profile, expected in [({'gpu_layers': 1, 'mmproj_offload': False},
                               ['visual-review', 'server', 'close']),
                              ({'gpu_layers': 0, 'mmproj_offload': False}, ['server'])]:
        calls.clear()
        config = {'profile': {'model': {'path': 'model'}, 'projector': {'path': 'projector'}, **profile},
                  'runtime': {'binary': 'server'},
                  'limits': {'context_tokens': 100, 'max_output_tokens': 10}}
        session = multimodal_runtime.ReviewSession(tmp_path, config, tmp_path / 'review')
        with pytest.raises(RuntimeError, match='simulated launch failure'):
            session.__enter__()
        assert calls == expected


def test_cli_recognize_uses_one_grant_for_all_engines(tmp_path, monkeypatch):
    calls = []

    class Fake:
        def close(self):
            calls.append('close')

    monkeypatch.setattr(platform_resources, 'reserve', lambda kind: (calls.append(kind), Fake())[1])

    def run(root, engine, image, output, timeout, *, reservation):
        calls.append(engine)
        assert isinstance(reservation, Fake)
        return {'elapsed_seconds': 1, 'blocks': [], 'tables': []}

    monkeypatch.setattr(cli, 'run_engine', run)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['ocr', '--bundle', str(tmp_path), 'recognize',
                                      '--engine', 'all', '--image', str(tmp_path / 'input.png'),
                                      '--output', str(tmp_path / 'out')])
    assert cli.main() is False
    assert calls == ['cli-recognize', *cli.ENGINES, 'close']


def test_startup_probe_is_job_owned_and_cpu_probe_skips_gpu(platform_env, monkeypatch):
    command = [sys.executable, '-c', "print('probe-ok')"]
    options = {'env': os.environ.copy(), 'capture_output': True, 'text': True, 'encoding': 'utf-8',
               'errors': 'backslashreplace', 'timeout': 10,
               'creationflags': subprocess.CREATE_NO_WINDOW}
    cpu = platform_resources.run_startup_probe(command, gpu_required=False, **options)
    assert cpu.returncode == 0 and 'probe-ok' in cpu.stdout
    assert not platform_env.exists()
    gpu = platform_resources.run_startup_probe(command, gpu_required=True, **options)
    assert gpu.returncode == 0 and 'probe-ok' in gpu.stdout
    assert GpuCoordinator(platform_env).snapshot().owner_request_id is None


def test_owned_job_drain_waits_for_residual_child(tmp_path, platform_env):
    from ocr_workbench.processes import ProcessJob

    gate, pidfile = tmp_path / 'go', tmp_path / 'child.pid'
    script = ("import subprocess,sys,time;from pathlib import Path;"
              "gate=Path(sys.argv[1]);pidfile=Path(sys.argv[2]);"
              "\nwhile not gate.exists(): time.sleep(.01)\n"
              "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
              "pidfile.write_text(str(child.pid));time.sleep(60)")
    reservation = platform_resources.reserve('residual-child')
    with ProcessJob() as job:
        parent = subprocess.Popen([sys.executable, '-c', script, str(gate), str(pidfile)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            job.assign(parent)
            reservation.track(parent, job)
            gate.touch()
            deadline = time.monotonic() + 10
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert pidfile.exists()
            assert job.active_processes() >= 2
            with pytest.raises(platform_resources.ResourceUnavailable):
                reservation.close()
            job.drain(timeout=10)
            parent.wait(timeout=5)
            assert job.drained is True and job.active_processes() == 0
        finally:
            if parent.poll() is None:
                job.close()  # Only the just-created owned Job is closed.
                parent.wait(timeout=5)
    reservation.close()
    assert GpuCoordinator(platform_env).snapshot().owner_request_id is None
