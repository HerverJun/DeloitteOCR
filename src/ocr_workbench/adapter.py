"""Single-GPU EngineAdapter contract for resident, isolated Windows workers."""

import json
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


class Cancelled(Exception):
    pass


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
        self.bundle = Path(bundle).resolve()
        self.engine = engine
        self.worker_source = Path(worker_source).resolve() if worker_source else None
        self.capabilities = (
            {engine: True}
            if engine in {"dewarp", "geometry"}
            else json.loads(
                (self.bundle / "config/engines.json").read_text(encoding="utf-8")
            )[engine]["capabilities"]
        )
        self.ipc = Path(session_root) / uuid.uuid4().hex
        self.ipc.mkdir(parents=True)
        self.process = None
        self.job = None
        self.log = None
        self.gpu_lock = None
        self.cancelled = threading.Event()
        self.load_seconds = 0
        self.ready = False
        self.geometry_provider = 'paddle'

    def configure_geometry(self, provider):
        if self.engine != 'geometry' or self.process is not None:
            raise ValueError('Geometry provider must be selected before worker loading')
        if provider not in ('paddle', 'tableformer-raw', 'rapidtable', 'context'):
            raise ValueError('Unknown geometry provider')
        self.geometry_provider = provider
        # Use the running application's reviewed source, including its protocol.
        self.worker_source = Path(__file__).resolve().parents[1]

    def load(self):
        if self.cancelled.is_set():
            raise Cancelled()
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
        runtime_name = 'ppocr' if self.engine in {'dewarp', 'geometry'} else self.engine
        if self.engine == 'geometry' and self.geometry_provider in ('tableformer-raw', 'rapidtable'):
            from ocr_workbench.geometry_provider_config import provider_config
            runtime_name = provider_config(self.geometry_provider)['runtime']
        runtime = (
            self.bundle
            / "runtimes"
            / runtime_name
            / "python.exe"
        )
        environment = os.environ.copy()
        environment["PATH"] = (
            str(runtime.parent)
            + os.pathsep
            + str(Path(os.environ["SystemRoot"]) / "System32")
        )
        for name in list(environment):
            if name.upper() in {
                "PYTHONPATH",
                "PYTHONHOME",
                "CUDA_HOME",
                "CUDA_PATH",
            } or name.startswith("CUDA_PATH_V"):
                del environment[name]
        self.job = ProcessJob()
        self.log = (self.ipc / "engine.log").open("w", encoding="utf-8")
        try:
            command = [
                    str(runtime),
                    "-B",
                    "-X",
                    "utf8",
                    "-I",
                ]
            if self.worker_source:
                # Evaluation runs can use reviewed workspace code with an
                # immutable bundle runtime, without modifying the shipped copy.
                command += ["-c", "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('ocr_workbench.engine_host',run_name='__main__')", str(self.worker_source)]
            else:
                command += ["-m", "ocr_workbench.engine_host"]
            command += [
                    "--bundle",
                    str(self.bundle),
                    "--engine",
                    self.engine,
                    "--ipc",
                    str(self.ipc),
                ]
            if self.engine == 'geometry':
                command += ['--geometry-provider', self.geometry_provider]
            self.process = subprocess.Popen(
                command,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.job.assign(self.process)
            status = self.wait_for(self.ipc / "status.json", 240)
            if status["status"] != "ready":
                raise RuntimeError(status.get("message", "引擎加载失败"))
            self.load_seconds = status["load_seconds"]
            self.ready = True
        except BaseException:
            self.unload()
            raise

    def wait_for(self, path, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise Cancelled("任务已取消")
            if path.exists():
                return read_json(path)
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError(f'引擎异常退出，请查看 {self.ipc / "engine.log"}')
            self.cancelled.wait(0.05)
        raise TimeoutError("识别超时，请缩小图片或换用其他引擎")

    def recognize(self, image, output, geometry_request=None, *, allow_empty=False):
        key = uuid.uuid4().hex
        response = self.ipc / "response.json"
        response.unlink(missing_ok=True)
        publish(
            self.ipc / "request.json",
            {"id": key, "image": str(image), "output": str(output), "geometry_request": geometry_request},
        )
        reply = self.wait_for(response, 900)
        if reply.get("id") != key or reply["status"] != "success":
            raise RuntimeError(reply.get("message", "识别失败"))
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
        result["engine_session"] = {
            "id": self.ipc.name,
            "load_seconds": self.load_seconds,
            "pid": self.process.pid,
        }
        return result

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
                    self.job.close()
                self.process.wait(timeout=15)
        finally:
            if self.job:
                self.job.close()
                self.job = None
            if self.log:
                self.log.close()
                self.log = None
            if self.gpu_lock:
                self.gpu_lock.close()
                self.gpu_lock = None
