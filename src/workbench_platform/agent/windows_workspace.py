"""Ensure private task directories remain readable by DSH's restricted token.

Some inherited host ACLs grant the owner access only through groups removed
from that token. An explicit owner rule on our private directory preserves
normal access; DSH's restricting SID and integrity label still bound writes.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

from workbench_platform.supervisor.processes import require_f_path


def prepare_owner_access(path, *, writable=True, inherit=True):
    path = require_f_path(path)
    if os.name != 'nt':
        return {'path': str(path), 'changed': False}
    if path.is_symlink() or path.is_junction():
        raise ValueError('Private execution directory cannot be a link')
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    dword = wintypes.DWORD

    class Trustee(ctypes.Structure):
        _fields_ = [('multiple', pointer), ('operation', dword), ('form', dword),
                    ('kind', dword), ('sid', pointer)]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [('permissions', dword), ('mode', dword), ('inheritance', dword), ('trustee', Trustee)]

    class AclSize(ctypes.Structure):
        _fields_ = [('count', dword), ('used', dword), ('free', dword)]

    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, dword, dword,
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    advapi.GetNamedSecurityInfoW.restype = dword
    advapi.GetAclInformation.argtypes = [pointer, pointer, dword, dword]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [pointer, dword, ctypes.POINTER(pointer)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [pointer, pointer]
    advapi.EqualSid.restype = wintypes.BOOL
    advapi.SetEntriesInAclW.argtypes = [dword, ctypes.POINTER(ExplicitAccess), pointer, ctypes.POINTER(pointer)]
    advapi.SetEntriesInAclW.restype = dword
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, dword, dword, pointer, pointer, pointer, pointer]
    advapi.SetNamedSecurityInfoW.restype = dword
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    owner, dacl, descriptor, merged = pointer(), pointer(), pointer(), pointer()
    requested_access = 0x1F01FF if writable else 0x1200A9
    inheritance = 3 if inherit else 0
    try:
        error = advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                                             ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        size = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        for index in range(size.count):
            ace = pointer()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            mask = dword.from_address(ace.value + 4).value
            if (kind == 0 and flags & inheritance == inheritance and mask & requested_access == requested_access
                    and advapi.EqualSid(owner, pointer(ace.value + 8))):
                return {'path': str(path), 'changed': False}
        access = ExplicitAccess(requested_access, 1, inheritance, Trustee(None, 0, 0, 1, owner))
        error = advapi.SetEntriesInAclW(1, ctypes.byref(access), dacl, ctypes.byref(merged))
        if error:
            raise ctypes.WinError(error)
        error = advapi.SetNamedSecurityInfoW(str(path), 1, 4, None, None, merged, None)
        if error:
            raise ctypes.WinError(error)
        return {'path': str(path), 'changed': True}
    finally:
        if merged:
            kernel.LocalFree(merged)
        if descriptor:
            kernel.LocalFree(descriptor)
