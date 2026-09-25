"""Windows job object: kill worker and native descendants when owner exits."""
import ctypes
from ctypes import wintypes
import time


class BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ['ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount']]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IOCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class BasicAccounting(ctypes.Structure):
    _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]


class ProcessJob:
    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.drained = False
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            process.kill()
            process.wait()
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self):
        if not self.handle:
            raise RuntimeError('Job handle is closed')
        record, size = BasicAccounting(), wintypes.DWORD()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(record),
                                                  ctypes.sizeof(record), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return record.ActiveProcesses

    def drain(self, timeout=15):
        """Terminate only this owned Job, then prove all its descendants exited."""
        if not self.handle:
            raise RuntimeError('Job handle is closed')
        if self.active_processes() and not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while self.active_processes():
            if time.monotonic() >= deadline:
                raise TimeoutError('Owned Job descendants did not exit')
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
