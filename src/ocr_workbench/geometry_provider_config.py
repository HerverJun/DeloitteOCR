"""Explicit offline provider selection and content identities; no model imports."""
import hashlib
import json
from pathlib import Path

from ocr_workbench.geometry_contract import fingerprint

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ('paddle', 'tableformer-raw', 'rapidtable')


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def provider_config(provider):
    if provider not in PROVIDERS:
        raise ValueError('未知表格几何提供方')
    if provider == 'paddle':
        return {'provider': provider, 'runtime': 'ppocr',
                'model_lock_sha256': file_sha(ROOT / 'config/table-model-lock.json')}
    path = ROOT / 'config/geometry-providers.json'
    value = json.loads(path.read_text('utf-8'))['providers'][provider]
    return {**value, 'provider': provider}


def verify_provider(provider):
    config = provider_config(provider)
    if provider == 'paddle':
        return config
    for relative, expected in config['files'].items():
        path = ROOT / relative
        if not path.is_file():
            raise ValueError('未安装实验性几何模型或依赖：' + relative)
        if file_sha(path) != expected:
            raise ValueError('实验性几何来源校验失败：' + relative)
    return config


def provider_identity(provider):
    config = verify_provider(provider)
    names = ('geometry_provider_config.py', 'geometry_candidate_worker.py', 'table_geometry_worker.py',
             'geometry_providers.py', 'geometry_contract.py', 'coordinates.py', 'engine_host.py', 'adapter.py')
    return {'provider': provider, 'config': config,
            'provider_code_sha256': fingerprint({name: file_sha(Path(__file__).with_name(name)) for name in names})}
