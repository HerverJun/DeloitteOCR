"""Windows Job Object ownership; the job handle, never a PID scan, owns cleanup."""

import ctypes
import time
from ctypes import wintypes

KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
HANDLE = wintypes.HANDLE
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t


class BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", SIZE_T),
                ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ULONG_PTR), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class Accounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


KERNEL.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
KERNEL.CreateJobObjectW.restype = HANDLE
KERNEL.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
KERNEL.SetInformationJobObject.restype = wintypes.BOOL
KERNEL.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
KERNEL.AssignProcessToJobObject.restype = wintypes.BOOL
KERNEL.QueryInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
KERNEL.QueryInformationJobObject.restype = wintypes.BOOL
KERNEL.TerminateJobObject.argtypes = [HANDLE, wintypes.UINT]
KERNEL.TerminateJobObject.restype = wintypes.BOOL
KERNEL.CloseHandle.argtypes = [HANDLE]
KERNEL.CloseHandle.restype = wintypes.BOOL


def require(ok):
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows Job Object operation failed")


class OwnedJob:
    def __init__(self):
        self.handle = KERNEL.CreateJobObjectW(None, None)
        require(self.handle)
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway flags.
        try:
            require(KERNEL.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign(self, process):
        require(KERNEL.AssignProcessToJobObject(self.handle, HANDLE(process._handle)))

    def active(self):
        info = Accounting()
        require(KERNEL.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
        return info.ActiveProcesses

    def terminate(self):
        require(KERNEL.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + 10
        while self.active() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.active():
            raise RuntimeError("Owned Job Object still contains live processes")

    def close(self):
        if self.handle:
            KERNEL.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
