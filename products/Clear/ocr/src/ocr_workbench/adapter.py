"""Single-GPU EngineAdapter contract for resident, isolated Windows workers."""

import json
import hashlib
import msvcrt
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from ocr_workbench.processes import ProcessJob
from ocr_workbench.engine_host import publish
from ocr_workbench.atomic_files import read_json
from ocr_workbench.errors import EngineFailure, EngineTimeout


WORKER_BOOTSTRAP = '''import hashlib,json,os,runpy,sys
from pathlib import Path
source=Path(sys.argv.pop(1)).resolve()
if os.environ.get('CLEAR_OCR_WORKER_SOURCE_MANIFEST'):
    manifest=Path(os.environ['CLEAR_OCR_WORKER_SOURCE_MANIFEST']).resolve()
    if source.drive.upper()!='F:' or manifest.drive.upper()!='F:':
        raise ValueError('Worker source escaped the local F drive')
    payload=manifest.read_bytes()
    if hashlib.sha256(payload).hexdigest()!=os.environ['CLEAR_OCR_WORKER_BINDING_SHA256']:
        raise ValueError('Worker binding changed before child import')
    binding=json.loads(payload)
    if Path(binding['worker_source']).resolve()!=source or binding.get('app_id')!='ocr':
        raise ValueError('Worker source differs from verified child binding')
    declared=set()
    for item in binding['worker_source_files']:
        relative=Path(item['path']);member=(source/relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or not member.is_relative_to(source) or item['path'] in declared:
            raise ValueError('Worker source declaration is invalid')
        declared.add(item['path'])
        if hashlib.sha256(member.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Worker source changed before child import')
    actual={member.relative_to(source).as_posix() for member in (source/'ocr_workbench').rglob('*') if member.is_file()}
    if actual!=declared:
        raise ValueError('Worker source contains missing or undeclared payload')
sys.path.insert(0,str(source))
runpy.run_module('ocr_workbench.engine_host',run_name='__main__')
'''


class Cancelled(Exception):
    pass


def loading_timeout(environ=None):
    """Loading has a separate bounded budget; inference remains unchanged."""
    environ = os.environ if environ is None else environ
    value = environ.get('CLEAR_OCR_LOAD_TIMEOUT_SECONDS', '240')
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        raise ValueError('CLEAR_OCR_LOAD_TIMEOUT_SECONDS must be an integer from 1 to 1800') from None
    if str(timeout) != str(value) or not 1 <= timeout <= 1800:
        raise ValueError('CLEAR_OCR_LOAD_TIMEOUT_SECONDS must be an integer from 1 to 1800')
    return timeout


