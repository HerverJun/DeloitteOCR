"""Windows Job ownership evidence. Timeouts never stand in for process exit."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
import uuid

from .gpu import Owner, ResourceEvidence, ReleaseUnconfirmed


def local_path(value):
    path = Path(value).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('GPU worker storage must remain on the local F drive')
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('GPU worker storage cannot traverse a reparse point')
    return path.resolve()


def atomic_json(path, value):
    path = local_path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.new')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt==19:raise
            time.sleep(.05)


def kernel():
    if os.name != 'nt':
        raise OSError('GPU ownership requires Windows Job Objects')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        'OpenJobObjectW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        'IsProcessInJob': ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        'GetProcessTimes': ([wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)], wintypes.BOOL),
        'GetExitCodeProcess': ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        getattr(api, name).argtypes, getattr(api, name).restype = arguments, result
    return api


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def process_identity(pid):
    api = kernel()
    handle = checked(api.OpenProcess(0x1000, False, pid))
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        checked(api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)))
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        api.CloseHandle(handle)


def owner_active(owner):
    """False only for a missing/exited exact owner; access errors are unknown."""
    api = kernel()
    handle = api.OpenProcess(0x1000, False, owner.pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID absent.
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        checked(api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)))
        actual = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        if actual != owner.creation_time:
            return False  # PID reuse is not the recorded owner.
        code = wintypes.DWORD()
        checked(api.GetExitCodeProcess(handle, ctypes.byref(code)))
        return code.value == 259
    finally:
        api.CloseHandle(handle)


class BasicLimit(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                   'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in ('TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
        (name, wintypes.DWORD) for name in ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]


class WindowsJob:
    def __init__(self):
        self.api = kernel()
        self.name = 'Local\\ClearGpu-' + uuid.uuid4().hex
        self.handle = checked(self.api.CreateJobObjectW(None, self.name))
        self.drained = False
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway.
        try:
            checked(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign(self, process):
        checked(self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))))

    def contains(self, process):
        answer = wintypes.BOOL()
        checked(self.api.IsProcessInJob(wintypes.HANDLE(int(process._handle)), self.handle, ctypes.byref(answer)))
        return bool(answer.value)

    def active(self):
        record = Accounting()
        checked(self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(record), ctypes.sizeof(record), None))
        return record.ActiveProcesses

    def drain(self, timeout=15):
        if self.active():
            checked(self.api.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + timeout
        while self.active():
            if time.monotonic() >= deadline:
                raise ReleaseUnconfirmed('Owned GPU process tree did not exit')
            time.sleep(.05)
        self.drained = True

    def close(self):
        if self.handle:
            checked(self.api.CloseHandle(self.handle))
            self.handle = None


def persist_owner(directory, lease, job):
    directory = local_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / (lease.request_id + '.json'), {
        'format': 'clear-gpu-owner-v1', 'request_id': lease.request_id,
        'generation': lease.generation, 'owner': asdict(lease.owner),
        'job_name': job.name, 'kill_on_close': True, 'breakaway': False,
    })


def recovery_evidence(record, owner, *, active_probe=owner_active, job_probe=None):
    if (record.get('format') != 'clear-gpu-owner-v1' or record.get('owner') != asdict(owner)
            or record.get('kill_on_close') is not True or record.get('breakaway') is not False
            or not str(record.get('job_name', '')).startswith('Local\\ClearGpu-')):
        raise ReleaseUnconfirmed('No matching owned Job evidence; GPU remains reserved')
    active = active_probe(owner)
    if job_probe is None:
        api = kernel()
        handle = api.OpenJobObjectW(4, False, record['job_name'])  # JOB_OBJECT_QUERY only.
        if not handle:
            if ctypes.get_last_error() != 2:  # Job removed after last handle closed.
                raise ctypes.WinError(ctypes.get_last_error())
            children = False
        else:
            try:
                info = Accounting()
                checked(api.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
                children = bool(info.ActiveProcesses)
            finally:
                api.CloseHandle(handle)
    else:
        children = job_probe(record['job_name'])
    return ResourceEvidence(True, active, children, active is False and children is False)


def recover_owned(coordinator, directory, **probes):
    """Observe only. Never kill an old PID or unlock from TTL alone."""
    snapshot = coordinator.snapshot()
    if snapshot.owner is None:
        return False
    path = local_path(directory) / (snapshot.owner_request_id + '.json')
    if not path.is_file():
        raise ReleaseUnconfirmed('GPU owner has no verifiable recovery record')
    record = json.loads(path.read_text('utf-8'))
    if record.get('request_id') != snapshot.owner_request_id or record.get('generation') != snapshot.generation:
        raise ReleaseUnconfirmed('GPU recovery generation changed')
    evidence = recovery_evidence(record, snapshot.owner, **probes)
    coordinator.reclaim(snapshot.generation, witness=lambda _: evidence)
    return True
