"""Read loaded Windows modules to detect accidental development-environment DLLs."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or PureWindowsPath(value).drive or ':' in value):
        raise ValueError('Invalid runtime source path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError('Invalid runtime source path')
    return path


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate runtime declaration key')
        result[key] = value
    return result


def _read_json(path, limit):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Runtime declaration exceeds size limit')
    return json.loads(data, object_pairs_hook=_unique), data


def _mineru_sources(bundle):
    configured = os.environ.get('CLEAR_MINERU_PRO_RUNTIME_MANIFEST')
    if not configured:
        return []
    requested = Path(configured).absolute()
    path = requested.resolve(strict=True)
    if requested.drive.upper() != 'F:' or path.drive.upper() != 'F:':
        raise RuntimeError('MinerU runtime source manifest escaped local F drive')
    record, payload = _read_json(path, 16_777_216)
    if (hashlib.sha256(payload).hexdigest() != os.environ.get('CLEAR_MINERU_PRO_RUNTIME_MANIFEST_SHA256')
            or record.get('schema_version') != 1):
        raise RuntimeError('MinerU runtime source declaration differs from the pinned launcher identity')
    sources, seen = [], set()
    for source in record['sources']:
        requested = Path(source['root']).absolute()
        root = requested.resolve(strict=True)
        if requested.drive.upper() != 'F:' or root.drive.upper() != 'F:' or str(root).casefold() in seen:
            raise RuntimeError('MinerU runtime source escaped local F drive or is duplicated')
        seen.add(str(root).casefold())
        files = source['files']
        if not isinstance(files, dict):
            raise RuntimeError('MinerU runtime module inventory is invalid')
        for name, item in files.items():
            relative = _relative(name)
            if (relative.suffix.lower() not in {'.dll', '.pyd', '.exe'}
                    or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                    or type(item['bytes']) is not int or item['bytes'] < 0):
                raise RuntimeError('MinerU runtime module identity is invalid')
        sources.append((root, files))
    return sources


def _registered_sources(bundle):
    """Accept only exact module hashes in the startup-verified private bundle."""
    record_path = bundle / 'runtime-sources.json'
    if not record_path.exists():
        return _mineru_sources(bundle)
    try:
        declaration, _ = _read_json(bundle / 'bundle.manifest.json', 1_048_576)
        declared = {}
        for item in declaration['files']:
            name = str(_relative(item['path']))
            if name in declared:
                raise ValueError('Duplicate bundle payload')
            declared[name] = item['sha256']
        if declaration['app_id'] != 'ocr':
            raise ValueError('Runtime sources must belong to OCR')
        records = {}
        for name in ('native-binding.json', 'runtime-sources.json'):
            path = (bundle / name).resolve(strict=True)
            if not path.is_relative_to(bundle):
                raise ValueError('Runtime declaration escapes private bundle')
            record, data = _read_json(path, 4_194_304)
            if hashlib.sha256(data).hexdigest() != declared.get(name):
                raise ValueError('Registered runtime declaration changed')
            records[name] = record
        binding, record = records['native-binding.json'], records['runtime-sources.json']
        asset = Path(binding['asset_bundle']).resolve(strict=True)
        if (binding['app_id'] != 'ocr' or asset.drive.upper() != 'F:'
                or bundle.drive.upper() != 'F:' or asset == bundle
                or asset.is_relative_to(bundle) or bundle.is_relative_to(asset)
                or record['schema_version'] != 1
                or Path(record['source_bundle']).resolve(strict=True) != asset
                or not re.fullmatch('[0-9a-f]{64}', record['candidate_sha256'])
                or record['candidate_sha256'] != binding['asset_candidate_sha256']):
            raise ValueError('Runtime source identity differs from registered assets')
        sources, seen = [], set()
        for source in record['sources']:
            relative = _relative(source['path'])
            if (len(relative.parts) < 2 or relative.parts[0] != 'runtimes'
                    or str(relative) in seen):
                raise ValueError('Runtime alias is outside the declared runtime tree')
            seen.add(str(relative))
            alias = bundle.joinpath(*relative.parts)
            target = asset.joinpath(*relative.parts).resolve(strict=True)
            if (not (alias.is_junction() or alias.is_symlink())
                    or not target.is_relative_to(asset) or alias.resolve(strict=True) != target):
                raise ValueError('Runtime alias target changed')
            files = source['files']
            if not isinstance(files, dict):
                raise ValueError('Invalid runtime module inventory')
            for name, item in files.items():
                module = _relative(name)
                if (module.suffix.lower() not in {'.dll', '.pyd', '.exe'}
                        or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                        or type(item['bytes']) is not int or item['bytes'] < 0):
                    raise ValueError('Invalid runtime module identity')
            sources.append((target, files))
        return sources + _mineru_sources(bundle)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f'Registered runtime sources are invalid: {error}') from error


def audit_module_paths(bundle, paths, windows, defender):
    """Classify real module paths; linked modules also require sealed file hashes."""
    bundle, windows, defender = (Path(path).resolve() for path in (bundle, windows, defender))
    external, security_modules, verified, loaded = [], [], [], []
    sources = None
    for value in paths:
        path = Path(value).resolve()
        loaded.append(str(path))
        if path.is_relative_to(defender) and path.name.lower() == 'mpoav.dll':
            security_modules.append(str(path))
            continue
        if path.is_relative_to(windows):
            if path.name.lower().startswith(('cudnn', 'cublas', 'cudart', 'msvcp140', 'vcruntime140', 'python3')):
                external.append(str(path))
            continue
        if path.is_relative_to(bundle):
            continue
        if sources is None:
            sources = _registered_sources(bundle)
        accepted = False
        for target, files in sources:
            if not path.is_relative_to(target):
                continue
            item = files.get(path.relative_to(target).as_posix())
            if item is None:
                continue
            if path.stat().st_size != item['bytes']:
                raise RuntimeError(f'Registered runtime module integrity changed: {path}')
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != item['sha256']:
                raise RuntimeError(f'Registered runtime module integrity changed: {path}')
            verified.append({'path': str(path), 'sha256': digest})
            accepted = True
            break
        if not accepted:
            external.append(str(path))
    if external:
        raise RuntimeError(f'Unbundled application runtime DLLs were loaded: {external}')
    return {'loaded_modules': sorted(loaded), 'os_security_modules': security_modules,
            'non_system_modules_inside_bundle': not verified,
            'verified_runtime_source_modules': verified}


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
    paths=[]
    windows=Path(os.environ['SystemRoot']).resolve()
    defender=(Path(os.environ['ProgramData'])/'Microsoft/Windows Defender/Platform').resolve()
    for handle in modules[:needed.value//ctypes.sizeof(wintypes.HMODULE)]:
        buffer=ctypes.create_unicode_buffer(32768)
        length=api.GetModuleFileNameW(handle,buffer,len(buffer))
        if not length or length>=len(buffer):
            raise RuntimeError('Could not resolve a loaded module')
        paths.append(buffer.value)
    return {'python': sys.version, 'executable': sys.executable,
            **audit_module_paths(bundle, paths, windows, defender)}