def configured_worker_source(environ=None):
    """Only an explicit F-drive binding can override the packaged worker."""
    environ = os.environ if environ is None else environ
    if not environ.get('CLEAR_OCR_WORKER_SOURCE'):
        return None
    if not all(environ.get(key) for key in ('CLEAR_OCR_WORKER_SOURCE_MANIFEST', 'CLEAR_OCR_WORKER_BINDING_SHA256')):
        raise ValueError('OCR worker source requires a complete verified binding')
    source_path = Path(environ['CLEAR_OCR_WORKER_SOURCE']).absolute()
    manifest_path = Path(environ['CLEAR_OCR_WORKER_SOURCE_MANIFEST']).absolute()
    source, manifest = source_path.resolve(), manifest_path.resolve()
    if any(path.drive.upper() != 'F:' for path in (source_path, manifest_path, source, manifest)):
        raise ValueError('OCR worker source and manifest must remain on local F drive')
    data = manifest.read_bytes()
    if hashlib.sha256(data).hexdigest() != environ['CLEAR_OCR_WORKER_BINDING_SHA256']:
        raise ValueError('OCR worker binding identity changed')
    binding = json.loads(data)
    if binding.get('app_id') != 'ocr' or Path(binding['worker_source']).resolve() != source:
        raise ValueError('OCR worker source differs from its verified binding')
    declared = set()
    for item in binding['worker_source_files']:
        relative = Path(item['path'])
        member = (source / relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or not member.is_relative_to(source) or item['path'] in declared:
            raise ValueError('OCR worker source declaration is invalid')
        declared.add(item['path'])
        if hashlib.sha256(member.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('OCR worker source payload changed')
    actual = {member.relative_to(source).as_posix() for member in (source / 'ocr_workbench').rglob('*') if member.is_file()}
    if actual != declared or not {'ocr_workbench/engine_host.py', 'ocr_workbench/worker.py', 'ocr_workbench/adapter.py'} <= actual:
        raise ValueError('OCR worker source has missing or undeclared modules')
    return source


class EngineLoadTimeout(EngineTimeout):
    """The worker did not publish readiness within its startup deadline."""

    phase = 'engine_load'

    def __init__(self, engine, timeout_seconds, ipc, last_stage=None):
        self.engine = engine
        self.timeout_seconds = timeout_seconds
        self.ipc = Path(ipc)
        self.last_stage = last_stage
        super().__init__('load')


def runtime_path(runtime, system_root):
    site = runtime.parent / 'Lib' / 'site-packages' / 'nvidia'
    bundled = sorted(path for path in site.glob('*/bin') if path.is_dir())
    return os.pathsep.join([str(runtime.parent), *(str(path) for path in bundled),
                            str(Path(system_root) / 'System32')])


def check_recognition_result(result, *, allow_empty=False):
    """An empty, completed region is reviewable; a worker error never is."""
    if result.get('status') != 'success':
        raise RuntimeError('模型执行失败，未生成成功结果')
    empty = not result.get('text', '').strip()
    if empty and not allow_empty:
        raise RuntimeError('模型未返回可用文字，未生成成功结果')
    if empty:
        result['content_status'] = 'no_text_detected'
    return result


class EngineAdapter:
    def __init__(self, bundle, engine, session_root, *, worker_source=None):
        self.load_timeout_seconds = loading_timeout()
        self.bundle = Path(bundle).resolve()
        self.engine = engine
        self.worker_source = Path(worker_source).resolve() if worker_source else (configured_worker_source() or Path(__file__).resolve().parents[1])
        if engine == 'mineru_pro' and self.worker_source is None and os.environ.get('CLEAR_MINERU_PRO_PYTHON'):
            self.worker_source = Path(__file__).resolve().parents[1]
        from ocr_workbench.mineru_pro import engine_catalog
        self.capabilities = (
            {engine: True}
            if engine in {"dewarp", "geometry"}
            else engine_catalog(self.bundle)[engine]["capabilities"]
        )
        self.ipc = Path(session_root) / uuid.uuid4().hex
        self.ipc.mkdir(parents=True)
        self.process = None
        self.job = None
        self.log = None
        self.gpu_lock = None
        self.reservation = None
        self.cancelled = threading.Event()
        self.load_seconds = 0
        self.process_start_seconds = 0
        self.load_wait_seconds = 0
        self.load_progress = None
        self.progress_callback = None
        self.ready = False
        self.recognition_timeout = 900
        self.geometry_provider = 'paddle'

    def configure_geometry(self, provider):
        if self.engine != 'geometry' or self.process is not None:
            raise ValueError('Geometry provider must be selected before worker loading')
        if provider not in ('paddle', 'tableformer-raw', 'rapidtable', 'context'):
            raise ValueError('Unknown geometry provider')
        self.geometry_provider = provider
        # Use the running application's reviewed source, including its protocol.
        if self.worker_source is None:
            self.worker_source = Path(__file__).resolve().parents[1]

    def load(self):
        if self.cancelled.is_set():
            raise Cancelled()
        # The raw RapidTable candidate is explicitly CPU-only. Shared context
        # OCR is a separate EngineAdapter session and still obtains a grant.
        gpu_required = not (self.engine == 'geometry' and self.geometry_provider == 'rapidtable')
        if gpu_required:
            from ocr_workbench.platform_resources import reserve
            self.reservation = reserve(self.engine, self.cancelled)
        try:
            self._load()
        except BaseException:
            self.unload()
            raise

    def _load(self):
        configured_source = configured_worker_source()
        if configured_source is not None and self.worker_source != configured_source:
            raise ValueError('Worker source differs from the verified private binding')
        gpu_required = self.reservation is not None or not (
            self.engine == 'geometry' and self.geometry_provider == 'rapidtable')
        if gpu_required:
            self._lock_legacy_gpu()
        runtime_name = 'ppocr' if self.engine in {'dewarp', 'geometry'} else self.engine
        if self.engine == 'geometry' and self.geometry_provider in ('tableformer-raw', 'rapidtable'):
            from ocr_workbench.geometry_provider_config import provider_config
            runtime_name = provider_config(self.geometry_provider)['runtime']
        runtime = self.bundle / 'runtimes' / runtime_name / 'python.exe'
        if self.engine == 'mineru_pro' and os.environ.get('CLEAR_MINERU_PRO_PYTHON'):
            requested_runtime = Path(os.environ['CLEAR_MINERU_PRO_PYTHON']).absolute()
            runtime = requested_runtime.resolve()
            if requested_runtime.drive.upper() != 'F:' or runtime.drive.upper() != 'F:':
                raise ValueError('MinerU Pro runtime must remain on local F drive')
        environment = os.environ.copy()
        environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
        environment["PATH"] = runtime_path(runtime, os.environ["SystemRoot"])
        for name in list(environment):
            if name.upper() in {"PYTHONPATH", "PYTHONHOME", "CUDA_HOME", "CUDA_PATH"} or name.startswith("CUDA_PATH_V"):
                del environment[name]
        self.job = ProcessJob()
        self.log = (self.ipc / "engine.log").open("w", encoding="utf-8")
        command = [str(runtime), "-B", "-X", "utf8", "-I"]
        if self.worker_source:
            command += ['-c', WORKER_BOOTSTRAP, str(self.worker_source)]
        else:
            command += ["-m", "ocr_workbench.engine_host"]
        command += ["--bundle", str(self.bundle), "--engine", self.engine, "--ipc", str(self.ipc)]
        if self.engine == 'geometry':
            command += ['--geometry-provider', self.geometry_provider]
        process_started = time.perf_counter()
        self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT,
                                        env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        self.process_start_seconds = time.perf_counter() - process_started
        self.job.assign(self.process)
        if self.reservation:
            self.reservation.track(self.process, self.job)
        load_wait_started = time.perf_counter()
        try:
            status = self.wait_for(self.ipc / "status.json", self.load_timeout_seconds, stage='load')
        except TimeoutError as error:
            stage = None
            try:
                stage = read_json(self.ipc / 'loading.json').get('stage')
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            raise EngineLoadTimeout(self.engine, self.load_timeout_seconds, self.ipc, stage) from error
        self.load_wait_seconds = time.perf_counter() - load_wait_started
        if status["status"] != "ready":
            raise EngineFailure('engine_load_failed', 'load', '引擎权重加载失败；尚未开始识别。',
                                user_action='查看该尝试的引擎日志、模型安装和可用资源；修复后再明确继续。')
        self.load_seconds = status["load_seconds"]
        self.ready = True

    def _lock_legacy_gpu(self):
        lock_path = Path(os.environ["LOCALAPPDATA"]) / "OfflineOCR/gpu.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.gpu_lock = lock_path.open("a+b")
        if self.gpu_lock.tell() == 0:
            self.gpu_lock.write(b"0")
            self.gpu_lock.flush()
        self.gpu_lock.seek(0)
        try:
            msvcrt.locking(self.gpu_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.gpu_lock.close()
            self.gpu_lock = None
            raise RuntimeError("另一个 OCR 实例正在使用 GPU，请稍后重试")

    def wait_for(self, path, timeout, *, stage='inference'):
        deadline = time.monotonic() + timeout
        next_progress_check = 0
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise Cancelled("任务已取消")
            if path.exists():
                return read_json(path)
            if stage == 'load' and time.monotonic() >= next_progress_check:
                next_progress_check = time.monotonic() + 1
                progress_path = self.ipc / 'load-progress.json'
                if progress_path.is_file():
                    progress = read_json(progress_path)
                    if progress != self.load_progress:
                        self.load_progress = progress
                        if self.progress_callback is not None:
                            self.progress_callback(progress)
            if self.process is None or self.process.poll() is not None:
                if self.cancelled.is_set():
                    raise Cancelled("任务已取消")
                raise EngineFailure('engine_worker_exited', stage, '引擎进程异常退出。',
                    accepted_write=stage != 'load', user_action='查看该尝试的引擎日志；先核实原请求与进程状态，再明确恢复或创建新尝试。')
            self.cancelled.wait(0.05)
        raise EngineTimeout(stage)

    def recognize(self, image, output, geometry_request=None, *, allow_empty=False, recognition_mode='page'):
        request_started = time.perf_counter()
        from ocr_workbench.recognition_modes import validate_recognition_mode
        validate_recognition_mode(self.engine, recognition_mode)
        if recognition_mode == 'local_text' and geometry_request is not None:
            raise ValueError('Local text cannot use a geometry request')
        key = uuid.uuid4().hex
        response = self.ipc / "response.json"
        response.unlink(missing_ok=True)
        request = {"id": key, "image": str(image), "output": str(output), "geometry_request": geometry_request}
        if recognition_mode != 'page':
            request['recognition_mode'] = recognition_mode
        publish(self.ipc / "request.json", request)
        reply = self.wait_for(response, getattr(self, 'recognition_timeout', 900))
        if reply.get('id') != key:
            raise EngineFailure('engine_response_unknown', 'inference', '引擎响应身份无法核实；原请求不得自动重发。',
                accepted_write=True, user_action='保留原尝试身份，检查回执和输出；核实处理结果后再继续。')
        if reply['status'] != 'success':
            raise EngineFailure('engine_inference_failed', 'inference', '引擎已接受请求，但识别执行失败。',
                accepted_write=True, user_action='查看该尝试的引擎日志；按具体原因修复或选择用户允许的后备引擎。')
        if self.engine == "geometry":
            result = read_json(Path(output) / 'geometry.json')
            if result.get('status') != 'success':
                raise RuntimeError('表格几何组件未返回有效结果')
            return result
        if self.engine == "dewarp":
            result = json.loads(
                (Path(output) / "dewarp.json").read_text(encoding="utf-8")
            )
            if result.get("status") != "success" or not Path(result["image"]).is_file():
                raise RuntimeError("去弯曲未生成有效图片")
            return result
        result = json.loads((Path(output) / "result.json").read_text(encoding="utf-8"))
        check_recognition_result(result, allow_empty=allow_empty)
        if recognition_mode == 'local_text':
            raw = result.get('raw')
            if (result.get('recognition_mode') != 'local_text'
                    or not isinstance(raw, dict)
                    or raw.get('recognition_mode') != 'local_text'
                    or raw.get('layout_applied') is not False
                    or raw.get('formatter_applied') is not False
                    or result.get('tables') != []):
                raise RuntimeError('局部文字识别回执与请求模式不一致，结果未采用')
        result["engine_session"] = {
            "id": self.ipc.name,
            "load_seconds": self.load_seconds,
            "pid": self.process.pid,
            'process_start_seconds': self.process_start_seconds,
            'load_wait_seconds': self.load_wait_seconds,
            'load_timeout_seconds': self.load_timeout_seconds,
            'request_response_seconds': time.perf_counter() - request_started,
        }
        return result

    def recognize_local_text(self, image, output, *, allow_empty=False):
        """Explicit whole-crop text request; never inferred from geometry payloads."""
        return self.recognize(image, output, allow_empty=allow_empty, recognition_mode='local_text')

    def cancel(self):
        self.cancelled.set()

    def unload(self):
        self.ready = False
        try:
            if self.process is not None and self.process.poll() is None:
                if not self.cancelled.is_set():
                    publish(self.ipc / "stop.json", {})
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                if self.job:
                    self.job.drain()
                self.process.wait(timeout=15)
        finally:
            if self.job:
                if not self.job.drained:
                    try:
                        self.job.drain()
                    except Exception:
                        pass  # Reservation.close refuses unproven Job cleanup.
                self.job.close()
                self.job = None
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if self.log:
                self.log.close()
                self.log = None
            if self.gpu_lock:
                self.gpu_lock.close()
                self.gpu_lock = None
            if self.reservation:
                self.reservation.close()
                self.reservation = None
