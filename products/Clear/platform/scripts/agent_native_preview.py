"""Private native launchers over F-drive assets; never rewrite shared bindings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time

from workbench_platform.catalog.registry import AppRegistry
from workbench_platform.catalog import routes
from workbench_platform.supervisor.processes import require_f_path, storage_environment
from scripts.classification_worker_compat import validate as classification_store_compatibility


SERVER = '''import json,os,subprocess,sys,hashlib
from pathlib import Path
root=Path(__file__).resolve().parent
binding=json.loads((root/'binding.json').read_text(encoding='utf-8'))
data_root=Path(binding['data_root'])
data=data_root/'native'/binding['app_id']
data.mkdir(parents=True,exist_ok=True)
source=Path(binding['source'])
sys.path[:0]=[str(source/'src')]
asset=Path(binding['asset'])
support=binding['resource_support']
support_root=Path(support['root']).resolve()
for relative,expected in support['files'].items():
    member=(support_root/relative).resolve()
    if not member.is_relative_to(support_root) or member.drive.casefold()!='f:' or hashlib.sha256(member.read_bytes()).hexdigest()!=expected:
        raise ValueError('Native resource support identity changed')
sys.path.insert(0,str(support_root/'src'))
import_extension=None
if binding.get('import_extension'):
    import importlib.util
    if hashlib.sha256(Path(binding['import_extension']).read_bytes()).hexdigest()!=binding['import_extension_sha256']:
        raise ValueError('Native import extension identity changed')
    spec=importlib.util.spec_from_file_location('clear_native_import_extension',binding['import_extension'])
    import_extension=importlib.util.module_from_spec(spec);spec.loader.exec_module(import_extension)
if binding['app_id']=='ocr':
    worker_source=Path(binding['worker_source']).resolve()
    if worker_source.drive.casefold()!='f:' or not worker_source.is_relative_to(root):
        raise ValueError('OCR worker source must remain in the private F drive launch bundle')
    for item in binding['worker_source_files']:
        member=(worker_source/item['path']).resolve()
        if not member.is_relative_to(worker_source) or hashlib.sha256(member.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('OCR worker source identity changed')
        api_member=(source/'src'/item['path']).resolve()
        if not api_member.is_relative_to(source/'src') or hashlib.sha256(api_member.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('OCR API source differs from the fixed worker source')
    declared_source={item['path'] for item in binding['worker_source_files']}
    if {member.relative_to(source/'src').as_posix() for member in (source/'src/ocr_workbench').rglob('*.py')}!=declared_source:
        raise ValueError('OCR API source has missing or undeclared modules')
    os.environ['CLEAR_OCR_WORKER_SOURCE']=str(worker_source)
    os.environ['CLEAR_OCR_WORKER_SOURCE_MANIFEST']=str(root/'binding.json')
    os.environ['CLEAR_OCR_WORKER_BINDING_SHA256']=hashlib.sha256((root/'binding.json').read_bytes()).hexdigest()
    from ocr_workbench import service
    if binding.get('task_extension'):
        import importlib.util
        import hashlib
        if hashlib.sha256(Path(binding['task_extension']).read_bytes()).hexdigest()!=binding['task_extension_sha256']:
            raise ValueError('OCR task extension identity changed')
        spec=importlib.util.spec_from_file_location('clear_task_ocr_extension',binding['task_extension'])
        extension=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extension)
        extension.install(service)
    if import_extension:import_extension.install(service,'ocr')
    from workbench_platform.resources.native import install_ocr_service
    install_ocr_service(service)
    sys.argv=['ocr-workbench','--bundle',str(asset),'--web-root',binding['web_root'],'--data',str(data),'--token-file',str(data_root/'ocr-token.txt')]
    service.main()
else:
    import importlib.util
    compatibility=binding['store_compatibility']
    adapter_path=Path(compatibility['adapter_path'])
    if hashlib.sha256(adapter_path.read_bytes()).hexdigest()!=compatibility['adapter_sha256']:
        raise ValueError('Classification Store adapter identity changed')
    spec=importlib.util.spec_from_file_location('clear_classification_store_compat',adapter_path)
    adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
    adapter.verify(binding)
    os.environ['ICP_DATA_DIR']=str(data)
    os.environ['HF_HOME']=str(data/'cache/huggingface')
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    weights=asset/'resources/weights/manifest.json'
    if weights.is_file():
        declaration=json.loads(weights.read_text(encoding='utf-8'))
        for encoder,item in declaration['models'].items():
            original=asset/'resources/weights'/encoder/'model.safetensors'
            target=data/'cache/huggingface/hub'/('models--timm--'+item['name'])/'snapshots'/item['revision']/'model.safetensors'
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists():
                os.link(original,target)
    sys.path.insert(0,str(asset/'src/image_platform'))
    from platform_weights import install_weights
    install_weights(asset,data)
    # Development API sources include the Agent routes, while their sealed
    # runtime schemas and provenance live beside the packaged worker sources.
    from image_platform import runtime as native_runtime
    native_runtime.REPO=asset
    if import_extension:
        from image_platform import api
        import_extension.install(api,'classification')
    from image_platform.cli import main
    with (data/'worker.log').open('ab') as log:
        worker=subprocess.Popen([sys.executable,'-I','-B','-u',str(root/'worker.py')],
            cwd=root,env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            sys.argv=['image-platform','serve']
            # Keep READY on stdout for the supervisor while preserving native
            # HTTP exceptions in the same private F workspace as worker logs.
            with (data/'server.log').open('a',encoding='utf-8') as server_log:
                sys.stderr=server_log
                main()
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=15)
'''
WORKER = '''import hashlib,importlib.util,json,sys
from pathlib import Path
root=Path(__file__).resolve().parent
binding_bytes=(root/'binding.json').read_bytes()
arguments=sys.argv[1:]
gpu_child=arguments[:1]==['--gpu-child']
if gpu_child:
    if len(arguments)!=3 or arguments[1]!='--binding-sha256' or hashlib.sha256(binding_bytes).hexdigest()!=arguments[2]:
        raise ValueError('Classification GPU child binding identity changed')
elif arguments not in ([],['--once']):
    raise ValueError('Unexpected classification private worker arguments')
binding=json.loads(binding_bytes)
asset=Path(binding['asset']).resolve()
manifest=(asset/'bundle.manifest.json').read_bytes()
if hashlib.sha256(manifest).hexdigest()!=binding['asset_declaration_sha256']:
    raise ValueError('Classification asset declaration identity changed')
source=Path(binding['worker_source']).resolve()
if source!=(asset/'src').resolve() or source.drive.casefold()!='f:':
    raise ValueError('Classification worker source must stay in its verified F asset bundle')
for item in binding['worker_source_files']:
    path=(asset/item['path']).resolve()
    if not path.is_relative_to(asset) or path.drive.casefold()!='f:' or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
        raise ValueError('Classification worker payload identity changed')
declared={item['path'] for item in binding['worker_source_files']}
if any(path.relative_to(asset).as_posix() not in declared for path in source.rglob('*.py')):
    raise ValueError('Classification worker source contains an undeclared module')
sys.path.insert(0,str(source))
support=binding['resource_support']
support_root=Path(support['root']).resolve()
for relative,expected in support['files'].items():
    member=(support_root/relative).resolve()
    if not member.is_relative_to(support_root) or member.drive.casefold()!='f:' or hashlib.sha256(member.read_bytes()).hexdigest()!=expected:
        raise ValueError('Native resource support identity changed')
sys.path.insert(0,str(support_root/'src'))
compatibility=binding['store_compatibility']
adapter_path=Path(compatibility['adapter_path'])
if adapter_path.drive.casefold()!='f:' or adapter_path.resolve().drive.casefold()!='f:' or hashlib.sha256(adapter_path.read_bytes()).hexdigest()!=compatibility['adapter_sha256']:
    raise ValueError('Classification Store adapter identity changed')
spec=importlib.util.spec_from_file_location('clear_classification_store_compat',adapter_path)
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
adapter.install(binding,Path(__file__))
from workbench_platform.resources.native import install
from image_platform import platform_resources
install(platform_resources,app_id='classification')
if gpu_child:
    from image_platform.platform_resources import child_main
    child_main()
    raise SystemExit(0)
from image_platform.cli import main
sys.argv=['image-platform','worker']+arguments
main()
'''


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    require_f_path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _classification_worker_assets(asset):
    """Keep worker sources and export provenance in one verified sealed bundle."""
    declaration = json.loads((asset / 'bundle.manifest.json').read_text(encoding='utf-8'))
    if declaration.get('app_id') != 'classification' or not isinstance(declaration.get('files'), list):
        raise ValueError('Classification asset declaration is invalid')
    files = [item for item in declaration['files']
             if item['path'].startswith(('src/', 'schemas/', 'resources/third-party/'))
             or item['path'] == 'resources/weights/manifest.json']
    declared = {item['path'] for item in files}
    required = {'src/image_platform/__init__.py', 'src/image_platform/settings.py',
                'src/image_platform/cli.py', 'src/image_platform/worker.py',
                'src/image_platform/training.py', 'resources/third-party/APACHE-2.0.txt',
                'schemas/model-metadata.schema.json'}
    if not required <= declared or len(declared) != len(files):
        raise ValueError('Classification worker sources or provenance are missing')
    for item in files:
        path = require_f_path(asset / item['path'])
        if not path.is_relative_to(asset) or not path.is_file() or sha(path) != item['sha256']:
            raise ValueError('Classification worker payload integrity changed')
    source = require_f_path(asset / 'src')
    if any(path.relative_to(asset).as_posix() not in declared for path in source.rglob('*.py')):
        raise ValueError('Classification worker source contains an undeclared module')
    return source, files


def _resource_support():
    support_root = require_f_path(Path(__file__).resolve().parents[1])
    names = ['src/workbench_platform/__init__.py', *[
        'src/workbench_platform/resources/' + name + '.py' for name in ('gpu', 'recovery', 'native', 'reader_features')]]
    return {'root': str(support_root), 'files': {name: sha(require_f_path(support_root / name)) for name in names}}


def _ocr_worker_sources(native_source, bundle):
    """Pin reviewed Python sources privately; keep shared models/runtime read-only."""
    source = require_f_path(native_source / 'src')
    target = require_f_path(bundle / 'worker-source')
    if not target.is_relative_to(bundle.resolve()):
        raise ValueError('Private OCR worker source escapes launch bundle')
    files = []
    for member in sorted((source / 'ocr_workbench').rglob('*.py')):
        real = require_f_path(member)
        if not real.is_relative_to(source):
            raise ValueError('OCR worker input source escapes registered project')
        relative = member.relative_to(source)
        output = require_f_path(target / relative)
        if not output.is_relative_to(target):
            raise ValueError('Private OCR worker output escapes launch bundle')
        output.parent.mkdir(parents=True, exist_ok=True)
        data = real.read_bytes()
        output.write_bytes(data)
        files.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(data).hexdigest()})
    required = {'ocr_workbench/engine_host.py', 'ocr_workbench/worker.py', 'ocr_workbench/adapter.py'}
    if not required <= {item['path'] for item in files}:
        raise ValueError('OCR worker source is incomplete')
    return target, files


def _native_inputs(source, program):
    """Validate both native pages and runtimes before modifying a workspace."""
    inputs = []
    _resource_support()
    for family in ('ocr', 'classification'):
        asset = require_f_path(program / 'bundles' / family)
        native_source = require_f_path(Path(os.environ.get('WORKBENCH_' + family.upper() + '_SOURCE') or source.parent / family))
        web_root = require_f_path(native_source / ('frontend/dist' if family == 'ocr' else 'apps/web/dist'))
        if not (web_root / 'index.html').is_file():
            raise ValueError('Native production frontend missing: ' + family)
        runtime = require_f_path(asset / 'runtimes/control/python.exe')
        if not runtime.is_file() or not (asset / 'bundle.manifest.json').is_file():
            raise ValueError('Existing verified native assets missing')
        if family == 'classification':
            _classification_worker_assets(asset)
            classification_store_compatibility(native_source, asset)
        inputs.append((family, asset, native_source, web_root, runtime))
    return inputs


def configure(source, program, data):
    source, program, data = [require_f_path(value) for value in (source, program, data)]
    native_inputs = _native_inputs(source, program)
    resource_support = _resource_support()
    data.mkdir(parents=True, exist_ok=True)
    os.environ.update(storage_environment(data))
    os.environ.update(WORKBENCH_DATA_DIR=str(data), WORKBENCH_DATA_PYTHON=str(program / 'runtimes/data/python.exe'),
                      WORKBENCH_FORENSICS_PYTHON=str(program / 'runtimes/forensics/python.exe'),
                      WORKBENCH_PROGRAM_ROOT=str(program), WORKBENCH_NATIVE_STARTUP_SECONDS='60')
    bundle_root, config = data / 'agent-native/bundles', data / 'agent-native/config'
    config.mkdir(parents=True, exist_ok=True)
    receipts = []
    for family, asset, native_source, web_root, runtime in native_inputs:
        bundle = bundle_root / family
        bundle.mkdir(parents=True, exist_ok=True)
        binding = {'app_id': family, 'data_root': str(data), 'source': str(native_source), 'asset': str(asset), 'web_root': str(web_root),
                   'resource_support': resource_support,
                   'frontend_index_sha256': sha(web_root / 'index.html'),
                   'runtime_sha256': sha(runtime), 'asset_declaration_sha256': sha(asset / 'bundle.manifest.json')}
        if family == 'ocr':
            worker_source, worker_files = _ocr_worker_sources(native_source, bundle)
            binding.update(worker_source=str(worker_source), worker_source_files=worker_files,
                           worker_source_origin=str(native_source / 'src'))
        if family == 'classification':
            worker_source, worker_files = _classification_worker_assets(asset)
            binding.update(worker_source=str(worker_source), worker_source_files=worker_files,
                           store_compatibility=classification_store_compatibility(native_source, asset),
                           worker_entry_sha256=hashlib.sha256(WORKER.encode('utf-8')).hexdigest())
        if family=='ocr' and os.environ.get('CLEAR_EXECUTION_MODE')=='worker':
            binding['task_extension']=str(source/'src/workbench_platform/tasks/ocr_extension.py')
            binding['task_extension_sha256']=sha(Path(binding['task_extension']))
        if os.environ.get('CLEAR_EXECUTION_MODE')=='worker':
            binding['import_extension']=str(source/'src/workbench_platform/tasks/native_import_extension.py')
            binding['import_extension_sha256']=sha(Path(binding['import_extension']))
        save(bundle / 'binding.json', binding)
        (bundle / 'server.py').write_text(SERVER, encoding='utf-8')
        (bundle / 'worker.py').write_bytes(WORKER.encode('utf-8'))
        (bundle / 'launch.cmd').write_text('@echo off\n"' + str(runtime) + '" -I -B -u "%~dp0server.py"\n', encoding='utf-8')
        files = [file for file in bundle.rglob('*') if file.is_file() and file.name != 'bundle.manifest.json']
        save(bundle / 'bundle.manifest.json', {'app_id': family,
            'files': [{'path': file.relative_to(bundle).as_posix(), 'sha256': sha(file)} for file in files]})
        shutil.copyfile(source / f'config/apps/{family}.json', config / f'{family}.json')
        save(config / f'{family}.install.json', {'app_id': family, 'installed': True,
            'manifest_sha256': sha(config / f'{family}.json'), 'launch': {
                'bundle_root': str(bundle), 'executable': str(bundle / 'launch.cmd'), 'working_directory': str(bundle),
                'allowed_arguments': [], 'executable_sha256': sha(bundle / 'launch.cmd'),
                'bundle_manifest_sha256': sha(bundle / 'bundle.manifest.json')}})
        receipts.append(binding)
    token = data / 'ocr-token.txt'
    if not token.exists():
        token.write_text(secrets.token_urlsafe(36), encoding='utf-8')
    os.environ['WORKBENCH_OCR_TOKEN_FILE'] = str(token)
    save(data / 'agent-native/receipt.json', {'shared_assets_read_only': True, 'copies_models': False,
        'private_data': str(data), 'bindings': receipts})
    registry = AppRegistry(config, install_root=bundle_root)
    if registry.failures:
        raise ValueError('Private native registration failed')
    routes._registry = registry
    return registry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8866)
    parser.add_argument('--stop-file', type=Path)
    arguments = parser.parse_args()
    arguments.source, arguments.program, arguments.data = [
        require_f_path(value) for value in (arguments.source, arguments.program, arguments.data)]
    _native_inputs(arguments.source, arguments.program)
    from scripts.candidate_bootstrap import _FileLease
    arguments.data.mkdir(parents=True, exist_ok=True)
    lease = _FileLease(require_f_path(arguments.data / '.live-preview.lock'))
    if not lease.acquire():
        raise ValueError('Preview data directory already in use')
    try:
        configure(arguments.source, arguments.program, arguments.data)
        os.environ['WORKBENCH_PORTAL_DIST'] = str(arguments.source / 'apps/portal/dist')
        import uvicorn
        from workbench_platform.app import app
        if arguments.stop_file:
            stop_file = require_f_path(arguments.stop_file)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=arguments.port, access_log=False,
                                                   timeout_graceful_shutdown=20))
            def watch_stop():
                while not stop_file.exists() and not server.should_exit:
                    time.sleep(.25)
                server.should_exit = True
            threading.Thread(target=watch_stop, daemon=True).start()
            server.run()
        else:
            uvicorn.run(app, host='127.0.0.1', port=arguments.port, access_log=False)
    finally:
        lease.close()


if __name__ == '__main__':
    main()
