"""Bind one portable package to one DATA using the existing native supervisor.

Only generated machine launch files live in DATA. API and workers import the
same sealed current source, while immutable runtimes and models stay in package.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil

from packages.runtime import require_path


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path = require_path(path, 'Local launch binding', role='write')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_launcher(path, app_id):
    if app_id not in {'ocr', 'classification'}:
        raise ValueError('Unknown native application')
    path = require_path(path, 'Native launcher', role='write')
    # Environment substitution avoids the batch file's code page and prevents
    # literal percent/bang characters in the runtime path becoming cmd syntax.
    path.write_text('@echo off\r\nsetlocal DisableDelayedExpansion\r\n"%CLEAR_' +
        app_id.upper() + '_CONTROL_PYTHON%" -I -B -u "%~dp0server.py"\r\n', encoding='ascii')


def verify_binding(binding, config):
    """A local binding selects only this package's exact app/source identities."""
    app_id = binding.get('app_id')
    if app_id not in {'ocr', 'classification'} or binding.get('installation_id') != config.installation_id:
        raise ValueError('Native launch binding belongs to another installation')
    source = require_path(Path(binding['source']) / 'src', 'Native source', role='read')
    asset = require_path(binding['asset'], 'Native asset bundle', role='read')
    expected_asset = config.resource('program_assets') / 'bundles' / app_id
    expected_source = config.resource(app_id + '_source') / 'src'
    expected_web = expected_source.parent / ('frontend/dist' if app_id == 'ocr' else 'apps/web/dist')
    if (source != expected_source or asset != expected_asset
            or Path(binding['platform']) != config.resource('platform_source')
            or Path(binding['data_root']) != config.data
            or Path(binding['worker_source']) != source
            or Path(binding['web_root']) != expected_web):
        raise ValueError('Native binding paths differ from registered package resources')
    if sha(asset / 'bundle.manifest.json') != binding.get('content_manifest_sha256'):
        raise ValueError('Native asset manifest identity changed')
    declared = set()
    for item in binding['worker_source_files']:
        relative = Path(item['path'])
        member = require_path(source / relative, 'Native source member', role='read')
        if (relative.is_absolute() or '..' in relative.parts or item['path'] in declared
                or not member.is_relative_to(source) or sha(member) != item['sha256']):
            raise ValueError('Native API/worker source identity changed')
        declared.add(item['path'])
    package = source / ('ocr_workbench' if app_id == 'ocr' else 'image_platform')
    actual = {member.relative_to(source).as_posix() for member in package.rglob('*')
              if member.is_file() and '__pycache__' not in member.parts}
    if not actual or actual != declared:
        raise ValueError('Native source contains undeclared or missing modules')
    return source, asset


SERVER = '''import hashlib,json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
binding=json.loads((root/'binding.json').read_text('utf-8'))
sys.path[:0]=[binding['platform'],str(Path(binding['platform'])/'src'),str(Path(binding['source'])/'src')]
from packages.runtime import current_install,require_path
config=current_install()
if config is None or config.installation_id!=binding['installation_id']:
    raise ValueError('Native launch requires its own installation configuration')
sys.path.insert(0,str(Path(binding['platform'])/'scripts'))
from portable_native import verify_binding
source,asset=verify_binding(binding,config)
data=require_path(config.data/'native'/binding['app_id'],'Native DATA',role='write')
data.mkdir(parents=True,exist_ok=True)
from packages.runtime.cli import DrainGate
def drain_app(module):
    original=module.create_app
    def create_app(*args,**kwargs):
        app=original(*args,**kwargs)
        app.add_middleware(DrainGate,data=config.data)
        return app
    module.create_app=create_app
if binding['app_id']=='ocr':
    os.environ['CLEAR_OCR_WORKER_SOURCE']=str(source)
    os.environ['CLEAR_OCR_WORKER_SOURCE_MANIFEST']=str(root/'binding.json')
    os.environ['CLEAR_OCR_WORKER_BINDING_SHA256']=hashlib.sha256((root/'binding.json').read_bytes()).hexdigest()
    from ocr_workbench import service
    if os.environ.get('CLEAR_EXECUTION_MODE')=='worker':
        from workbench_platform.tasks import ocr_extension,native_import_extension
        ocr_extension.install(service)
        native_import_extension.install(service,'ocr')
    from workbench_platform.resources.native import install_ocr_service
    install_ocr_service(service)
    drain_app(service)
    sys.argv=['ocr-workbench','--bundle',str(asset),'--web-root',binding['web_root'],
        '--data',str(data),'--token-file',str(config.data/'ocr-token.txt')]
    service.main()
else:
    import subprocess
    os.environ['ICP_DATA_DIR']=str(data)
    os.environ['HF_HOME']=str(config.roots['cache']/'classification-huggingface')
    sys.path.insert(0,str(asset/'src/image_platform'))
    from platform_weights import install_weights
    install_weights(asset,data)
    from image_platform import runtime,settings as native_settings
    runtime.REPO=native_settings.REPO=asset
    from image_platform import api
    api.REPO=source.parent
    if os.environ.get('CLEAR_EXECUTION_MODE')=='worker':
        from workbench_platform.tasks import native_import_extension
        native_import_extension.install(api,'classification')
    drain_app(api)
    from image_platform.cli import main
    with (data/'worker.log').open('ab') as log:
        child=subprocess.Popen([sys.executable,'-I','-B','-u',str(root/'worker.py')],cwd=root,
            env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            sys.argv=['image-platform','serve']
            main()
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=20)
'''

