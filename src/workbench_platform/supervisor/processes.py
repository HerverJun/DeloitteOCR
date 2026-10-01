"""Trusted Windows process ownership and loopback identity supervision.

Only the local registry selects argv/cwd. The broker receives a private launch
nonce and is assigned to a kill-on-close Job Object before it executes argv.
"""

from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from ctypes import wintypes

from packages.contracts.models import AppHealth, AppManifest, Health, LaunchReceipt
from workbench_platform.catalog.registry import AppRegistry, Installation, _bundle_path, _file_sha256, _unique_pairs


class LaunchFailure(RuntimeError):
    pass


class LaunchConflict(ValueError):
    pass


class NotOwned(PermissionError):
    pass


KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
HANDLE = wintypes.HANDLE
KERNEL.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
KERNEL.CreateJobObjectW.restype = HANDLE
KERNEL.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
KERNEL.SetInformationJobObject.restype = wintypes.BOOL
KERNEL.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
KERNEL.AssignProcessToJobObject.restype = wintypes.BOOL
KERNEL.TerminateJobObject.argtypes = [HANDLE, wintypes.UINT]
KERNEL.TerminateJobObject.restype = wintypes.BOOL
KERNEL.CloseHandle.argtypes = [HANDLE]
KERNEL.CloseHandle.restype = wintypes.BOOL
KERNEL.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL.OpenProcess.restype = HANDLE
KERNEL.GetProcessTimes.argtypes = [HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
KERNEL.GetProcessTimes.restype = wintypes.BOOL
KERNEL.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(wintypes.DWORD)]
KERNEL.GetExitCodeProcess.restype = wintypes.BOOL
KERNEL.IsProcessInJob.argtypes = [HANDLE, HANDLE, ctypes.POINTER(wintypes.BOOL)]
KERNEL.IsProcessInJob.restype = wintypes.BOOL
KERNEL.QueryInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
KERNEL.QueryInformationJobObject.restype = wintypes.BOOL
IPHLP = ctypes.WinDLL("iphlpapi", use_last_error=True)
IPHLP.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                     wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
IPHLP.GetExtendedTcpTable.restype = wintypes.DWORD


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
        "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class _TcpOwner(ctypes.Structure):
    _fields_ = [("state", wintypes.DWORD), ("local_address", wintypes.DWORD),
                ("local_port", wintypes.DWORD), ("remote_address", wintypes.DWORD),
                ("remote_port", wintypes.DWORD), ("pid", wintypes.DWORD)]


def _require(ok):
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows process ownership operation failed")


