"""Temporary ASCII junction for Paddle's narrow C++ model-file APIs.

Junction creation requires no symlink privilege, copies no weights, and supports
cross-volume targets. All model bytes remain in the relocated bundle.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import struct
import tempfile


@contextmanager
def ascii_model_directory(models):
    requested=Path(models).absolute()
    models=requested.resolve()
    if requested.drive.upper()!='F:' or models.drive.upper()!='F:':
        raise RuntimeError('Paddle model directory must remain on the local F drive')
    if str(models).isascii():
        yield models
        return
    configured=os.environ.get('CLEAR_PADDLE_TEMP') or os.environ.get('TEMP')
    if not configured:
        raise RuntimeError('Paddle requires an explicitly registered ASCII F drive temporary directory')
    requested_parent=Path(configured).absolute()
    parent=requested_parent.resolve()
    if (requested_parent.drive.upper()!='F:' or parent.drive.upper()!='F:'
            or not str(parent).isascii()):
        raise RuntimeError('Paddle temporary directory must be an ASCII local F drive path')
    try:
        parent.mkdir(parents=True,exist_ok=True)
        temporary=Path(tempfile.mkdtemp(prefix='offlineocr-paddle-',dir=parent))
    except OSError as error:
        raise RuntimeError('Registered F drive Paddle temporary directory is not writable; no fallback is allowed') from error
    junction=temporary/'models'
    junction.mkdir()
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                            wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    api.DeviceIoControl.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,
                                 ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    try:
        substitute=('\\??\\'+str(models)).encode('utf-16-le')
        display=str(models).encode('utf-16-le')
        paths=substitute+b'\0\0'+display+b'\0\0'
        data=struct.pack('<IHHHHHH',0xA0000003,len(paths)+8,0,0,len(substitute),len(substitute)+2,len(display))+paths
        handle=api.CreateFileW(str(junction),0x40000000,7,None,3,0x02200000,None)
        if handle==ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buffer=ctypes.create_string_buffer(data);returned=wintypes.DWORD()
            if not api.DeviceIoControl(handle,0x000900A4,buffer,len(data),None,0,ctypes.byref(returned),None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            api.CloseHandle(handle)
        yield junction
    finally:
        # rmdir removes a directory junction itself, never its model target.
        if junction.exists() or junction.is_junction():os.rmdir(junction)
        temporary.rmdir()
