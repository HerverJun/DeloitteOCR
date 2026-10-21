"""Real Windows named Jobs and native admission; no CUDA or model inference."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from workbench_platform.resources.gpu import GpuCoordinator, Owner
from workbench_platform.resources.native import (BLOCKED, launch_record, recover_native_resources)
from workbench_platform.resources.recovery import WindowsJob, owner_active, process_identity

SOURCE = Path(__file__).resolve().parents[2]
OCR = Path('F:/OCR/release/Clear/versions/0.10.0/ocr/src')

NATIVE = '''import json,os,subprocess,sys,threading,time
from pathlib import Path
source,ocr,out,mode=map(str,sys.argv[1:5]);out=Path(out)
assert sys.stdin.readline().strip()=='start'
launch=json.loads((out/'launch-environment.json').read_text('utf-8'))
os.environ.update(WORKBENCH_NATIVE_LAUNCH_RECORD=launch['path'],WORKBENCH_NATIVE_LAUNCH_SHA256=launch['sha256'])
sys.path[:0]=[str(Path(source)/'src'),ocr]
from workbench_platform.resources import native
from ocr_workbench import platform_resources
def phase(value):
    with (out/'phases.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(value,ensure_ascii=False)+'\\n')
native.install(platform_resources,app_id='ocr',phase=phase)
if mode=='crash_after_grant':native.persist_owner=lambda *args:os._exit(71)
try:
    reservation=platform_resources.reserve('ppocr')
except Exception as error:
    (out/'error.txt').write_text(str(error),encoding='utf-8')
    raise
(out/'granted').write_text('ready')
if mode=='descendant':
    child=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(120)'])
    (out/'child.pid').write_text(str(child.pid))
while not (out/'exit').exists():
    if (out/'crash').exists():os._exit(72)
    time.sleep(.02)
reservation.close()
'''


def eventually(probe, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(.02)
    return bool(probe())


@pytest.fixture
def native_world(tmp_path):
    assert tmp_path.drive.upper() == 'F:'
    data = tmp_path / 'data'
    data.mkdir()
    script = tmp_path / 'native.py'
    script.write_text(NATIVE, encoding='utf-8')
    started = []
    def start(name, mode='hold'):
        out = tmp_path / name
        out.mkdir()
        job = WindowsJob()
        env = dict(os.environ)
        for key in ('TEMP', 'TMP', 'TMPDIR', 'LOCALAPPDATA', 'APPDATA'):
            path = tmp_path / 'environment' / key
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
        env.update(WORKBENCH_APP_ID='ocr', WORKBENCH_LAUNCH_ID='launch' + name,
                   WORKBENCH_INSTANCE_ID='instance' + name, WORKBENCH_GPU_LEDGER=str(data / 'gpu.sqlite3'),
                   PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-I', '-B', '-u', str(script), str(SOURCE), str(OCR), str(out), mode],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=tmp_path, env=env, text=True)
        started.append((job, process))
        job.assign(process)
        record, record_hash = launch_record(data / 'native-launches', requester='ocr',
            launch_id=env['WORKBENCH_LAUNCH_ID'], instance_id=env['WORKBENCH_INSTANCE_ID'], job_name=job.name,
            broker_pid=process.pid, broker_creation_time=process_identity(process.pid))
        # Environment is immutable once the child starts. Pass the trusted
        # record through a gated JSON frame before any native import/admission.
        command = json.dumps({'path': str(record), 'sha256': record_hash})
        # A small test bootstrap sets these from the initial stdin frame.
        (out / 'launch-environment.json').write_text(command, encoding='utf-8')
        process.stdin.write('start\n')
        process.stdin.flush()
        return out, job, process
    yield data, start
    for job, process in started:
        if job.handle:
            job.drain()
            job.close()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()


def test_real_restart_reclaims_only_after_exact_owner_and_descendant_job_exit(native_world):
    data, start = native_world
    out, job, process = start('Original', 'descendant')
    assert eventually(lambda: (out / 'child.pid').exists()), (out / 'error.txt').read_text('utf-8') if (out / 'error.txt').exists() else ''
    coordinator = GpuCoordinator(data / 'gpu.sqlite3', clock=lambda: time.time() + 60)
    snapshot = coordinator.snapshot()
    assert snapshot.owner.pid == process.pid
    (out / 'crash').write_text('fault injection')
    process.wait(timeout=10)
    assert owner_active(snapshot.owner) is False
    assert job.active() > 0  # A real grandchild still belongs to the named Job.
    result = recover_native_resources(coordinator, data)
    assert result['blockers'] and coordinator.snapshot().owner == snapshot.owner
    job.drain()
    job.close()
    restarted = GpuCoordinator(data / 'gpu.sqlite3', clock=lambda: time.time() + 60)
    result = recover_native_resources(restarted, data)
    assert result['reclaimed'] == snapshot.owner_request_id and not result['blockers']
    assert restarted.snapshot().owner is None
    assert restarted.snapshot().generation == snapshot.generation + 1


def test_dead_pending_is_cancelled_with_witness_while_live_owner_is_untouched(native_world):
    data, start = native_world
    first, first_job, first_process = start('Holder')
    assert eventually(lambda: (first / 'granted').exists())
    second, second_job, second_process = start('Waiting')
    coordinator = GpuCoordinator(data / 'gpu.sqlite3')
    assert eventually(lambda: bool(coordinator.snapshot().pending))
    pending = coordinator.snapshot().pending[0]
    holder = coordinator.snapshot().owner
    result = recover_native_resources(coordinator, data)
    assert not result['cancelled_pending'] and coordinator.snapshot().pending == (pending,)
    second_job.drain()
    second_job.close()
    second_process.wait(timeout=10)
    result = recover_native_resources(coordinator, data)
    assert result['cancelled_pending'] == [pending]
    assert coordinator.snapshot().owner == holder and first_process.poll() is None
    (first / 'exit').write_text('release original')
    first_process.wait(timeout=10)
    assert coordinator.snapshot().owner is None


def test_grant_response_crash_before_owner_receipt_uses_pre_enqueued_proof(native_world):
    data, start = native_world
    out, job, process = start('CrashWindow', 'crash_after_grant')
    assert eventually(lambda: process.poll() is not None)
    assert process.returncode == 71
    job.drain()
    job.close()
    coordinator = GpuCoordinator(data / 'gpu.sqlite3', clock=lambda: time.time() + 60)
    before = coordinator.snapshot()
    assert before.owner is not None
    assert not (data / 'gpu-owners' / (before.owner_request_id + '.json')).exists()
    assert recover_native_resources(coordinator, data)['reclaimed'] == before.owner_request_id
    assert coordinator.snapshot().owner is None


def test_legacy_missing_proof_fails_explicitly_without_unlocking_or_inference(native_world):
    data, start = native_world
    coordinator = GpuCoordinator(data / 'gpu.sqlite3', clock=lambda: 1000)
    legacy = Owner('ocr', 'oldLaunch', 'oldInstance', 12345, '123456789012345678')
    coordinator.request('oldNative', 'ocr')
    coordinator.grant('oldNative', legacy, owned=lambda _: True)
    out, job, process = start('Blocked')
    assert eventually(lambda: process.poll() is not None)
    assert process.returncode != 0 and BLOCKED in (out / 'error.txt').read_text('utf-8')
    assert not (out / 'granted').exists()
    assert coordinator.snapshot().owner == legacy
    assert not coordinator.snapshot().pending


def test_live_waiting_phase_then_grant_and_tampered_record_refusal(native_world):
    data, start = native_world
    first, _, first_process = start('PhaseHolder')
    assert eventually(lambda: (first / 'granted').exists())
    second, _, second_process = start('PhaseWaiter')
    assert eventually(lambda: (second / 'phases.jsonl').exists())
    assert '等待 GPU 资源' in (second / 'phases.jsonl').read_text('utf-8')
    (first / 'exit').write_text('release')
    first_process.wait(timeout=10)
    assert eventually(lambda: (second / 'granted').exists())
    phases = [json.loads(row) for row in (second / 'phases.jsonl').read_text('utf-8').splitlines()]
    assert phases[0] == '等待 GPU 资源' and phases[-1] == '加载引擎'
    coordinator = GpuCoordinator(data / 'gpu.sqlite3', clock=lambda: time.time() + 60)
    snapshot = coordinator.snapshot()
    path = data / 'gpu-requests' / (snapshot.owner_request_id + '.json')
    original = path.read_bytes()
    record = json.loads(original)
    record['owner']['creation_time'] = '999999999999999999'
    path.write_text(json.dumps(record), encoding='utf-8')
    assert recover_native_resources(coordinator, data)['blockers']
    assert coordinator.snapshot().owner == snapshot.owner
    path.write_bytes(original)
    (second / 'exit').write_text('release')
    second_process.wait(timeout=10)


def test_committed_request_return_loss_cancels_only_its_own_pending(native_world, tmp_path):
    data, start = native_world
    coordinator = GpuCoordinator(data / 'gpu.sqlite3')
    coordinator.request('unrelatedManual', 'manual')
    script = tmp_path / 'native.py'
    text = script.read_text('utf-8').replace("try:\n    reservation=platform_resources.reserve('ppocr')", '''if mode=='lost_request_return':
    OriginalCoordinator=native.GpuCoordinator
    class LostReturn(OriginalCoordinator):
        def request(self,*args,**kwargs):
            super().request(*args,**kwargs)
            raise OSError('injected local commit return loss')
    native.GpuCoordinator=LostReturn
try:
    reservation=platform_resources.reserve('ppocr')''')
    text = text.replace("    raise\n(out/'granted')", '''    if mode=='lost_request_return':
        while not (out/'exit').exists():time.sleep(.02)
        raise SystemExit(0)
    raise
(out/'granted')''')
    script.write_text(text, encoding='utf-8')
    out, _, process = start('LostReturn', 'lost_request_return')
    assert eventually(lambda: (out / 'error.txt').exists())
    assert process.poll() is None
    assert coordinator.snapshot().pending == ('unrelatedManual',)
    assert coordinator.snapshot().owner is None
    assert not (out / 'granted').exists()
    (out / 'exit').write_text('close failed admission')
    process.wait(timeout=10)
