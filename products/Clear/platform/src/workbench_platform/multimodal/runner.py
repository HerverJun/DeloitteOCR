"""One gated, owned process per GPU lease; waiting is handled by the dispatcher."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from workbench_platform.resources.gpu import Owner, ResourceBusy, ResourceEvidence, ReleaseUnconfirmed
from workbench_platform.resources.recovery import WindowsJob, local_path, persist_owner, process_identity

PROTOCOL = 'clear-gpu-worker-v1'
OOM_MARKERS = ('out of memory', 'cuda_error_out_of_memory', 'resourceexhaustederror', 'cublas_status_alloc_failed')


def worker_environment(output, runtime, *, gpu=False, device_index=0):
    output, runtime = local_path(output), local_path(runtime)
    env = os.environ.copy()
    directories = ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'HF_HOME', 'MODELSCOPE_CACHE',
                   'TORCH_HOME', 'MPLCONFIGDIR', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
                   'NUMBA_CACHE_DIR', 'PADDLE_PDX_CACHE_HOME', 'PADDLEOCR_HOME', 'CUDA_CACHE_PATH',
                   'TORCHINDUCTOR_CACHE_DIR', 'TRITON_CACHE_DIR', 'PIP_CACHE_DIR', 'JUPYTER_CONFIG_DIR')
    for name in directories:
        directory = local_path(output / 'runtime' / name.lower())
        directory.mkdir(parents=True, exist_ok=True)
        env[name] = str(directory)
    native_root = local_path(Path(env.get('CLEAR_NATIVE_TEMP_ROOT', 'F:/OCR/temp/manual')))
    if not str(native_root).isascii():
        raise ValueError('Native temporary root must be an ASCII path on F:')
    temporary = local_path(native_root / hashlib.sha256(str(output).encode()).hexdigest()[:20])
    temporary.mkdir(parents=True, exist_ok=True)
    for name in ('TEMP', 'TMP', 'TMPDIR', 'CLEAR_PADDLE_TEMP'):
        env[name] = str(temporary)
    for name in list(env):
        if name.upper() in {'PYTHONHOME', 'PYTHONPATH', 'CUDA_HOME', 'CUDA_PATH'} or name.startswith('CUDA_PATH_V'):
            env.pop(name, None)
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1',
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               PYTHONIOENCODING='utf-8', OMP_NUM_THREADS='2', MKL_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2',
               CUDA_VISIBLE_DEVICES=str(device_index) if gpu else '')
    env['PATH'] = os.pathsep.join([str(runtime.parent), os.path.join(os.environ.get('SystemRoot', 'C:/Windows'), 'System32')])
    return env


@dataclass(frozen=True)
class Outcome:
    status: str
    returncode: int | None = None
    error: str | None = None
    elapsed_seconds: float = 0


def read_event(output, epoch, allowed_checkpoint):
    path = output / 'runner-event.json'
    if not path.is_file():
        return None
    try:
        event = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None
    if event.get('worker_epoch') != epoch:
        return None
    if event.get('event') != 'chunk_committed':
        return event
    if type(event.get('chunk_index')) is not int or event['chunk_index'] < 0:
        raise ValueError('Invalid audio chunk receipt')
    receipt = event.get('checkpoint', {})
    target = local_path(output / receipt.get('path', ''))
    roots = (output, local_path(allowed_checkpoint))
    if not any(target.is_relative_to(root) for root in roots) or not target.is_file():
        raise ValueError('Audio checkpoint escapes its assigned directory')
    with target.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != receipt.get('sha256'):
            raise ValueError('Audio checkpoint changed before commit')
    return event


class WorkerRunner:
    def __init__(self, coordinator, ownership_root, *, job_factory=WindowsJob, popen=subprocess.Popen,
                 identity=process_identity, poll_seconds=.2):
        self.coordinator, self.ownership_root = coordinator, local_path(ownership_root)
        self.job_factory, self.popen, self.identity = job_factory, popen, identity
        self.poll_seconds = poll_seconds

    def run(self, command, output, env, request, *, request_id=None, cancelled=lambda: False,
            progress=lambda value: None, committed=lambda event: None, process_started=lambda process: None,
            timeout=7200):
        output = local_path(output)
        output.mkdir(parents=True, exist_ok=True)
        epoch = request['runner']['worker_epoch']
        gpu = request['options']['device'] == 'cuda'
        if request['runner'].get('protocol') != PROTOCOL:
            raise ValueError('Worker gate protocol is required')
        if gpu and (self.coordinator is None or not request_id):
            raise ValueError('GPU worker has no shared ledger request')
        started = time.monotonic()
        process = job = lease = owner = None
        sent_stop, last_chunk, last_heartbeat = False, -1, started
        last_progress, previous_progress, stop_at = started, None, None
        outcome = None
        cleanup_error = None
        if (output/'worker.log').exists():
            os.replace(output/'worker.log',output/('worker-previous-'+uuid.uuid4().hex+'.log'))
        with (output / 'worker.log').open('wb') as log:
            try:
                job = self.job_factory()
                process = self.popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                     cwd=output, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                process._clear_trace_path=output/'commands.jsonl'
                try:
                    job.assign(process)
                except BaseException:
                    process.kill()  # Only this new, still-gated child.
                    process.wait(timeout=15)
                    raise
                process_started(process)
                if gpu:
                    owner = Owner('manual', request['runner']['launch_id'], request['runner']['instance_id'],
                                  process.pid, self.identity(process.pid))
                    try:
                        lease = self.coordinator.grant(request_id, owner, owned=lambda actual:
                            actual == owner and job.contains(process) and self.identity(process.pid) == owner.creation_time)
                    except ResourceBusy:
                        outcome = Outcome('waiting')
                    if lease is not None:
                        persist_owner(self.ownership_root, lease, job)
                if outcome is None:
                    self.send(process, 'start', epoch)
                    while process.poll() is None:
                        if cancelled():
                            self.send(process, 'stop', epoch)
                            outcome = Outcome('cancelled')
                            break
                        if time.monotonic() - last_progress > timeout:
                            outcome = Outcome('failed', error='分析超过当前模型的无进展时限')
                            break
                        if stop_at is not None and time.monotonic()-stop_at>15:
                            # The checkpoint is already committed. Drain the
                            # unresponsive owned worker before yielding its lease.
                            outcome = Outcome('yielded')
                            break
                        if lease is not None and time.monotonic() - last_heartbeat >= 5:
                            lease = self.coordinator.heartbeat(lease)
                            last_heartbeat = time.monotonic()
                        event = read_event(output, epoch, request['runner'].get('checkpoint_root', output))
                        if event and event.get('event') == 'chunk_committed' and event['chunk_index'] > last_chunk:
                            committed(event)
                            last_chunk = event['chunk_index']
                            last_progress = time.monotonic()
                            if cancelled():
                                self.send(process,'stop',epoch)
                                outcome=Outcome('cancelled')
                                break
                            if gpu and self.coordinator.snapshot().pending:
                                self.send(process, 'stop', epoch)
                                sent_stop = True
                                stop_at = time.monotonic()
                            else:
                                self.send(process, 'continue', epoch)
                        status = output / 'progress.json'
                        if status.is_file():
                            try:
                                value = json.loads(status.read_text('utf-8'))
                                if value.get('worker_epoch') == epoch:
                                    if value != previous_progress:
                                        last_progress, previous_progress = time.monotonic(), value
                                    progress(value)
                            except (ValueError, OSError):
                                pass
                        time.sleep(self.poll_seconds)
                    if outcome is None:
                        code = process.returncode
                        tail = (output / 'worker.log').read_text('utf-8', errors='replace')[-5000:]
                        if sent_stop and code == 75:
                            outcome = Outcome('yielded', code)
                        elif code:
                            outcome = Outcome('oom' if any(word in tail.lower() for word in OOM_MARKERS) else 'failed',
                                              code, '模型执行失败：' + tail[-2400:])
                        else:
                            outcome = Outcome('succeeded', code)
            except Exception as error:
                outcome = Outcome('failed', error=str(error)[:2400])
            finally:
                try:
                    if job is not None:
                        job.drain()
                    if process is not None:
                        process.wait(timeout=15)
                    if lease is not None:
                        if not job.drained or job.active() or process.poll() is None:
                            raise ReleaseUnconfirmed('GPU worker tree exit was not confirmed')
                        self.coordinator.release(lease, witness=lambda actual: ResourceEvidence(
                            actual == owner, False, False, True))
                    elif gpu and outcome is not None and outcome.status!='waiting':
                        if request_id in self.coordinator.snapshot().pending:
                            self.coordinator.cancel_pending(request_id,'manual')
                except Exception as error:
                    cleanup_error = str(error)
                finally:
                    if process is not None and process.stdin is not None:
                        process.stdin.close()
                    if job is not None:
                        try:job.close()
                        except Exception as error:cleanup_error=str(error)
        if cleanup_error:
            return Outcome('needs_verification', error='GPU/子进程清理凭证不可确认：' + cleanup_error,
                           elapsed_seconds=time.monotonic() - started)
        return Outcome(outcome.status, outcome.returncode, outcome.error, time.monotonic() - started)

    @staticmethod
    def send(process, command, epoch):
        frame=(json.dumps({'command':command,'worker_epoch':epoch})+'\n').encode('utf-8')
        def trace(phase,**extra):
            path=getattr(process,'_clear_trace_path',None)
            if path is None:return
            value={'phase':phase,'command':command,'worker_epoch':epoch,'pid':process.pid,
                'frame_repr':repr(frame),'frame_hex':frame.hex(),'at':time.time(),
                'runner_file':__file__,'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),**extra}
            with local_path(path).open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(value,ensure_ascii=False)+'\n')
                stream.flush()
                os.fsync(stream.fileno())
        try:
            trace('before_write')
            process.stdin.write(frame)
            process.stdin.flush()
            trace('flushed')
        except (OSError, BrokenPipeError) as error:
            trace('write_failed',error=type(error).__name__)
            if process.poll() is None:
                raise
