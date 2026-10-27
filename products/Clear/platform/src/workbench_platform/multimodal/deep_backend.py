"""Explicit engine binding for new jobs; retries never follow a changed default."""
from __future__ import annotations
import copy
import os
from pathlib import Path

from workbench_platform.resources.recovery import local_path

DEEP_KINDS = frozenset({'catnet','document_forensics'})
INPUT_SUFFIXES = {'.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff'}
SOURCE = Path(__file__).resolve().parents[3]
PRODUCT_SOURCES = ('modules/manual_multimodal/safire_worker.py','modules/manual_multimodal/safire_processing.py',
    'modules/manual_multimodal/runtime_paths.py', 'packages/runtime/config.py',
    'modules/manual_multimodal/worker.py','modules/manual_multimodal/worker_protocol.py',
    'modules/manual_multimodal/image_geometry.py','src/workbench_platform/multimodal/deep_backend.py',
    'src/workbench_platform/multimodal/service.py','src/workbench_platform/multimodal/runner.py',
    'src/workbench_platform/multimodal/qualification.py','src/workbench_platform/resources/recovery.py')


def backend(config):
    value = os.environ.get('WORKBENCH_DEEP_FORENSICS_BACKEND',
                           config.get('deep_forensics',{}).get('default_backend','catnet')).lower()
    if value not in {'safire','catnet'}:
        raise ValueError('Unknown deep forensics backend: '+value)
    return value


def asset_root(config):
    from packages.runtime import resource_path, current_install
    if not current_install() and config.get('deep_forensics', {}).get('asset_root') and not os.environ.get('WORKBENCH_SAFIRE_ROOT'):
        return local_path(config['deep_forensics']['asset_root'])
    return resource_path('safire', environment='WORKBENCH_SAFIRE_ROOT')


def runtime(config):
    from packages.runtime import resource_path, current_install
    if not current_install() and config.get('deep_forensics', {}).get('runtime') and not os.environ.get('WORKBENCH_SAFIRE_PYTHON'):
        return local_path(config['deep_forensics']['runtime'])
    return resource_path('safire_python', environment='WORKBENCH_SAFIRE_PYTHON')


def assets(config, hash_file):
    root = asset_root(config)
    required = [root/'scripts/safire_native.py',root/'scripts/safire_metrics.py',root/'scripts/run_native.py',
                root/'models/safire.pth',root/'models/sam_vit_b_01ec64.pth',
                root/'evidence/safire.pth.download.json',root/'evidence/sam_vit_b_01ec64.pth.download.json']
    upstream = root/'upstream/SAFIRE-ec2af6af8c789fb6359493a3e352f89c6fd8107f'
    if not upstream.is_dir() or not (root/'dependencies').is_dir():
        raise ValueError('SAFIRE pinned source or dependency overlay is missing')
    required += sorted(upstream.rglob('*.py'))
    required += sorted(path for path in (root/'dependencies').rglob('*') if path.is_file()
                       and path.suffix.lower() in {'.py','.pyd','.dll','.json','.txt','.pth'}
                       and '__pycache__' not in path.parts)
    result = {}
    for path in required:
        real = path.resolve()
        if not real.is_relative_to(root) or not real.is_file():
            raise ValueError('Missing or escaped SAFIRE resource: '+str(path))
        result[path.relative_to(root).as_posix()] = {'bytes':path.stat().st_size,'sha256':hash_file(path)}
    return result


def bind(service, engine=None):
    engine = engine or backend(service.config)
    if engine == 'safire':
        return {'engine':'safire','version':'clear-safire-product-v1',
            'resource_id':'safire',
            'asset_root':str(asset_root(service.config)),'runtime':str(runtime(service.config)),
            'runtime_sha256':service.qualification.file_hash(runtime(service.config)),
            'product_source_hashes':{name:service.qualification.file_hash(SOURCE/name) for name in PRODUCT_SOURCES},
            'resources':assets(service.config, service.qualification.file_hash),
            'pixel_threshold':.9,'calibration':None,'operational_trigger':'any_candidate_region',
            'page_trigger_threshold':0.0,'business_accuracy_qualified':False}
    return {'engine':'catnet','version':'legacy-catnet-v2',
        'runtime':str(service.program/'runtimes/catnet/python.exe'),
        'model_sha256':service.file_hash(service.models/'catnet/CAT_full_v2.pth.tar')}


def item_binding(service, item):
    value = item.get('model_binding')
    # No engine binding in old persisted jobs means the historical CAT-Net route.
    binding=copy.deepcopy(value) if value else bind(service, 'catnet')
    from packages.runtime import current_install
    installation=current_install()
    if installation and binding.get('engine')=='safire' and binding.get('resource_id')=='safire':
        # All frozen resources, adapter hashes and executable hashes are still
        # verified by verify(); only the local launch address is regenerated.
        binding['asset_root']=str(installation.resource('safire'))
        binding['runtime']=str(installation.resource('safire_python'))
    return binding


def verify(service, binding):
    if binding['engine'] == 'safire':
        if binding.get('runtime_sha256') and service.qualification.file_hash(local_path(binding['runtime']))!=binding['runtime_sha256']:
            raise ValueError('Pinned SAFIRE runtime changed; requalification is required')
        for name,expected in binding.get('product_source_hashes',{}).items():
            path=local_path(SOURCE/name)
            if not path.is_relative_to(SOURCE) or service.qualification.file_hash(path)!=expected:
                raise ValueError('Pinned SAFIRE product adapter changed: '+name)
        # Verify the persisted root, not today's environment override/default.
        root = local_path(binding['asset_root'])
        for name, identity in binding['resources'].items():
            path = local_path(root/name)
            if not path.is_relative_to(root) or not service.resource_matches(path,identity):
                raise ValueError('Pinned SAFIRE resource changed; create a new analysis after requalification: '+name)
    elif binding['engine'] == 'catnet':
        if binding['model_sha256'] != service.file_hash(service.models/'catnet/CAT_full_v2.pth.tar'):
            raise ValueError('Pinned CAT-Net model changed; historical retry was not substituted')
    else:
        raise ValueError('Unknown persisted detector engine')
