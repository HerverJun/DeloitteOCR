"""Content verification with a machine-local NTFS change-identity cache.

An explicit verify reads every byte. Startup may reuse a previously checked
digest only while file ID, size, mtime and NTFS ChangeTime remain identical.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt
    class _BasicInfo(ctypes.Structure):
        _fields_ = [('CreationTime', ctypes.c_int64), ('LastAccessTime', ctypes.c_int64),
                    ('LastWriteTime', ctypes.c_int64), ('ChangeTime', ctypes.c_int64),
                    ('FileAttributes', wintypes.DWORD)]
    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL


def signature(path):
    info = path.stat()
    change = info.st_ctime_ns
    if os.name == 'nt':
        basic = _BasicInfo()
        with path.open('rb') as stream:
            if not _kernel.GetFileInformationByHandleEx(wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno())),
                    0, ctypes.byref(basic), ctypes.sizeof(basic)):
                raise ctypes.WinError(ctypes.get_last_error())
        change = basic.ChangeTime
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _pairs(items):
    value = {}
    for name, row in items:
        if name in value:
            raise ValueError('Duplicate content declaration: ' + name)
        value[name] = row
    return value


def verify_content(config, *, use_cache=False):
    manifest = config.check(config.program/'manifests/package.json', role='read')
    declaration = json.loads(manifest.read_text('utf-8-sig'), object_pairs_hook=_pairs)
    if declaration.get('format') != 'clear-portable-package-v1' or declaration.get('standalone') is not True:
        raise ValueError('This is not a standalone Clear offline package')
    manifest_sha = digest(manifest)
    cache_file = config.check(config.roots['cache']/'integrity-v1.json', role='write')
    try:
        cached = json.loads(cache_file.read_text('utf-8')) if use_cache else {}
    except (OSError, ValueError):
        cached = {}
    cache_identity = {'format': 'clear-portable-integrity-v1', 'program': str(config.program),
        'installation_id': config.installation_id, 'package_sha256': manifest_sha}
    if any(cached.get(key) != value for key, value in cache_identity.items()):
        cached = {}
    previous = cached.get('files', {}) if cached.get('ok') is True else {}
    errors, checked, hashed, reused, records = [], 0, 0, 0, {}
    declared = declaration.get('files')
    if not isinstance(declared, dict) or not declared:
        raise ValueError('Offline package has no content closure')
    actual = set()
    for directory, children, files in os.walk(config.program, followlinks=False):
        parent = Path(directory)
        for name in [*children, *files]:
            path = parent/name
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Package cannot contain a reparse point')
            except (OSError, ValueError) as error:
                errors.append({'member': path.relative_to(config.program).as_posix(), 'error': str(error)})
        for name in files:
            path = parent/name
            if path != manifest:
                actual.add(path.relative_to(config.program).as_posix())
    for unexpected in sorted(actual-set(declared)):
        errors.append({'member': unexpected, 'error': 'Undeclared package member'})
    for member, expected in declared.items():
        try:
            relative = Path(member)
            if (not member or '\\' in member or ':' in member or relative.is_absolute()
                    or any(part in {'', '.', '..'} for part in member.split('/'))
                    or not isinstance(expected, dict) or not re.fullmatch('[0-9a-f]{64}', expected.get('sha256', ''))):
                raise ValueError('Invalid relative content declaration')
            # The exact physical tree above already checked every ancestor.
            # Rechecking all ancestors for each of ~180k leaves magnifies NTFS
            # metadata I/O without strengthening the immutable package boundary.
            path = config.program/relative
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('Package member became a reparse point')
            before = signature(path)
            if before[2] != expected['bytes']:
                raise ValueError('Missing or changed file size')
            record = previous.get(member, {})
            if record.get('signature') == before and record.get('sha256') == expected['sha256']:
                reused += 1
            else:
                if digest(path) != expected['sha256']:
                    raise ValueError('File content checksum differs')
                if signature(path) != before:
                    raise ValueError('Package file changed while verifying')
                hashed += 1
            records[member] = {'signature': before, 'sha256': expected['sha256']}
            checked += 1
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append({'member': member, 'error': str(error)})
    for name in config.resources:
        try:
            config.resource(name)
        except (ValueError, OSError) as error:
            errors.append({'resource': name, 'error': str(error)})
    errors.extend({'closure': value} for value in declaration.get('blockers', []))
    if digest(manifest) != manifest_sha:
        errors.append({'error': 'Content manifest changed while verifying'})
    result = dict(ok=not errors, files_checked=checked, files_hashed=hashed, files_cached=reused, errors=errors,
        package_sha256=manifest_sha, package_status=declaration.get('status', 'unqualified'),
        installation_id=config.installation_id, checked_at=datetime.now(timezone.utc).isoformat())
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_file.with_name(cache_file.name+'.'+uuid.uuid4().hex+'.new')
    temporary.write_text(json.dumps({**cache_identity, 'ok': not errors, 'files': records}), encoding='utf-8')
    os.replace(temporary, cache_file)
    return result