WORKER = '''import hashlib,json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
binding=json.loads((root/'binding.json').read_text('utf-8'))
sys.path[:0]=[binding['platform'],str(Path(binding['platform'])/'src'),str(Path(binding['source'])/'src')]
from packages.runtime import current_install,require_path
config=current_install()
if config is None or config.installation_id!=binding['installation_id']:
    raise ValueError('Worker installation differs')
sys.path.insert(0,str(Path(binding['platform'])/'scripts'))
from portable_native import verify_binding
source,asset=verify_binding(binding,config)
from image_platform import runtime,settings as native_settings
runtime.REPO=native_settings.REPO=asset
from workbench_platform.resources.native import install
from image_platform import platform_resources
install(platform_resources,app_id='classification')
from image_platform.cli import main
sys.argv=['image-platform','worker']+sys.argv[1:]
main()
'''


def configure(config):
    from workbench_platform.catalog.registry import AppRegistry
    from workbench_platform.catalog import routes
    os.environ.update(config.environment())
    data, platform = config.data, config.resource('platform_source')
    program = config.resource('program_assets')
    os.environ.update(WORKBENCH_DATA_DIR=str(data), WORKBENCH_PROGRAM_ROOT=str(program),
        WORKBENCH_NATIVE_STARTUP_SECONDS='120', WORKBENCH_AGENT_ENABLED='1')
    bundle_root = require_path(data / 'installation/native/bundles', role='write')
    declarations = require_path(data / 'installation/native/config', role='write')
    declarations.mkdir(parents=True, exist_ok=True)
    for app_id in ('ocr', 'classification'):
        source = config.resource(app_id + '_source')
        asset = require_path(program / 'bundles' / app_id, role='read')
        runtime = require_path(asset / 'runtimes/control/python.exe', role='read')
        web = source / ('frontend/dist' if app_id == 'ocr' else 'apps/web/dist')
        if not runtime.is_file() or not (web / 'index.html').is_file():
            raise ValueError(f'Missing {app_id} offline runtime or frontend; repair package')
        bundle = bundle_root / app_id
        bundle.mkdir(parents=True, exist_ok=True)
        worker_files = [{'path': member.relative_to(source / 'src').as_posix(), 'sha256': sha(member)}
            for member in sorted((source / 'src' / ('ocr_workbench' if app_id == 'ocr' else 'image_platform')).rglob('*'))
            if member.is_file() and '__pycache__' not in member.parts]
        binding = {'schema_version': 1, 'installation_id': config.installation_id, 'app_id': app_id,
            'data_root': str(data), 'platform': str(platform), 'source': str(source),
            'asset': str(asset), 'web_root': str(web), 'worker_source': str(source / 'src'),
            'worker_source_files': worker_files, 'content_manifest_sha256': sha(asset / 'bundle.manifest.json')}
        save(bundle / 'binding.json', binding)
        (bundle / 'server.py').write_text(SERVER, encoding='utf-8')
        (bundle / 'worker.py').write_text(WORKER, encoding='utf-8')
        write_launcher(bundle / 'launch.cmd', app_id)
        save(bundle / 'bundle.manifest.json', {'app_id': app_id, 'files': [
            {'path': member.name, 'sha256': sha(member)} for member in sorted(bundle.iterdir())
            if member.is_file() and member.name != 'bundle.manifest.json']})
        shutil.copyfile(platform / f'config/apps/{app_id}.json', declarations / f'{app_id}.json')
        save(declarations / f'{app_id}.install.json', {'app_id': app_id, 'installed': True,
            'manifest_sha256': sha(declarations / f'{app_id}.json'), 'launch': {
                'bundle_root': str(bundle), 'executable': str(bundle / 'launch.cmd'),
                'working_directory': str(bundle), 'allowed_arguments': [],
                'executable_sha256': sha(bundle / 'launch.cmd'),
                'bundle_manifest_sha256': sha(bundle / 'bundle.manifest.json')}})
    token = require_path(data / 'ocr-token.txt', role='write')
    if not token.exists():
        token.write_text(secrets.token_urlsafe(48), encoding='utf-8')
    os.environ['WORKBENCH_OCR_TOKEN_FILE'] = str(token)
    registry = AppRegistry(declarations, install_root=bundle_root)
    if registry.failures:
        raise ValueError('Portable native app registration failed: ' + str(registry.failures))
    routes._registry = registry
    return registry


def create_app(config):
    configure(config)
    from workbench_platform.app import app
    return app
