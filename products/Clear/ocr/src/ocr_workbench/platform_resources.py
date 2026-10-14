"""OCR GPU entry gate: platform ledger or explicit standalone compatibility.

The platform installation must ship workbench_platform into the service/CLI
runtime and provide a supervisor-owned ledger path. A partial platform launch
configuration fails closed; it never silently falls back to the old OCR lock.
The old per-user lock remains necessary for independently launched old kits.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import threading
import uuid


_PLATFORM_KEYS = ("WORKBENCH_APP_ID", "WORKBENCH_INSTANCE_ID",
                  "WORKBENCH_LAUNCH_ID", "WORKBENCH_GPU_LEDGER")


class ResourceUnavailable(RuntimeError):
    pass


def platform_mode():
    values = [os.environ.get(key) for key in _PLATFORM_KEYS]
    if not any(values):
        return False
    if not all(values) or values[0] != "ocr":
        raise ResourceUnavailable("平台 GPU 协调器未完整配置，禁止加载模型")
    if not os.environ.get('LOCALAPPDATA') or not Path(os.environ['LOCALAPPDATA']).is_dir():
        raise ResourceUnavailable("平台未配置与旧 OCR 包共享的 GPU 兼容锁目录")
    ledger = Path(values[3])
    if not ledger.is_absolute() or not ledger.parent.is_dir():
        raise ResourceUnavailable("GPU 协调账本位置无效")
    try:
        from workbench_platform.resources.gpu import GpuCoordinator  # noqa: F401
    except ImportError:
        raise ResourceUnavailable("OCR 运行时缺少平台 GPU 协调器") from None
    return True


def _identity():
    """Kernel process creation identity, not a wall-clock/PID approximation."""
    if os.name != "nt":
        raise ResourceUnavailable("平台 GPU 协调器仅支持受管 Windows 进程")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetCurrentProcess.restype = wintypes.HANDLE
    handle = api.GetCurrentProcess()
    api.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME)]
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                               ctypes.byref(kernel), ctypes.byref(user)):
        raise ResourceUnavailable("无法核实进程创建身份")
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    in_job = wintypes.BOOL()
    if not api.IsProcessInJob(handle, None, ctypes.byref(in_job)) or not in_job.value:
        raise ResourceUnavailable("GPU 申请者不是受管进程")
    return str((created.dwHighDateTime << 32) | created.dwLowDateTime)


class _Standalone:
    """Compatibility only. Existing OCR locks remain at each legacy entry."""

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
        self.thread = threading.Thread(target=self._heartbeat, name="OCR GPU lease heartbeat", daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while not self.stopping.wait(5):
            try:
                self.lease = self.coordinator.heartbeat(self.lease)
            except Exception:
                # Ownership remains reserved in the ledger. The next process
                # boundary must fail closed; no automatic second owner exists.
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
        # The caller must close/drain its own Job and wait for the process. A
        # surviving child leaves the grant reserved for explicit recovery.
        if self.thread.is_alive() or self.heartbeat_failed or _identity() != self.creation_time:
            raise ResourceUnavailable("GPU 持有者身份或心跳状态无法核实")
        if any(process.poll() is None or getattr(job, "handle", None) is not None
               or getattr(job, "drained", False) is not True
               for process, job in self.children):
            raise ResourceUnavailable("GPU 子进程或受管 Job 仍未清理")
        self.coordinator.release(self.lease, witness=lambda owner: ResourceEvidence(
            exact_identity_checked=owner == self.owner,
            owner_active=True,
            children_active=False,
            gpu_released=True,
        ))


def reserve(kind, cancelled=None):
    """Enqueue before GPU load; a cancelled waiter removes only its own request."""
    if not platform_mode():
        return _Standalone()
    from workbench_platform.resources.gpu import GpuCoordinator, Owner, ResourceBusy

    if not isinstance(kind, str) or not kind or len(kind) > 60:
        raise ValueError("Invalid resource kind")
    cancelled = cancelled or threading.Event()
    creation_time = _identity()
    owner = Owner("ocr", os.environ["WORKBENCH_LAUNCH_ID"],
                  os.environ["WORKBENCH_INSTANCE_ID"], os.getpid(), creation_time)
    path = Path(os.environ["WORKBENCH_GPU_LEDGER"])
    if not path.is_absolute() or not path.parent.is_dir():
        raise ResourceUnavailable("GPU 协调账本位置无效")
    try:
        coordinator = GpuCoordinator(path)
    except Exception:
        raise ResourceUnavailable("GPU 协调器不可用") from None
    request_id = "ocr-" + uuid.uuid4().hex
    coordinator.request(request_id, "ocr")
    granted = False
    try:
        while True:
            if cancelled.is_set():
                raise ResourceUnavailable("等待 GPU 时已取消")
            try:
                lease = coordinator.grant(request_id, owner,
                                          owned=lambda actual: actual == owner and _identity() == creation_time)
                granted = True
                return Reservation(coordinator, lease, owner, creation_time)
            except ResourceBusy:
                cancelled.wait(0.1)
    finally:
        if not granted:
            coordinator.cancel_pending(request_id, "ocr")


@contextmanager
def _standalone_lock():
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.cwd()))) / 'OfflineOCR'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'gpu.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ResourceUnavailable('另一个 OCR 实例正在使用 GPU') from None
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ResourceUnavailable('另一个 OCR 实例正在使用 GPU') from None
        yield


def run_startup_probe(command, *, gpu_required=True, **options):
    """Keep CUDA dependency probes inside an owned Job and one platform grant."""
    if not gpu_required:
        return subprocess.run(command, **options)
    if not platform_mode():
        with _standalone_lock():
            return subprocess.run(command, **options)
    from ocr_workbench.processes import ProcessJob

    reservation = reserve('startup-probe')
    try:
        with ProcessJob() as job:
            process = subprocess.Popen(command, env=options['env'], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=options['text'],
                                       encoding=options['encoding'], errors=options['errors'],
                                       creationflags=options['creationflags'])
            job.assign(process)
            reservation.track(process, job)
            try:
                stdout, stderr = process.communicate(timeout=options['timeout'])
            finally:
                job.drain()
                process.wait(timeout=15)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        reservation.close()
