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


def test_managed_overrides_dependency_cache_aliases_and_preserves_user_home(installation, monkeypatch):
    path, _value = installation
    config = load_install(path)
    names = ('PADDLE_HOME', 'PADDLE_EXTENSION_DIR', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE',
        'HF_ASSETS_CACHE', 'HUGGINGFACE_ASSETS_CACHE', 'HF_XET_CACHE', 'HF_MODULES_CACHE',
        'TRANSFORMERS_CACHE', 'PYTORCH_TRANSFORMERS_CACHE', 'PYTORCH_PRETRAINED_BERT_CACHE',
        'TORCH_EXTENSIONS_DIR')
    for name in names:
        monkeypatch.setenv(name, 'C:/unmanaged-cache')
    before = {name: os.environ.get(name) for name in ('HOME', 'USERPROFILE', 'CODEX_HOME')}
    env = config.environment()
    for name in names:
        assert Path(env[name]).is_relative_to(config.roots['cache'])
        assert Path(env[name]).is_dir()
    assert env['HF_HUB_CACHE'] == env['HUGGINGFACE_HUB_CACHE'] == env['TRANSFORMERS_CACHE']
    assert not set(before).intersection(env)
    assert before == {name: os.environ.get(name) for name in before}


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


@pytest.mark.parametrize('field,bad',[('paths',None),('resources',[]),('network',None),('mode',[]),('installation_id',123)])
def test_malformed_binding_has_an_actionable_configuration_error(installation, field, bad):
    path,value = installation
    value[field] = bad
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError,match='Installation|installation'):
        load_install(path)


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


def test_seal_alias_requires_an_exact_registered_root_and_preserves_old_config(installation):
    path, value = installation
    original = load_install(path)
    seal = original.program / 'assets/program/models/multimodal/seal'
    adjacent = seal.parent / 'audio'
    child = seal / 'sub-model'
    for directory in (original.resources['ocr_models'], seal, adjacent, child):
        directory.mkdir(parents=True, exist_ok=True)
    alias = original.roots['paddle_temp'] / 'offlineocr-paddle-models/models'
    # Older configs remain readable, but an absent resource ID grants no alias.
    with pytest.raises(ValueError, match='not registered'):
        require_model_mapping(alias, seal)
    value['resources']['seal_models'] = 'assets/program/models/multimodal/seal'
    path.write_text(json.dumps(value), encoding='utf-8')
    config = load_install(path)
    for target in (config.resources['ocr_models'], seal):
        assert require_model_mapping(alias, target) == (alias, target)
    for target in (adjacent, child, seal.parent):
        with pytest.raises(ValueError, match='not registered'):
            require_model_mapping(alias, target)


def test_explicit_model_mapping_cannot_authorize_other_assets(installation):
    path, value = installation
    config = load_install(path)
    seal = config.program / 'assets/program/models/multimodal/seal'
    value['resources']['seal_models'] = 'assets/program/models/multimodal/seal'
    alias = config.roots['paddle_temp'] / 'explicit-seal'
    for target in (seal.parent / 'audio', seal / 'sub-model', seal.parent):
        target.mkdir(parents=True, exist_ok=True)
        value['model_mappings'] = [{'link': str(alias), 'target': str(target)}]
        path.write_text(json.dumps(value), encoding='utf-8')
        with pytest.raises(ValueError, match='exact registered model root'):
            load_install(path)
    # Explicit aliases can retain their own names, while targets stay exact.
    value['model_mappings'] = [{'link': str(alias), 'target': str(seal)}]
    path.write_text(json.dumps(value), encoding='utf-8')
    assert load_install(path).mappings == ((alias, seal),)


@pytest.mark.skipif(os.name != 'nt', reason='Windows model resource junction boundary')
@pytest.mark.parametrize('member', ['assets/linked-seal', 'assets/linked-seal/seal'])
def test_registered_seal_resource_cannot_follow_a_junction(installation, member):
    path, value = installation
    config = load_install(path)
    actual = config.program / 'assets/real-models'
    (actual / 'seal').mkdir(parents=True)
    link = config.program / 'assets/linked-seal'
    subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(actual)], check=True, capture_output=True)
    value['resources']['seal_models'] = member
    path.write_text(json.dumps(value), encoding='utf-8')
    try:
        with pytest.raises(ValueError, match='unregistered reparse'):
            load_install(path)
    finally:
        link.rmdir()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Paddle alias parent junction boundary')
def test_dynamic_and_explicit_alias_reject_a_parent_junction_escape(installation):
    path, value = installation
    config = load_install(path)
    seal = config.program / 'assets/program/models/multimodal/seal'
    seal.mkdir(parents=True)
    value['resources']['seal_models'] = 'assets/program/models/multimodal/seal'
    outside = path.parent / 'outside-paddle'
    outside.mkdir()
    parent = config.roots['paddle_temp'] / 'offlineocr-paddle-escape'
    parent.parent.mkdir()
    subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(parent), str(outside)], check=True, capture_output=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    try:
        with pytest.raises(ValueError, match='not registered'):
            require_model_mapping(parent / 'models', seal)
        value['model_mappings'] = [{'link': str(parent / 'models'), 'target': str(seal)}]
        path.write_text(json.dumps(value), encoding='utf-8')
        with pytest.raises(ValueError, match='Paddle temp link'):
            load_install(path)
        assert list(outside.iterdir()) == []
    finally:
        parent.rmdir()
