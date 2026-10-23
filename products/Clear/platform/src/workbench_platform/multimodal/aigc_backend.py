"""Independent, locally pinned whole-image detector assets and worker contract.

Asset identities are engineering evidence, never accuracy qualification. Jobs keep
their inventory, runtime and adapter identities across retries and default changes.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

from workbench_platform.resources.recovery import local_path

KIND = 'aigc_image'
ENGINES = frozenset({'community_forensics384', 'spai', 'univfd'})
SOURCE = Path(__file__).resolve().parents[3]
INPUT_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
PRODUCT_SOURCES = ('modules/manual_multimodal/aigc_worker.py',
    'modules/manual_multimodal/aigc_processing.py', 'modules/manual_multimodal/worker_protocol.py',
    'modules/manual_multimodal/worker.py', 'modules/manual_multimodal/image_geometry.py',
    'packages/contracts/multimodal.py', 'src/workbench_platform/multimodal/aigc_backend.py',
    'src/workbench_platform/multimodal/service.py', 'src/workbench_platform/multimodal/runner.py',
    'src/workbench_platform/multimodal/qualification.py', 'src/workbench_platform/resources/recovery.py')


def asset_root(config):
    return local_path(os.environ.get('WORKBENCH_AIGC_ROOT', config.get('aigc_image', {}).get(
        'asset_root', 'F:/OCR/output/aigc-image-detection-20261022')))


def member(root, name):
    """An inventory member is relative and cannot redirect outside its F root."""
    if not isinstance(name, str) or not name:
        raise ValueError('Empty AIGC inventory member')
    relative = PurePosixPath(name)
    if relative.is_absolute() or ':' in name or '\\' in name or any(part in {'', '.', '..'} for part in name.split('/')):
        raise ValueError('Invalid AIGC inventory member: ' + name)
    path = local_path(root / relative)
    if not path.is_relative_to(root):
        raise ValueError('AIGC resource escapes its inventory root')
    return path


def runtime_entries(directory):
    """Executable closure excludes bytecode caches only; code, data and DLLs stay pinned."""
    directory=local_path(directory)
    pending=[directory]
    while pending:
        parent=pending.pop()
        with os.scandir(parent) as scan:entries=sorted(scan,key=lambda entry:entry.name)
        for entry in entries:
            path=Path(entry.path)
            identity=entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(identity.st_mode) or getattr(identity,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('Linked AIGC runtime dependency is not permitted: '+str(path))
            if stat.S_ISDIR(identity.st_mode):
                if entry.name!='__pycache__':pending.append(path)
                continue
            if path.suffix.lower() in {'.pyc','.pyo'}:
                raise ValueError('Unpinned sourceless bytecode is not permitted in an AIGC import root: '+str(path))
            if stat.S_ISREG(identity.st_mode):yield path,identity


def runtime_members(directory):
    for path,_identity in runtime_entries(directory):yield path


def verify_runtime(service, metadata):
    resources=metadata.get('runtime_resources')
    directories=metadata.get('runtime_closed_directories')
    if not isinstance(resources,dict) or not resources or not isinstance(directories,list) or not directories:
        raise ValueError('AIGC transitive runtime closure has not been pinned')
    closed=[local_path(name) for name in directories]
    if len(closed)!=len(set(closed)):
        raise ValueError('Duplicate AIGC runtime closure root')
    executable=local_path(metadata['runtime'])
    lines=[line.strip() for line in (executable.parent/'python312._pth').read_text('utf-8-sig').splitlines()
        if line.strip() and not line.lstrip().startswith('#')]
    if lines[:2]!=['python312.zip','.'] or lines[-1:]!=['import site'] or any(line.startswith('import ') for line in lines[:-1]):
        raise ValueError('AIGC embedded runtime import configuration is not explicit')
    imports={executable.parent,*[local_path(Path(line) if Path(line).is_absolute() else executable.parent/line) for line in lines[2:-1]]}
    if set(closed)!=imports:
        raise ValueError('AIGC runtime closure differs from its embedded Python import roots')
    actual={path:identity for root in closed for path,identity in runtime_entries(root)}
    declared={Path(name).absolute():identity for name,identity in resources.items()}
    if set(actual)!=set(declared):
        raise ValueError('AIGC runtime closure has missing or undeclared files')
    uncached=[]
    for path,identity in declared.items():
        metadata=actual[path]
        key=(str(path),metadata.st_size,metadata.st_mtime_ns)
        digest=service.resource_hash_cache.get(key)
        if metadata.st_size!=identity['bytes']:
            raise ValueError('Pinned AIGC runtime dependency changed: '+str(path))
        if digest is None:uncached.append((path,key,identity['sha256']))
        elif digest!=identity['sha256']:
            raise ValueError('Pinned AIGC runtime dependency changed: '+str(path))
    if uncached:
        def hash_member(row):
            path,key,expected=row
            before=path.stat()
            if (before.st_size,before.st_mtime_ns)!=(key[1],key[2]):
                raise ValueError('AIGC runtime dependency changed during verification: '+str(path))
            digest=service.file_hash(path)
            after=path.stat()
            if (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
                raise ValueError('AIGC runtime dependency changed during verification: '+str(path))
            if digest!=expected:raise ValueError('Pinned AIGC runtime dependency changed: '+str(path))
            return key,digest
        # Read every pinned byte. Independent small-file opens dominate first
        # admission on Windows, so bound parallel I/O while preserving hashes.
        with ThreadPoolExecutor(max_workers=8,thread_name_prefix='aigc-integrity') as pool:
            for key,digest in pool.map(hash_member,uncached):service.resource_hash_cache[key]=digest


def identity_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def public_identity(binding):
    """Small content identities; the full private closure stays in the task journal."""
    if not binding:return None
    if binding.get('identity_format')=='clear-aigc-public-identity-v1':return copy.deepcopy(binding)
    cached=binding.get('public_identity')
    if isinstance(cached,dict) and cached.get('identity_format')=='clear-aigc-public-identity-v1':
        return copy.deepcopy(cached)
    def closure(resources):
        return {'sha256':identity_hash(resources),'file_count':len(resources),
            'bytes':sum(row.get('bytes',0) for row in resources.values())}
    resources=binding.get('resources',{})
    return {'identity_format':'clear-aigc-public-identity-v1',
        **{key:binding.get(key) for key in ('engine','version','code_revision','preprocessing_version',
            'runtime_sha256','inventory_sha256')},
        'weight_hashes':{name:row['sha256'] for name,row in resources.items()
            if name.lower().endswith(('.pt','.pth','.safetensors','.onnx'))},
        'resource_closure':closure(resources),'runtime_closure':closure(binding.get('runtime_resources',{})),
        'runtime_import_roots_sha256':identity_hash(binding.get('runtime_closed_directories',[])),
        'adapter_sources_sha256':identity_hash(binding.get('product_source_hashes',{})),
        'calibration_sha256':identity_hash(binding['calibration']) if binding.get('calibration') else None,
        'qualification':'experimental','business_accuracy_qualified':False}


def verify_source_closure(root, settings, resources):
    for key in ('source_root','clip_source_root','dependencies'):
        if not settings.get(key):continue
        name=settings[key]
        directory=member(root,name)
        actual={path.relative_to(root).as_posix() for path in runtime_members(directory)}
        expected={path for path in resources if path.startswith(name.rstrip('/')+'/')}
        if actual!=expected:
            raise ValueError('AIGC import source closure has missing or undeclared files: '+name)


def bind(service, engine=None):
    root = asset_root(service.config)
    inventory_path = member(root, 'aigc-inventory.json')
    inventory = json.loads(inventory_path.read_text('utf-8-sig'))
    if inventory.get('format') != 'clear-aigc-assets-v1':
        raise ValueError('Unknown AIGC asset inventory format')
    engine = engine or os.environ.get('WORKBENCH_AIGC_ENGINE', service.config.get('aigc_image', {}).get(
        'default_engine', inventory.get('primary', 'community_forensics384')))
    if engine not in ENGINES:
        raise ValueError('Unknown AIGC detector engine: ' + str(engine))
    selected = inventory.get('engines', {}).get(engine)
    if not isinstance(selected, dict):
        raise ValueError('AIGC engine is not installed: ' + engine)
    resources = selected.get('resources')
    if not isinstance(resources, dict) or not resources:
        raise ValueError('AIGC engine must pin its source and weight resources')
    for name, identity in resources.items():
        if not service.resource_matches(member(root, name), identity):
            raise ValueError('AIGC resource is missing or changed: ' + name)
    settings = copy.deepcopy(selected.get('settings', {}))
    for name in ('source_root', 'clip_source_root', 'checkpoint', 'clip_checkpoint', 'dino_checkpoint', 'config', 'dependencies', 'calibration_file'):
        if settings.get(name):
            path = member(root, settings[name])
            if not path.exists():
                raise ValueError('AIGC configured path is missing: ' + name)
            if path.is_file() and settings[name] not in resources:
                raise ValueError('AIGC configured file is not hash-pinned: ' + name)
    for name in settings.get('native_paths', {}).values():
        member(root, name)
    verify_source_closure(root,settings,resources)
    executable = local_path(selected.get('runtime', inventory.get('runtime', '')))
    if not executable.is_file():
        raise ValueError('AIGC isolated runtime is missing')
    verify_runtime(service,{**inventory,'runtime':str(executable)})
    calibration = None
    if settings.get('calibration_file'):
        calibration = json.loads(member(root, settings['calibration_file']).read_text('utf-8-sig'))
    binding={'engine': engine, 'version': selected['version'], 'code_revision': selected['code_revision'],
        'preprocessing_version': selected['preprocessing_version'], 'asset_root': str(root),
        'inventory_sha256': service.qualification.file_hash(inventory_path),
        'runtime': str(executable), 'runtime_sha256': service.qualification.file_hash(executable),
        'runtime_resources':copy.deepcopy(inventory['runtime_resources']),
        'runtime_closed_directories':copy.deepcopy(inventory['runtime_closed_directories']),
        'product_source_hashes': {name: service.qualification.file_hash(SOURCE / name) for name in PRODUCT_SOURCES},
        'resources': copy.deepcopy(resources), 'settings': settings, 'calibration': calibration,
        'business_accuracy_qualified': False, 'qualification': 'experimental'}
    binding['public_identity']=public_identity(binding)
    return binding


def item_binding(item):
    value = item.get('model_binding')
    if not isinstance(value, dict) or value.get('engine') not in ENGINES:
        raise ValueError('AIGC task has no supported frozen detector identity')
    return copy.deepcopy(value)


def verify(service, binding):
    if binding.get('engine') not in ENGINES:
        raise ValueError('Unknown persisted AIGC detector engine')
    if binding.get('public_identity') is not None and binding['public_identity']!=public_identity(
            {key:value for key,value in binding.items() if key!='public_identity'}):
        raise ValueError('AIGC public model identity disagrees with the private frozen closure')
    root = local_path(binding['asset_root'])
    if service.qualification.file_hash(member(root, 'aigc-inventory.json')) != binding['inventory_sha256']:
        raise ValueError('Pinned AIGC inventory changed; historical retry was not substituted')
    if service.qualification.file_hash(local_path(binding['runtime'])) != binding['runtime_sha256']:
        raise ValueError('Pinned AIGC runtime changed; requalification is required')
    verify_runtime(service,binding)
    for name, expected in binding.get('product_source_hashes', {}).items():
        if service.qualification.file_hash(member(SOURCE, name)) != expected:
            raise ValueError('Pinned AIGC product adapter changed: ' + name)
    for name, identity in binding['resources'].items():
        if not service.resource_matches(member(root, name), identity):
            raise ValueError('Pinned AIGC resource changed; create a new analysis after requalification: ' + name)
    verify_source_closure(root,binding.get('settings',{}),binding['resources'])


def worker_entry(kind, binding):
    """A binding does not imply SAFIRE: historical CAT-Net stays its own worker."""
    if kind == KIND:
        if not binding or binding.get('engine') not in ENGINES:
            raise ValueError('AIGC worker requires an AIGC detector binding')
        return 'aigc_worker.py'
    return 'safire_worker.py' if binding and binding.get('engine') == 'safire' else 'worker.py'


def worker_command(runtime, worker, request_path):
    """Explicit flags also isolate reads from pre-existing unpinned pyc caches."""
    runtime,worker,request_path=map(local_path,(runtime,worker,request_path))
    prefix=local_path(request_path.parent/'runtime'/('pycache-'+uuid.uuid4().hex))
    prefix.mkdir(parents=True,exist_ok=False)
    return [str(runtime),'-B','-X','pycache_prefix='+str(prefix),str(worker),str(request_path)]