class _OwnedJob:
    def __init__(self):
        self.handle = KERNEL.CreateJobObjectW(None, None)
        _require(self.handle)
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
        try:
            _require(KERNEL.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign(self, process):
        _require(KERNEL.AssignProcessToJobObject(self.handle, HANDLE(process._handle)))

    def contains(self, pid: int) -> bool:
        process = KERNEL.OpenProcess(0x1000, False, pid)
        _require(process)
        try:
            answer = wintypes.BOOL()
            _require(KERNEL.IsProcessInJob(process, self.handle, ctypes.byref(answer)))
            return bool(answer.value)
        finally:
            KERNEL.CloseHandle(process)

    def active(self) -> int:
        info = _Accounting()
        _require(KERNEL.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
        return info.ActiveProcesses

    def terminate(self):
        _require(KERNEL.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + 10
        while self.active() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.active():
            raise LaunchFailure("Owned process tree did not exit")

    def close(self):
        if self.handle:
            _require(KERNEL.CloseHandle(self.handle))
            self.handle = None


def process_creation_time(pid: int) -> str:
    handle = KERNEL.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    _require(handle)
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        _require(KERNEL.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)))
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        instant = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
    finally:
        KERNEL.CloseHandle(handle)


def _process_active(pid: int, creation_time: str) -> bool:
    handle = KERNEL.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not KERNEL.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel), ctypes.byref(user)):
            return False
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        instant = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        actual = instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
        code = wintypes.DWORD()
        return actual == creation_time and bool(KERNEL.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        KERNEL.CloseHandle(handle)


def _listener_pid(port: int) -> int:
    """Ask the OS which PID owns the actual IPv4 loopback LISTEN socket."""
    size = wintypes.DWORD()
    result = IPHLP.GetExtendedTcpTable(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    if result != 122:  # ERROR_INSUFFICIENT_BUFFER
        raise LaunchFailure("Cannot inspect loopback listener ownership")
    for _ in range(3):
        if not 4 <= size.value <= 4 * 1024 * 1024:
            raise LaunchFailure("Loopback ownership table exceeds limit")
        buffer = ctypes.create_string_buffer(size.value)
        result = IPHLP.GetExtendedTcpTable(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if result == 0:
            break
        if result != 122:
            raise LaunchFailure("Cannot inspect loopback listener ownership")
    else:
        raise LaunchFailure("Loopback ownership table changed repeatedly")
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    if 4 + count * ctypes.sizeof(_TcpOwner) > size.value:
        raise LaunchFailure("Invalid loopback ownership table")
    rows = (_TcpOwner * count).from_buffer_copy(buffer.raw[4:4 + count * ctypes.sizeof(_TcpOwner)])
    owners = {row.pid for row in rows if row.state == 2 and socket.ntohs(row.local_port & 0xffff) == port
              and socket.inet_ntoa(row.local_address.to_bytes(4, "little")) == "127.0.0.1"}
    if len(owners) != 1:
        raise LaunchFailure("Loopback listener ownership is unavailable or ambiguous")
    return owners.pop()


def _loopback_port(base_url: str) -> int:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise LaunchFailure("Service address is not a plain loopback origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise LaunchFailure("Invalid service port") from error
    if port is None or not 1 <= port <= 65535 or parsed.netloc != f"127.0.0.1:{port}":
        raise LaunchFailure("Invalid service port")
    return port


def _identity(port: int, app_id: str, instance_id: str, nonce: str) -> str:
    # http.client uses a direct TCP connection: no proxy, redirect, or URL from
    # the response can change the destination.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
    try:
        connection.request("GET", "/api/platform/v1/identity")
        response = connection.getresponse()
        if response.status != 200 or int(response.getheader("Content-Length", "0")) > 4096:
            raise LaunchFailure("Service identity handshake failed")
        body = response.read(4097)
        if len(body) > 4096:
            raise LaunchFailure("Service identity response is too large")
        value = json.loads(body)
        if (not isinstance(value, dict) or set(value) != {"app_id", "instance_id", "nonce", "version"}
                or (value["app_id"], value["instance_id"], value["nonce"]) != (app_id, instance_id, nonce)
                or not isinstance(value["version"], str) or not 0 < len(value["version"]) <= 256):
            raise LaunchFailure("Service identity differs from launch")
        return value["version"]
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, LaunchFailure):
            raise
        raise LaunchFailure("Service identity handshake failed") from error
    finally:
        connection.close()


def _verify_bundle(registry: AppRegistry, snapshot, app_id: str, install: Installation) -> None:
    """Reuse completed startup verification; unverified installs check all payloads."""
    record = json.loads((registry.config_dir / f"{app_id}.install.json").read_text(encoding="utf-8"),
                        object_pairs_hook=_unique_pairs)
    manifest_bytes = (registry.config_dir / f"{app_id}.json").read_bytes()
    if (hashlib.sha256(manifest_bytes).hexdigest() != record["manifest_sha256"] or
            AppManifest.model_validate(json.loads(manifest_bytes, object_pairs_hook=_unique_pairs)) != snapshot.manifests[app_id]):
        raise LaunchFailure("Trusted application declaration changed since catalog load")
    launch = record["launch"]
    root = Path(launch["bundle_root"]).resolve()
    if (not root.is_relative_to(registry.install_root) or
            Path(launch["executable"]).resolve() != install.executable or
            Path(launch["working_directory"]).resolve() != install.working_directory or
            launch["bundle_manifest_sha256"] != install.bundle_manifest_sha256 or
            launch["executable_sha256"] != install.executable_sha256 or
            tuple(launch["allowed_arguments"]) != install.allowed_arguments):
        raise LaunchFailure("Trusted installation changed since catalog load")
    manifest_path = (root / "bundle.manifest.json").resolve()
    if not manifest_path.is_relative_to(root):
        raise LaunchFailure("Bundle manifest escapes trusted root")
    with manifest_path.open("rb") as stream:
        data = stream.read(1_048_577)
    if len(data) > 1_048_576 or hashlib.sha256(data).hexdigest() != install.bundle_manifest_sha256:
        raise LaunchFailure("Trusted bundle declaration changed")
    declaration = json.loads(data, object_pairs_hook=_unique_pairs)
    if declaration["app_id"] != app_id or not isinstance(declaration["files"], list):
        raise LaunchFailure("Invalid bundle declaration")
    entries = registry.startup_entry_hashes(app_id, install, snapshot_revision=snapshot.revision)
    if entries is not None:
        for relative, expected in entries.items():
            file_path = root.joinpath(*_bundle_path(relative)).resolve()
            if (not file_path.is_relative_to(root) or not file_path.is_file()
                    or _file_sha256(file_path) != expected):
                raise LaunchFailure("Verified startup entry point changed")
        return
    seen = set()
    for item in declaration["files"]:
        parts = _bundle_path(item["path"])
        if parts in seen:
            raise LaunchFailure("Duplicate bundle payload")
        seen.add(parts)
        file_path = (root.joinpath(*parts)).resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file() or _file_sha256(file_path) != item["sha256"]:
            raise LaunchFailure("Bundle payload integrity changed")


@dataclass
class _Entry:
    identity: tuple
    future: Future[LaunchReceipt]
    job: _OwnedJob | None = None
    broker: subprocess.Popen | None = None


class Supervisor:
    def __init__(self, registry: AppRegistry, *, handshake_timeout: float = 5.0,
                 data_root: Path | None = None):
        self.registry = registry
        self.handshake_timeout = handshake_timeout
        self.data_root = data_root.resolve() if data_root else None
        if self.data_root:
            if not self.data_root.is_dir():
                raise ValueError("Platform data directory is unavailable")
            (self.data_root / "native-local").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._current: dict[str, tuple[int, str]] = {}
        self._closed = False
        self._closed_event = threading.Event()

    def _reserve(self, launch_id: str, identity: tuple) -> tuple[_Entry, bool]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", launch_id):
            raise ValueError("Invalid launch ID")
        with self._lock:
            if self._closed:
                raise LaunchFailure("Supervisor is closed")
            existing = self._entries.get(launch_id)
            if existing:
                same_owned = existing.identity[:2] == identity[:2] and identity[0] == "owned"
                same_discovered = (existing.identity[:2] == identity[:2] and identity[0] == "discovered"
                                   and existing.identity[3:] == identity[3:])
                if existing.identity != identity and not (same_owned or same_discovered):
                    raise LaunchConflict("Launch ID belongs to another request")
                return existing, False
            entry = _Entry(identity, Future())
            self._entries[launch_id] = entry
            return entry, True

    def start(self, app_id: str, launch_id: str) -> LaunchReceipt:
        with self._lock:
            if self._closed:
                raise LaunchFailure("Supervisor is closed")
            existing = self._entries.get(launch_id)
            if existing:
                if existing.identity[:2] != ("owned", app_id):
                    raise LaunchConflict("Launch ID belongs to another request")
        if existing:
            return existing.future.result()  # Startup can acquire the lock while we wait.
        snapshot = self.registry._snapshot  # one immutable catalog generation
        install = snapshot.installations.get(app_id)
        if install is None or not install.installed:
            raise LaunchFailure("Application bundle is not installed")
        identity = ("owned", app_id, snapshot.revision, install.executable,
                    install.bundle_manifest_sha256, install.executable_sha256, install.allowed_arguments)
        entry, creator = self._reserve(launch_id, identity)
        if creator:
            try:
                receipt = self._start_owned(entry, snapshot, install, app_id, launch_id)
                entry.future.set_result(receipt)
                threading.Thread(target=self._watch, args=(entry, receipt, snapshot.revision), daemon=True).start()
            except BaseException as error:
                try:
                    self._cleanup(entry)
                except BaseException as cleanup_error:
                    error = LaunchFailure("Owned launch failed and process cleanup failed")
                    error.__cause__ = cleanup_error
                finally:
                    entry.future.set_exception(error)  # Waiters never remain pending after a failed launch.
        return entry.future.result()

    def _start_owned(self, entry, snapshot, install, app_id, launch_id):
        _verify_bundle(self.registry, snapshot, app_id, install)
        instance_id, nonce = "i" + uuid.uuid4().hex, "n" + uuid.uuid4().hex
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "ProgramData") if key in os.environ}
        environment.update({"PATH": str(install.executable.parent) + os.pathsep + os.environ.get("SystemRoot", r"C:\Windows") + r"\System32",
                            "WORKBENCH_APP_ID": app_id, "WORKBENCH_INSTANCE_ID": instance_id,
                            "WORKBENCH_NONCE": nonce, "WORKBENCH_LAUNCH_ID": launch_id,
                            "WORKBENCH_PORT": "0", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
        if self.data_root:
            environment.update({"WORKBENCH_GPU_LEDGER": str(self.data_root / "gpu.sqlite3"),
                                "LOCALAPPDATA": str(self.data_root / "native-local")})
        job = _OwnedJob()
        with self._lock:
            entry.job = job  # close() waits for the future before cleanup.
        broker = subprocess.Popen([sys.executable, "-I", "-S", "-u", str(Path(__file__).with_name("broker.py"))],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  creationflags=0x08000000, text=True, encoding="utf-8", errors="replace")
        entry.broker = broker
        try:
            job.assign(broker)
        except BaseException:
            broker.kill()  # Only this just-created, still-gated broker.
            broker.wait(timeout=5)
            raise
        payload = {"argv": [str(install.executable), *install.allowed_arguments],
                   "cwd": str(install.working_directory), "environment": environment}
        broker.stdin.write(json.dumps(payload) + "\n")
        broker.stdin.close()
        lines: queue.SimpleQueue[str] = queue.SimpleQueue()
        def read_output():
            for line in broker.stdout:
                line = line.rstrip("\r\n")
                if line.startswith(("CHILD ", "OUTPUT READY ", "BROKER_ERROR ")):
                    lines.put(line)
        threading.Thread(target=read_output, daemon=True).start()
        child = None
        port = None
        deadline = time.monotonic() + self.handshake_timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if broker.poll() is not None:
                    raise LaunchFailure("Application exited before identity handshake")
                continue
            if line.startswith("CHILD "):
                child = json.loads(line[6:])
            elif line.startswith("OUTPUT READY "):
                try:
                    port = int(line[len("OUTPUT READY "):])
                except ValueError as error:
                    raise LaunchFailure("Invalid bound port report") from error
                if not 1 <= port <= 65535:
                    raise LaunchFailure("Invalid bound port report")
            elif line.startswith("BROKER_ERROR "):
                raise LaunchFailure("Trusted launch broker failed")
            if child and port:
                while time.monotonic() < deadline and broker.poll() is None:
                    try:
                        version = _identity(port, app_id, instance_id, nonce)
                        if process_creation_time(child["pid"]) != child["creation_time"]:
                            raise LaunchFailure("Launched process identity changed")
                        listener_pid = _listener_pid(port)
                        if not job.contains(listener_pid):
                            raise LaunchFailure("Listener is outside owned process tree")
                        actual_creation = process_creation_time(listener_pid)
                        receipt = LaunchReceipt(schema_version="1.0", launch_id=launch_id, app_id=app_id,
                                                instance_id=instance_id, pid=listener_pid,
                                                creation_time=actual_creation,
                                                base_url=f"http://127.0.0.1:{port}/", handshake_nonce=nonce,
                                                ownership="owned", version=version)
                        self._observe(app_id, snapshot.revision, instance_id)
                        return receipt
                    except LaunchFailure as error:
                        if "handshake failed" not in str(error):
                            raise
                        time.sleep(0.05)
                raise LaunchFailure("Service identity handshake timed out or process exited")
        raise LaunchFailure("Service identity handshake timed out")

    def _observe(self, app_id: str, revision: int, instance_id: str):
        manifest = self.registry._snapshot.manifests[app_id]
        health = lambda state, reason: Health(schema_version="1.0", state=state, reason=reason)
        with self._lock:
            self.registry.observe_health(app_id, AppHealth(schema_version="1.0",
                service=health("ready", "Service identity verified"),
                ui=health("unknown", "UI has not been probed"), cpu=health("unknown", "CPU has not been probed"),
                gpu=health("unknown" if "gpu" in manifest.resources else "disabled", "GPU has not been probed"),
                models=health("unknown", "Model files have not been probed")), snapshot_revision=revision)
            self._current[app_id] = (revision, instance_id)

    def _unavailable(self, receipt: LaunchReceipt, revision: int, *, monitoring_stopped: bool = False):
        app_id = receipt.app_id
        with self._lock:
            if self._current.get(app_id) != (revision, receipt.instance_id):
                return
            self._current.pop(app_id, None)
            manifest = self.registry._snapshot.manifests.get(app_id)
            if manifest is None:
                return
            health = lambda state, reason: Health(schema_version="1.0", state=state, reason=reason)
            observation = AppHealth(schema_version="1.0",
                service=health("unknown" if monitoring_stopped else "unavailable",
                               "Supervisor stopped monitoring" if monitoring_stopped else "Service exited or was stopped"),
                ui=health("unknown", "UI has not been probed"), cpu=health("unknown", "CPU has not been probed"),
                gpu=health("unknown" if "gpu" in manifest.resources else "disabled", "GPU has not been probed"),
                models=health("unknown", "Model files have not been probed"))
            try:
                self.registry.observe_health(app_id, observation, snapshot_revision=revision)
            except ValueError:  # A reload already invalidated this generation.
                pass

    def _watch(self, entry: _Entry, receipt: LaunchReceipt, revision: int):
        while not self._closed_event.wait(0.1):
            if not _process_active(receipt.pid, receipt.creation_time):
                try:
                    if receipt.ownership == "owned":
                        self._cleanup(entry)
                finally:
                    self._unavailable(receipt, revision)
                return

    def discover(self, app_id: str, launch_id: str, *, pid: int, creation_time: str,
                 base_url: str, instance_id: str, nonce: str) -> LaunchReceipt:
        port = _loopback_port(base_url)
        with self._lock:
            if self._closed:
                raise LaunchFailure("Supervisor is closed")
            existing = self._entries.get(launch_id)
            if existing:
                if (existing.identity[:2] != ("discovered", app_id) or
                        existing.identity[3:] != (pid, creation_time, port, instance_id, nonce)):
                    raise LaunchConflict("Launch ID belongs to another request")
        if existing:
            return existing.future.result()
        snapshot = self.registry._snapshot
        if app_id not in snapshot.manifests:
            raise LaunchFailure("Application is not registered")
        identity = ("discovered", app_id, snapshot.revision, pid, creation_time, port, instance_id, nonce)
        entry, creator = self._reserve(launch_id, identity)
        if creator:
            try:
                if process_creation_time(pid) != creation_time:
                    raise LaunchFailure("Discovered process identity changed")
                version = _identity(port, app_id, instance_id, nonce)
                if _listener_pid(port) != pid:
                    raise LaunchFailure("Discovered PID does not own loopback listener")
                if process_creation_time(pid) != creation_time:
                    raise LaunchFailure("Discovered process identity changed")
                receipt = LaunchReceipt(schema_version="1.0", launch_id=launch_id, app_id=app_id,
                                        instance_id=instance_id, pid=pid, creation_time=creation_time,
                                        base_url=f"http://127.0.0.1:{port}/", handshake_nonce=nonce,
                                        ownership="discovered", version=version)
                self._observe(app_id, snapshot.revision, instance_id)
                entry.future.set_result(receipt)
                threading.Thread(target=self._watch, args=(entry, receipt, snapshot.revision), daemon=True).start()
            except BaseException as error:
                entry.future.set_exception(error)
        return entry.future.result()

    def stop(self, launch_id: str) -> None:
        with self._lock:
            entry = self._entries[launch_id]
            if entry.identity[0] != "owned":
                raise NotOwned("Discovered process is not owned by this platform")
        receipt = entry.future.result()  # Never wait on startup while holding self._lock.
        self._cleanup(entry)
        self._unavailable(receipt, entry.identity[2])

    def _cleanup(self, entry: _Entry) -> None:
        with self._lock:
            if entry.job:
                entry.job.terminate()
                entry.job.close()  # Close only after the entire owned tree exited.
                entry.job = None
            if entry.broker:
                try:
                    entry.broker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    entry.broker.kill()
                    entry.broker.wait(timeout=5)
                entry.broker = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._closed_event.set()
            entries = list(self._entries.values())
        for entry in entries:
            try:
                receipt = entry.future.result()
            except BaseException:
                receipt = None
            if entry.job:
                self._cleanup(entry)
            if receipt is not None:
                self._unavailable(receipt, entry.identity[2],
                                  monitoring_stopped=receipt.ownership == "discovered" and
                                  _process_active(receipt.pid, receipt.creation_time))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
