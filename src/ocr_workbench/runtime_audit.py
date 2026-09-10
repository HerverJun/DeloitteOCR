"""Read loaded Windows modules to detect accidental development-environment DLLs."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys


def inspect_runtime(bundle):
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.GetCurrentProcess.restype=wintypes.HANDLE
    api.K32EnumProcessModules.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    api.GetModuleFileNameW.argtypes=[wintypes.HMODULE,wintypes.LPWSTR,wintypes.DWORD]
    modules=(wintypes.HMODULE*4096)();needed=wintypes.DWORD()
    if not api.K32EnumProcessModules(api.GetCurrentProcess(),modules,ctypes.sizeof(modules),ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if needed.value>ctypes.sizeof(modules):
        raise RuntimeError('Too many modules to audit')
    paths=[];external=[];security_modules=[]
    windows=Path(os.environ['SystemRoot']).resolve()
    defender=(Path(os.environ['ProgramData'])/'Microsoft/Windows Defender/Platform').resolve()
    for handle in modules[:needed.value//ctypes.sizeof(wintypes.HMODULE)]:
        buffer=ctypes.create_unicode_buffer(32768)
        length=api.GetModuleFileNameW(handle,buffer,len(buffer))
        if not length or length>=len(buffer):
            raise RuntimeError('Could not resolve a loaded module')
        path=Path(buffer.value).resolve();paths.append(str(path))
        if path.is_relative_to(defender) and path.name.lower()=='mpoav.dll':
            security_modules.append(str(path))
            continue
        if not path.is_relative_to(bundle.resolve()) and not path.is_relative_to(windows):
            external.append(str(path))
        if path.is_relative_to(windows) and path.name.lower().startswith(('cudnn','cublas','cudart','msvcp140','vcruntime140','python3')):
            external.append(str(path))
    if external:
        raise RuntimeError(f'Unbundled application runtime DLLs were loaded: {external}')
    return {'python':sys.version,'executable':sys.executable,'loaded_modules':sorted(paths),
            'os_security_modules':security_modules,'non_system_modules_inside_bundle':True}
