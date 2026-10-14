"""Private native launchers over F-drive assets; never rewrite shared bindings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

from workbench_platform.catalog.registry import AppRegistry
from workbench_platform.catalog import routes
from workbench_platform.supervisor.processes import require_f_path, storage_environment


SERVER = '''import json,os,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parent
binding=json.loads((root/'binding.json').read_text(encoding='utf-8'))
data_root=Path(binding['data_root'])
data=data_root/'native'/binding['app_id']
data.mkdir(parents=True,exist_ok=True)
source=Path(binding['source'])
sys.path[:0]=[str(source/'src')]
asset=Path(binding['asset'])
if binding['app_id']=='ocr':
    from ocr_workbench.service import main
    sys.argv=['ocr-workbench','--bundle',str(asset),'--web-root',binding['web_root'],'--data',str(data),'--token-file',str(data_root/'ocr-token.txt')]
    main()
else:
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
    from image_platform.cli import main
    with (data/'worker.log').open('ab') as log:
        worker=subprocess.Popen([sys.executable,'-I','-B','-u',str(root/'worker.py'),str(source/'src')],
            cwd=root,env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            sys.argv=['image-platform','serve']
            main()
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=15)
'''
WORKER = '''import sys
sys.path.insert(0,sys.argv[1])
from image_platform.cli import main
sys.argv=['image-platform','worker']
main()
'''


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    require_f_path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _native_inputs(source, program):
    """Validate both native pages and runtimes before modifying a workspace."""
    inputs = []
    for family in ('ocr', 'classification'):
        asset = require_f_path(program / 'bundles' / family)
        native_source = require_f_path(source.parent / family)
        web_root = require_f_path(native_source / ('frontend/dist' if family == 'ocr' else 'apps/web/dist'))
        if not (web_root / 'index.html').is_file():
            raise ValueError('Native production frontend missing: ' + family)
        runtime = require_f_path(asset / 'runtimes/control/python.exe')
        if not runtime.is_file() or not (asset / 'bundle.manifest.json').is_file():
            raise ValueError('Existing verified native assets missing')
        inputs.append((family, asset, native_source, web_root, runtime))
    return inputs


def configure(source, program, data):
    source, program, data = [require_f_path(value) for value in (source, program, data)]
    native_inputs = _native_inputs(source, program)
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
                   'frontend_index_sha256': sha(web_root / 'index.html'),
                   'runtime_sha256': sha(runtime), 'asset_declaration_sha256': sha(asset / 'bundle.manifest.json')}
        save(bundle / 'binding.json', binding)
        (bundle / 'server.py').write_text(SERVER, encoding='utf-8')
        (bundle / 'worker.py').write_text(WORKER, encoding='utf-8')
        (bundle / 'launch.cmd').write_text('@echo off\n"' + str(runtime) + '" -I -B -u "%~dp0server.py"\n', encoding='utf-8')
        files = [file for file in bundle.iterdir() if file.is_file() and file.name != 'bundle.manifest.json']
        save(bundle / 'bundle.manifest.json', {'app_id': family,
            'files': [{'path': file.name, 'sha256': sha(file)} for file in files]})
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
        uvicorn.run(app, host='127.0.0.1', port=arguments.port, access_log=False)
    finally:
        lease.close()


if __name__ == '__main__':
    main()
