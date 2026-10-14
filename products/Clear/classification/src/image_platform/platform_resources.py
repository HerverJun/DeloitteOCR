"""One GPU grant per short-lived classification process.

Platform launches must supply a trusted ledger and install the platform
coordinator in this runtime. Partial configuration fails closed. Standalone
launches use the explicit single-legacy-app compatibility lock instead.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

_KEYS = ("WORKBENCH_APP_ID", "WORKBENCH_INSTANCE_ID", "WORKBENCH_LAUNCH_ID",
         "WORKBENCH_GPU_LEDGER")
_ACTIVE = False


class ResourceUnavailable(RuntimeError):
    pass


def platform_mode():
    values = [os.environ.get(key) for key in _KEYS]
    if not any(values):
        return False
    if not all(values) or values[0] != "classification":
        raise ResourceUnavailable("平台 GPU 协调器未完整配置")
    local = os.environ.get("LOCALAPPDATA")
    if not local or not Path(local).is_dir():
        raise ResourceUnavailable("平台未提供旧包兼容锁目录")
    ledger = Path(values[3])
    if not ledger.is_absolute() or not ledger.parent.is_dir():
        raise ResourceUnavailable("平台 GPU 账本位置无效")
    try:
        from workbench_platform.resources.gpu import GpuCoordinator  # noqa: F401
    except ImportError:
        raise ResourceUnavailable("分类运行时缺少平台 GPU 协调器") from None
    return True


def require_gpu_context():
    """Low-level CUDA loaders cannot bypass the managed platform child gate."""
    platform_mode()  # Partial installation configuration must fail even in a child.
    if not _ACTIVE:
        raise ResourceUnavailable("GPU 模型只能在受管资源子进程内加载")


def _identity():
    if os.name != "nt":
        raise ResourceUnavailable("平台 GPU 协调仅支持受管 Windows 进程")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetCurrentProcess.restype = wintypes.HANDLE
    handle = api.GetCurrentProcess()
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                               ctypes.byref(kernel), ctypes.byref(user)):
        raise ResourceUnavailable("无法核实 GPU 申请者创建身份")
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    in_job = wintypes.BOOL()
    if not api.IsProcessInJob(handle, None, ctypes.byref(in_job)) or not in_job.value:
        raise ResourceUnavailable("GPU 申请者不是受管进程")
    return str((created.dwHighDateTime << 32) | created.dwLowDateTime)


@contextmanager
def _legacy_lock():
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "OfflineOCR"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "gpu.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ResourceUnavailable("另一个旧工作台正在使用 GPU") from None
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ResourceUnavailable("另一个旧工作台正在使用 GPU") from None
        yield


class _Standalone:
    def track(self, process, job):
        pass

    def close(self):
        pass


class Reservation:
    def __init__(self, coordinator, lease, owner, creation_time):
        self.coordinator, self.lease, self.owner = coordinator, lease, owner
        self.creation_time = creation_time
        self.children = []
        self.stopping = threading.Event()
        self.heartbeat_failed = False
        self.thread = threading.Thread(target=self._heartbeat, name="Classification GPU heartbeat", daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while not self.stopping.wait(5):
            try:
                self.lease = self.coordinator.heartbeat(self.lease)
            except Exception:
                self.heartbeat_failed = True
                self.stopping.set()

    def track(self, process, job):
        if process is None or job is None or getattr(job, "handle", None) is None:
            raise ResourceUnavailable("GPU 子进程未纳入受管 Job")
        self.children.append((process, job))

    def close(self):
        from workbench_platform.resources.gpu import ResourceEvidence

        self.stopping.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive() or self.heartbeat_failed or _identity() != self.creation_time:
            raise ResourceUnavailable("GPU 持有者身份或心跳状态无法核实")
        if any(process.poll() is None or getattr(job, "handle", None) is not None
               or getattr(job, "drained", False) is not True for process, job in self.children):
            raise ResourceUnavailable("GPU 子进程或受管 Job 尚未清空")
        self.coordinator.release(self.lease, witness=lambda owner: ResourceEvidence(
            exact_identity_checked=owner == self.owner, owner_active=True,
            children_active=False, gpu_released=True))


def reserve(cancelled=None, control=None):
    if not platform_mode():
        return _Standalone()
    from workbench_platform.resources.gpu import GpuCoordinator, Owner, ResourceBusy

    creation_time = _identity()
    owner = Owner("classification", os.environ["WORKBENCH_LAUNCH_ID"],
                  os.environ["WORKBENCH_INSTANCE_ID"], os.getpid(), creation_time)
    coordinator = GpuCoordinator(Path(os.environ["WORKBENCH_GPU_LEDGER"]))
    request_id = "classification-" + uuid.uuid4().hex
    coordinator.request(request_id, "classification")
    granted = False
    try:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise ResourceUnavailable("等待 GPU 时原生任务租约失效")
            if control is not None:
                control()
            try:
                lease = coordinator.grant(request_id, owner,
                                          owned=lambda actual: actual == owner and _identity() == creation_time)
                granted = True
                return Reservation(coordinator, lease, owner, creation_time)
            except ResourceBusy:
                if cancelled is None:
                    time.sleep(.1)
                else:
                    cancelled.wait(.1)
    finally:
        if not granted:
            coordinator.cancel_pending(request_id, "classification")


class _BasicAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
        (name, wintypes.DWORD) for name in (
            "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class OwnedJob:
    """Kill only the Job created here; prove zero descendants before release."""

    def __init__(self):
        if os.name != "nt":
            raise ResourceUnavailable("GPU 进程隔离需要 Windows Job")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.handle = self.api.CreateJobObjectW(None, None)
        self.drained = False
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION layout follows the Win32 ABI.
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            process.kill()
            process.wait(timeout=10)
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self):
        record, size = _BasicAccounting(), wintypes.DWORD()
        if not self.handle or not self.api.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(record), ctypes.sizeof(record), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return record.ActiveProcesses

    def drain(self, timeout=15):
        if self.active_processes() and not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while self.active_processes():
            if time.monotonic() >= deadline:
                raise TimeoutError("分类受管 GPU 子进程未退出")
            time.sleep(.05)
        self.drained = True

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _run_gated(payload, reservation, control=None, lost=None):
    """Child waits on stdin until after assignment to the owned Job."""
    command = [sys.executable, "-m", "image_platform.platform_resources"]
    with OwnedJob() as job:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            reservation.track(process, job)
            job.assign(process)
            message = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                stdout, stderr = process.communicate(input=message, timeout=1)
            except subprocess.TimeoutExpired:
                while True:
                    if lost is not None and lost.is_set():
                        raise ResourceUnavailable("原生任务租约失联，终止自有 GPU 子进程")
                    if getattr(reservation, "heartbeat_failed", False):
                        raise ResourceUnavailable("共享 GPU 租约心跳失联，终止自有 GPU 子进程")
                    if control is not None:
                        control()
                    try:
                        stdout, stderr = process.communicate(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            job.drain()
            process.wait(timeout=15)


def run_isolated(payload, *, cancelled=None, control=None):
    reservation = reserve(cancelled, control)
    try:
        with _legacy_lock():
            return _run_gated(payload, reservation, control=control, lost=cancelled)
    finally:
        reservation.close()


def child_main():
    """Only a parent-assigned, gated child executes GPU work."""
    global _ACTIVE
    raw = sys.stdin.buffer.readline(1024 * 1024 + 1)
    if not raw or len(raw) > 1024 * 1024:
        raise ResourceUnavailable("GPU 子进程未收到受管启动载荷")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("mode") not in {"job", "predict", "doctor"}:
        raise ResourceUnavailable("GPU 子进程载荷无效")
    _ACTIVE = True
    try:
        if payload["mode"] == "job":
            from .settings import Settings
            from .store import Store
            from .worker import run_job

            store = Store(Settings(Path(payload["data_dir"])))
            job = store.job(payload["job_id"])
            if job["lease_token"] != payload["lease_token"]:
                raise ResourceUnavailable("分类任务租约已更替")
            run_job(store, job)
        elif payload["mode"] == "predict":
            from .predict import predict_images
            result = predict_images(payload["package"], payload["images"], "cuda",
                                    payload["auto_accept"])
            rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
            if payload.get("output"):
                Path(payload["output"]).write_text(rendered + "\n", encoding="utf-8")
            print(rendered)
        else:
            import onnxruntime as ort
            import torch
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()
            result = {"torch": torch.__version__, "cuda_available": torch.cuda.is_available(),
                      "compiled_cuda": torch.version.cuda, "ort": ort.__version__,
                      "providers": ort.get_available_providers()}
            if torch.cuda.is_available():
                result.update(gpu=torch.cuda.get_device_name(0),
                              tensor_check=float(torch.ones(4, device="cuda").sum().item()))
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        _ACTIVE = False


if __name__ == "__main__":
    from image_platform.platform_resources import child_main as _canonical_child_main
    _canonical_child_main()
