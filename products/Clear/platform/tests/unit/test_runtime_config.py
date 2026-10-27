import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from packages.runtime import load_install, require_path, storage_environment, require_model_mapping


@pytest.fixture
def installation(tmp_path, monkeypatch):
    program = tmp_path / '只读程序 中文'
    (program / 'app/platform').mkdir(parents=True)
    data = tmp_path / '用户 DATA'
    config = tmp_path / 'clear.install.json'
    value = {'schema_version': 1, 'installation_id': str(uuid.uuid4()), 'mode': 'single',
        'paths': {'program': str(program), 'assets': str(program / 'assets'),
            'runtimes': str(program / 'runtimes'), 'tools': str(program / 'tools'),
            'data': str(data), 'cache': str(tmp_path / '缓存'), 'temp': str(tmp_path / 'temp'),
            'paddle_temp': str(tmp_path / 'ascii-paddle'), 'secrets': str(tmp_path / 'secrets'),
            'backup': str(tmp_path / 'backup')},
        'resources': {'platform_source': 'app/platform', 'ocr_models': 'assets/ocr/models'},
        'network': {'listen': '127.0.0.1', 'public_origin': 'http://127.0.0.1:18889',
            'transport': 'loopback-http', 'portal_port': 18889, 'worker_port': 18890, 'postgres_port': 15432},
        'policy': {'allowed_drives': ['F:']}}
    config.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setenv('CLEAR_INSTALL_CONFIG', str(config))
    return config, value


def test_managed_separates_immutable_resources_and_writable_data(installation):
    path, value = installation
    config = load_install(path)
    assert require_path(config.program / 'app', role='read').is_relative_to(config.program)
    with pytest.raises(ValueError, match='write roots'):
        require_path(config.program / 'app', role='write')
    assert require_path(config.data / 'uploads') == config.data / 'uploads'
    with pytest.raises(ValueError, match='roots'):
        require_path(config.data / '../../escape', role='write')
    env = storage_environment(config.data)
    for name in ('TEMP', 'TMP', 'APPDATA', 'HF_HOME', 'DSH_HOME', 'NODE_COMPILE_CACHE'):
        assert Path(env[name]).is_relative_to(path.parent)
        assert Path(env[name]).is_dir()
    assert env['HF_HUB_OFFLINE'] == '1'
    assert 'HOME' not in env and 'CODEX_HOME' not in env


@pytest.mark.parametrize('transport,origin,listen', [
    ('lan-http', 'http://192.168.1.9:18889', '0.0.0.0'),
    ('lan-http', 'http://8.8.8.8:18889', '8.8.8.8'),
    ('lan-http', 'http://192.168.1.9:18889', '192.168.1.10'),
    ('https', 'http://192.168.1.9:18889', '192.168.1.9'),
    ('loopback-http', 'http://localhost:18889', '127.0.0.1'),
])
def test_invalid_network_is_rejected(installation, transport, origin, listen):
    path, value = installation
    value['network'].update(transport=transport, public_origin=origin, listen=listen)
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        load_install(path)


def test_explicit_lan_configuration(installation):
    path, value = installation
    value['mode'] = 'multi'
    value['network'].update(transport='lan-http', listen='192.168.1.9', public_origin='http://192.168.1.9:18889')
    path.write_text(json.dumps(value), encoding='utf-8')
    assert load_install(path).network['transport'] == 'lan-http'


def test_external_resource_and_overlapping_roots_rejected(installation):
    path, value = installation
    value['resources']['model'] = str(path.parent / 'unregistered/model')
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='roots'):
        load_install(path)
    del value['resources']['model']
    value['paths']['data'] = value['paths']['program'] + '/DATA'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='overlaps'):
        load_install(path)


def test_network_and_missing_config_cannot_fall_back(installation, monkeypatch):
    path, value = installation
    with pytest.raises(ValueError):
        require_path(Path('//server/share/DATA'))
    monkeypatch.setenv('CLEAR_INSTALL_CONFIG', str(path.parent / 'missing.json'))
    with pytest.raises(FileNotFoundError):
        require_path(path.parent / 'anywhere')


def test_frozen_data_read_mapping_never_redirects_models_or_writes(installation):
    path, value = installation
    old = path.parent/'retired-data'
    value['policy']['legacy_data_roots'] = [str(old)]
    path.write_text(json.dumps(value), encoding='utf-8')
    config = load_install(path)
    assert require_path(old/'results/report.json', role='read') == config.data/'results/report.json'
    with pytest.raises(ValueError):
        require_path(old/'results/report.json', role='write')
    with pytest.raises(ValueError):
        require_path(path.parent/'old-models/checkpoint.pth', role='read')


def test_managed_credentials_use_the_separate_account_secret_root(installation):
    from workbench_platform.agent.credentials import CredentialVault
    path, _ = installation
    config = load_install(path)
    first = CredentialVault(config.data/'agent/credentials')
    second = CredentialVault(config.data/'comparisons/credentials')
    assert first.root == config.roots['secrets']/'vaults/agent/credentials'
    assert second.root != first.root
    with pytest.raises(ValueError, match='Credentials'):
        CredentialVault(config.roots['backup']/'arbitrary-vault')


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction boundary')
def test_escape_junction_and_registered_paddle_alias(installation):
    path, _ = installation
    config = load_install(path)
    config.data.mkdir()
    outside = path.parent / 'outside'
    outside.mkdir()
    link = config.data / 'escape'
    subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(ValueError):
            require_path(link / 'file')
    finally:
        link.rmdir()
    models = config.resources['ocr_models']
    models.mkdir(parents=True)
    alias = config.roots['paddle_temp'] / 'offlineocr-paddle-test/models'
    alias.parent.mkdir(parents=True)
    require_model_mapping(alias, models)
    subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(alias), str(models)], check=True, capture_output=True)
    try:
        assert require_path(alias / 'model.json', role='read') == models / 'model.json'
        with pytest.raises(ValueError):
            require_path(alias / 'write.json')
    finally:
        alias.rmdir()
