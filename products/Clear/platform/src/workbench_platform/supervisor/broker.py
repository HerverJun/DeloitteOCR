"""Assigned-to-Job gate: no application code runs before the parent assigns us."""

import ctypes
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from ctypes import wintypes


KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
KERNEL.GetProcessTimes.restype = wintypes.BOOL


def creation_time(process):
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not KERNEL.GetProcessTimes(process._handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError(ctypes.get_last_error(), "Cannot identify child creation time")
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    value = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def main():
    payload = json.loads(sys.stdin.readline())
    argv, cwd, environment = payload["argv"], payload["cwd"], payload["environment"]
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        return 125
    # The broker is already in a non-breakaway Job Object. Its children inherit
    # that job; no PID scan or process-name cleanup is involved.
    child = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=0x08000000, text=True, encoding="utf-8", errors="replace")
    print("CHILD " + json.dumps({"pid": child.pid, "creation_time": creation_time(child)}), flush=True)
    for line in child.stdout:
        print("OUTPUT " + line.rstrip("\r\n")[:1024], flush=True)
    return child.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("BROKER_ERROR " + type(error).__name__, flush=True)
        sys.exit(125)
