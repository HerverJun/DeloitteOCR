import json
import asyncio
import os
from pathlib import Path
import sqlite3
import sys
import subprocess
from types import SimpleNamespace
from dataclasses import replace

import pytest
import httpx

from packages.runtime import cli
from packages.runtime.config import load_install


@pytest.fixture(autouse=True)
def isolated_process_environment():
    before = dict(os.environ)
    paths = sys.path[:]
    try:
        yield
    finally:
        os.environ.clear(); os.environ.update(before)
        sys.path[:] = paths


@pytest.fixture
def package(tmp_path):
    root = tmp_path / 'Clear Program'
    source = root / 'app/platform'
    source.mkdir(parents=True)
    (root / 'assets').mkdir()
    (root / 'runtimes').mkdir()
    (root / 'tools').mkdir()
    binary = root / 'runtimes/python.exe'
    binary.write_bytes(b'PINNED TEST RUNTIME')
    manifest = root / 'manifests'
    manifest.mkdir()
    cli._write(manifest / 'resources.json', {'format': 'clear-portable-resources-v1',
        'resources': {'platform_source': 'app/platform', 'platform_python': 'runtimes/python.exe'}})
    files = {path.relative_to(root).as_posix(): {'bytes': path.stat().st_size, 'sha256': cli._hash(path)}
             for path in (binary, manifest / 'resources.json')}
    cli._write(manifest / 'package.json', {'format': 'clear-portable-package-v1',
        'standalone': True, 'status': 'unqualified', 'files': files})
    return root


@pytest.fixture
def installation(package, tmp_path):
    if os.name != 'nt':
        pytest.skip('Installation credential persistence uses Windows DPAPI')
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(tmp_path / '资料 数据')])
    args.config = None
    result = cli.initialize(args, package)
    return load_install(result['config'])


def test_init_supports_chinese_data_without_writing_program(installation):
    config = installation
    assert config.data.name == '资料 数据'
    assert str(config.roots['paddle_temp']).isascii()
    assert config.roots['paddle_temp'].is_relative_to(config.data.parent)
    assert not config.path.is_relative_to(config.program)
    assert cli.verify(config)['ok']
    assert set(path.relative_to(config.program).as_posix() for path in config.program.rglob('*') if path.is_file()) == {
        'runtimes/python.exe', 'manifests/package.json', 'manifests/resources.json'}
    assert os.environ['HF_HUB_OFFLINE'] == '1'
    for name in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'HF_HOME', 'TORCH_HOME'):
        assert config.check(os.environ[name]).drive.upper() == 'F:'


def test_multi_portal_serves_registered_program_as_read_only(installation, monkeypatch):
    from fastapi.testclient import TestClient
    from workbench_platform.tasks.server import create_app
    root = installation.program / 'app/platform/apps/portal/dist'
    root.mkdir(parents=True)
    (root / 'index.html').write_text('<html>portable portal</html>', encoding='utf-8')
    (root / 'main.js').write_text('window.PORTABLE=true', encoding='utf-8')
    with pytest.raises(ValueError, match='write roots'):
        installation.check(root, role='write')
    monkeypatch.setenv('WORKBENCH_PORTAL_DIST', str(root))
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN', 'http://192.168.1.8:9021')
    monkeypatch.setenv('CLEAR_ALLOW_LAN_HTTP', '1')
    monkeypatch.setenv('CLEAR_WORKER_TOKEN', 'p' * 40)
    # These public static requests use no database or lifespan services.
    client = TestClient(create_app(None), base_url='http://192.168.1.8:9021')
    assert client.get('/jobs').text == '<html>portable portal</html>'
    assert client.get('/main.js').text == 'window.PORTABLE=true'
    assert client.get('/%2E%2E/clear.install.json').status_code == 404


def test_worker_restart_reads_registered_native_program_source(installation, monkeypatch):
    from workbench_platform.tasks.worker import ExecutionHost
    from workbench_platform.tasks import source_identity
    source = installation.program / 'app/platform'
    binding = installation.data / 'agent-native/bundles/ocr/binding.json'
    binding.parent.mkdir(parents=True)
    binding.write_text(json.dumps({'source': str(source)}), encoding='utf-8')
    with pytest.raises(ValueError, match='write roots'):
        installation.check(source, role='write')

    class SourceReadComplete(Exception):
        pass

    def fingerprint(path):
        if path == source:
            raise SourceReadComplete
        return 'platform-version'

    monkeypatch.setattr(source_identity, 'fingerprint', fingerprint)
    with pytest.raises(SourceReadComplete):
        ExecutionHost(None, installation.data, None)


def test_service_credentials_are_account_bound_encrypted(installation):
    private = cli._read_secrets(installation)
    assert len(private['worker_token']) >= 32
    content = cli._secret_file(installation).read_bytes()
    assert private['worker_token'].encode() not in content
    value = json.loads(content)
    value['installation_id'] = 'foreign-installation'
    cli._write(cli._secret_file(installation), value)
    with pytest.raises(ValueError, match='Windows'):
        cli._read_secrets(installation)


def test_init_refuses_existing_business_data(package, tmp_path):
    data = tmp_path / 'existing'
    data.mkdir(); (data / 'original.txt').write_text('原始资料', 'utf-8')
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(data)])
    args.config = None
    with pytest.raises(ValueError, match='empty DATA'):
        cli.initialize(args, package)
    assert (data / 'original.txt').read_text('utf-8') == '原始资料'


def test_verify_detects_tampering_and_manifest_escape(installation):
    runtime = installation.resource('platform_python')
    runtime.write_bytes(b'changed')
    result = cli.verify(installation)
    assert not result['ok'] and result['errors'][0]['member'] == 'runtimes/python.exe'
    manifest_path = installation.program / 'manifests/package.json'
    manifest = cli._json(manifest_path)
    manifest['files']['../outside.txt'] = {'bytes': 0, 'sha256': '0' * 64}
    cli._write(manifest_path, manifest)
    assert any(item.get('member') == '../outside.txt' for item in cli.verify(installation)['errors'])


def test_network_exposes_single_only_on_loopback():
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', 'F:/unused',
                                         '--transport', 'lan-http', '--listen', '192.168.1.8'])
    with pytest.raises(ValueError, match='multi'):
        cli._network(args)


def test_lifecycle_refuses_a_second_owner(installation):
    with cli._lifecycle(installation):
        with pytest.raises(RuntimeError, match='Another lifecycle'):
            with cli._lifecycle(installation):
                raise AssertionError('Second writer acquired the lifecycle lock')


def test_status_and_stop_reject_foreign_installation(installation, monkeypatch):
    record = dict(installation_id='foreign', data=str(installation.data), role='single',
                  pid=123, creation_time='456', launch_id='nonce')
    cli._write(cli._state_path(installation, 'single'), record)
    monkeypatch.setattr(cli, '_alive', lambda value: True)
    with pytest.raises(RuntimeError, match='ownership'):
        cli.status(installation)
    with pytest.raises(RuntimeError, match='ownership'):
        cli._stop_role(installation, 'single', 1)
    assert not list((installation.data / '.clear-services').glob('*.stop'))


def test_reused_pid_is_not_a_current_owned_process(installation, monkeypatch):
    record = dict(installation_id=installation.installation_id, data=str(installation.data), role='single',
                  pid=123, creation_time='OLD', launch_id='oldnonce')
    cli._write(cli._state_path(installation, 'single'), record)
    monkeypatch.setattr(cli, '_alive', lambda value: False)
    assert cli._owned(installation, 'single') is None
    assert cli._stop_role(installation, 'single', 1) == {'running': False}


def test_partial_qualification_does_not_authorize_full_start(installation, monkeypatch):
    verification = cli.verify(installation)
    cli._write(installation.roots['cache'] / 'qualification.json', {
        'status': 'passed', 'scope': 'ppocr', 'installation_id': installation.installation_id,
        'package_sha256': verification['package_sha256'], 'machine': {'test': True}})
    monkeypatch.setattr(cli, '_machine', lambda: {'test': True})
    with pytest.raises(ValueError, match='qualification'):
        cli.start(installation, SimpleNamespace(allow_unqualified=False))


def test_single_backup_restore_preserves_original_and_rebinds_host_state(installation, tmp_path):
    source = installation.data
    (source / 'asset-blobs').mkdir()
    (source / 'asset-blobs/original.bin').write_bytes(b'EXACT ORIGINAL\x00\xff')
    with sqlite3.connect(source / 'test.sqlite3') as db:
        db.execute('CREATE TABLE sample(id TEXT PRIMARY KEY, amount TEXT)')
        db.execute('INSERT INTO sample VALUES(?,?)', ('00001234', '99.90'))
    # These host-specific records must not be copied into a backup.
    cli._write(source / '.clear-services/single.json', {'installation_id': installation.installation_id,
        'data': str(source), 'role': 'single', 'pid': 0, 'creation_time': '0', 'launch_id': 'stale'})
    # Remove stale owned state to exercise an intentionally stopped installation.
    cli._state_path(installation, 'single').unlink()
    result = cli.backup(installation)
    destination = tmp_path / '恢复 新目录'
    restored = cli.restore(installation, SimpleNamespace(backup=Path(result['backup']), destination=destination))
    new_config = load_install(restored['config'])
    assert new_config.installation_id != installation.installation_id
    assert (destination / 'asset-blobs/original.bin').read_bytes() == b'EXACT ORIGINAL\x00\xff'
    assert (source / 'asset-blobs/original.bin').read_bytes() == b'EXACT ORIGINAL\x00\xff'
    with sqlite3.connect(destination / 'test.sqlite3') as db:
        assert db.execute('SELECT id,amount FROM sample').fetchone() == ('00001234', '99.90')
    assert not (destination / '.clear-services').exists()
    assert cli._read_secrets(new_config)['worker_token'] != cli._read_secrets(installation)['worker_token']
    assert restored['qualification'] == 'pending'


def test_global_config_option_can_follow_command():
    parser = cli.build_parser()
    assert parser.parse_args(['status', '--config', 'F:/state/install.json']).config == Path('F:/state/install.json')
    assert parser.parse_args(['--config', 'F:/state/install.json', 'status']).config == Path('F:/state/install.json')


def test_drain_counts_only_current_jobs_and_blocks_new_writes(installation):
    from starlette.responses import JSONResponse
    with sqlite3.connect(installation.data / 'jobs.sqlite3') as db:
        db.execute('CREATE TABLE manual_analysis(job_id TEXT, body TEXT)')
        db.execute('INSERT INTO manual_analysis VALUES(?,?)', ('live', json.dumps({'status': 'running'})))
        db.execute('INSERT INTO manual_analysis VALUES(?,?)', ('done', json.dumps({'status': 'succeeded', 'request': {'status': 'running'}})))
    active = cli._single_active(installation)
    assert active == [{'ledger': 'jobs.sqlite3', 'table': 'manual_analysis', 'count': 1}]
    async def app(scope, receive, send):
        await JSONResponse({'ok': True})(scope, receive, send)
    async def exercise():
        gate = cli.DrainGate(app, installation.data)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url='http://127.0.0.1:8889') as client:
            assert (await client.post('/submit')).status_code == 200
            cli._write(installation.data / '.clear-drain.json', {'phase': 'draining'})
            assert (await client.post('/submit')).status_code == 503
            assert (await client.get('/read-result')).status_code == 200
            (installation.data / '.clear-drain.json').unlink()
            assert (await client.post('/submit')).status_code == 200
    asyncio.run(exercise())


def test_same_package_move_rebinds_without_copying_business_data(installation, tmp_path):
    cli.verify(installation)
    previous_token = cli._read_secrets(installation)['worker_token']
    (installation.data / 'original.bin').write_bytes(b'original-data')
    moved = tmp_path / '搬迁 程序'
    # Both resolved endpoints are explicitly inside this F-drive test directory.
    assert installation.program.is_relative_to(tmp_path) and moved.is_relative_to(tmp_path)
    installation.program.rename(moved)
    result = cli.rebind(installation, moved)
    updated = load_install(result['config'])
    assert updated.program == moved.resolve()
    assert updated.data == installation.data
    assert updated.installation_id != installation.installation_id
    assert updated.resource('platform_source') == moved / 'app/platform'
    assert (updated.data / 'original.bin').read_bytes() == b'original-data'
    assert cli._read_secrets(updated)['worker_token'] != previous_token
    assert not (updated.roots['cache'] / 'qualification.json').exists()
    assert result['qualification'] == 'pending'


def test_incomplete_init_cannot_start_or_fall_back_to_database_owner(installation):
    cli._write(installation.data / 'installation-state.json', {
        'installation_id': installation.installation_id, 'phase': 'configured'})
    with pytest.raises(ValueError, match='initialization is incomplete'):
        cli.start(installation, SimpleNamespace(allow_unqualified=True))
    with pytest.raises(ValueError, match='Limited database'):
        cli._database_url(installation, {'pg_username': 'owner', 'pg_password': 'secret', 'database': 'ledger'})


def test_single_serve_holds_existing_data_lease_and_releases_on_failure(installation, monkeypatch):
    from scripts.candidate_bootstrap import _FileLease
    async def inside(config, args):
        competitor = _FileLease(config.data / '.live-preview.lock')
        assert not competitor.acquire()
        raise ValueError('simulated startup failure')
    monkeypatch.setattr(cli, '_serve_app', inside)
    with pytest.raises(ValueError, match='simulated startup'):
        asyncio.run(cli.serve(installation, SimpleNamespace(role='single')))
    competitor = _FileLease(installation.data / '.live-preview.lock')
    assert competitor.acquire()
    competitor.close()


def test_resource_id_deletion_or_repoint_cannot_bypass_verification(installation):
    missing = replace(installation, resources={'platform_source': installation.resource('platform_source')})
    result = cli.verify(missing)
    assert not result['ok']
    assert any(error.get('resource') == 'platform_python' for error in result['errors'])
    repointed = replace(installation, resources={**installation.resources,
        'platform_python': installation.resource('platform_source')})
    result = cli.verify(repointed)
    assert not result['ok']
    assert any(error.get('resource') == 'platform_python' and 'pinned' in error['error'] for error in result['errors'])


def test_diagnostics_can_report_a_missing_runtime_without_falling_back(installation):
    installation.resource('platform_python').unlink()
    cli._activate(installation, strict_resources=False)
    assert os.environ['CLEAR_PLATFORM_PYTHON'] == str(installation.resources['platform_python'])
    report = cli.doctor(installation)
    assert not report['ok']
    assert report['resources']['platform_python']['available'] is False


def test_start_does_not_reuse_a_service_with_changed_source_binding(installation, monkeypatch):
    monkeypatch.setattr(cli, '_owned', lambda config, role: {
        'pid': 123, 'package_sha256': 'old-source', 'config_sha256': cli._hash(config.path)})
    with pytest.raises(ValueError, match='源码或安装配置'):
        cli._start_role(installation, 'single')


@pytest.mark.parametrize('registered', [False, True])
def test_single_backup_carries_external_original_for_immutable_dataset_items(installation, tmp_path, registered):
    document = cli._json(installation.path)
    external = tmp_path / 'registered external storage'
    external.mkdir()
    if registered:
        document['paths']['backup'] = str(external)
    cli._write(installation.path, document)
    installation = load_install(installation.path)
    cli._activate(installation)
    original = external / 'original.bin'
    if not registered:
        assert not any(original.is_relative_to(root) for root in installation.roots.values())
    original.write_bytes(b'EXTERNAL ORIGINAL 00001234 99.90')
    native = installation.data / 'native/classification'
    native.mkdir(parents=True)
    items = native / 'dataset.items'
    immutable = json.dumps({'path': str(original), 'number': '00001234', 'amount': '99.90'}).encode()
    items.write_bytes(immutable)
    with sqlite3.connect(native / 'platform.sqlite3') as db:
        db.executescript('PRAGMA user_version=5; CREATE TABLE assets(id TEXT,path TEXT);'
            'CREATE TABLE datasets(id TEXT,manifest_path TEXT); CREATE TABLE models(id TEXT,package_path TEXT);'
            'CREATE TABLE jobs(id TEXT,state TEXT,lease_token TEXT,owner TEXT,lease_until REAL);')
        db.execute('INSERT INTO assets VALUES(?,?)', ('asset', str(original)))
        db.execute('INSERT INTO datasets VALUES(?,?)', ('dataset', str(items)))
    saved = cli.backup(installation)
    manifest = cli._json(Path(saved['backup']) / 'manifest.json')
    assert str(original) in manifest['external_path_map']
    original.unlink()
    result = cli.restore(installation, SimpleNamespace(backup=Path(saved['backup']), destination=tmp_path / 'new DATA'))
    restored = load_install(result['config'])
    assert (restored.data / 'native/classification/dataset.items').read_bytes() == immutable
    mapped = restored.check(original, 'Preserved dataset source', role='read')
    assert mapped.is_relative_to(restored.data)
    assert mapped.read_bytes() == b'EXTERNAL ORIGINAL 00001234 99.90'
    if registered:
        assert restored.check(original, role='write') == original
    else:
        with pytest.raises(ValueError, match='outside registered'):
            restored.check(original, role='write')


def test_requested_junction_is_rejected_before_init_restore_or_rebind(installation, tmp_path):
    if os.name != 'nt': pytest.skip('Windows junction regression')
    real = tmp_path / 'physical directory'
    real.mkdir()
    link = tmp_path / 'requested-junction'
    assert real.is_relative_to(tmp_path) and link.is_relative_to(tmp_path)
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)],
                   check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(link / 'DATA')])
        args.config = None
        with pytest.raises(ValueError, match='reparse'):
            cli.initialize(args, installation.program)
        with pytest.raises(ValueError, match='reparse'):
            cli.rebind(installation, link)
        saved = cli.backup(installation)
        with pytest.raises(ValueError, match='reparse'):
            cli.restore(installation, SimpleNamespace(backup=Path(saved['backup']), destination=link / 'restored'))
        assert list(real.iterdir()) == []
    finally:
        # Remove only the explicitly created F-drive junction, never its target.
        os.rmdir(link)


@pytest.mark.parametrize('outside_option', ['--config', '--cache'])
def test_development_f_policy_rejects_other_drive_before_any_mkdir(package, tmp_path, monkeypatch, outside_option):
    actual_mkdir = Path.mkdir
    calls = []
    def guarded_mkdir(self, *args, **kwargs):
        assert self.drive.upper() == 'F:', 'A forbidden write was attempted before path validation'
        calls.append(self)
        return actual_mkdir(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', guarded_mkdir)
    data = tmp_path / 'untouched DATA'
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(data),
        '--development-f-only', outside_option, 'C:/Clear-must-not-be-created/path'])
    args.config = getattr(args, 'config', None)
    with pytest.raises(ValueError, match='F:'):
        cli.initialize(args, package)
    assert calls == []
    assert not data.exists()


def test_cli_entry_clears_inherited_old_guard_before_help():
    entry = Path(__file__).resolve().parents[1] / 'scripts/clear_portable.py'
    environment = dict(os.environ, CLEAR_IO_GUARD='1', CLEAR_INSTALL_CONFIG='F:/missing-old-install/clear.install.json')
    result = subprocess.run([sys.executable, '-B', str(entry), '--help'], env=environment,
                            capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    assert 'init' in result.stdout and 'restore' in result.stdout


def test_backup_excludes_machine_readiness_and_refuses_unresolved_gpu(installation):
    ledger = installation.data / 'gpu.sqlite3'
    with sqlite3.connect(ledger) as db:
        db.executescript('CREATE TABLE gpu_state(id INTEGER,request_id TEXT,owner_json TEXT,token_sha256 TEXT);'
            'INSERT INTO gpu_state VALUES(1,NULL,NULL,NULL);'
            'CREATE TABLE gpu_requests(status TEXT);')
    for name in ('gpu-qualification.json', 'aigc-gpu-qualification.json', 'multimodal-cpu-validation.json'):
        cli._write(installation.data / name, {'ready': True, 'old_machine': True})
    cli._write(installation.data / 'gpu-owners/old-owner.json', {'pid': 123, 'job': 'old-host'})
    cli._write(installation.data / 'native-launches/old-launch.json', {'pid': 456})
    saved = cli.backup(installation)
    manifest = cli._json(Path(saved['backup']) / 'manifest.json')
    assert not any(cli._machine_member(record['path']) for record in manifest['files'])
    with sqlite3.connect(ledger) as db:
        db.execute("INSERT INTO gpu_requests VALUES('pending')")
    with pytest.raises(RuntimeError, match='unresolved'):
        cli.backup(installation)
    with sqlite3.connect(ledger) as db:
        assert db.execute('SELECT status FROM gpu_requests').fetchone()[0] == 'pending'
    from scripts.candidate_bootstrap import _FileLease
    lease = _FileLease(installation.data / '.live-preview.lock')
    assert lease.acquire()
    lease.close()


def test_restore_archives_legacy_gpu_state_instead_of_activating_it(installation, tmp_path):
    saved = cli.backup(installation)
    backup_root = Path(saved['backup'])
    legacy = backup_root / 'files/gpu.sqlite3'
    legacy.parent.mkdir(exist_ok=True)
    with sqlite3.connect(legacy) as db:
        db.execute('CREATE TABLE gpu_state(id INTEGER,owner_json TEXT)')
        db.execute('INSERT INTO gpu_state VALUES(1,?)', ('{"pid":123}',))
    qualification = backup_root / 'files/gpu-qualification.json'
    cli._write(qualification, {'ready': True, 'pid': 123})
    manifest = cli._json(backup_root / 'manifest.json')
    manifest['files'].extend({'path': path.name, 'sha256': cli._hash(path), 'size_bytes': path.stat().st_size}
                             for path in (legacy, qualification))
    cli._write(backup_root / 'manifest.json', manifest)
    result = cli.restore(installation, SimpleNamespace(backup=backup_root, destination=tmp_path / 'restored DATA'))
    restored = load_install(result['config'])
    assert not (restored.data / 'gpu.sqlite3').exists()
    assert not (restored.data / 'gpu-qualification.json').exists()
    assert (restored.data / 'migration/retired-machine-state/gpu.sqlite3').is_file()
    assert (restored.data / 'migration/retired-machine-state/gpu-qualification.json').is_file()
    assert result['qualification'] == 'pending'


def test_init_missing_resources_can_be_repaired_without_a_wedged_configuration(package, tmp_path):
    runtime = package / 'runtimes/python.exe'
    content = runtime.read_bytes()
    runtime.unlink()
    data = tmp_path / 'fresh DATA'
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(data)])
    args.config = None
    with pytest.raises(ValueError, match='resources are missing'):
        cli.initialize(args, package)
    assert not data.exists()
    runtime.write_bytes(content)
    result = cli.initialize(args, package)
    assert result['initialized']


def test_resume_can_seed_only_a_never_initialized_missing_vault(installation):
    previous = cli._read_secrets(installation)['worker_token']
    cli._secret_file(installation).unlink()
    cli._write(installation.data / 'installation-state.json', {
        'phase': 'configured', 'installation_id': installation.installation_id})
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(installation.data), '--resume'])
    args.config = installation.path
    assert cli.initialize(args, installation.program)['initialized']
    assert cli._read_secrets(installation)['worker_token'] != previous


def test_resume_does_not_reset_an_initialized_or_business_vault(installation):
    cli._secret_file(installation).unlink()
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(installation.data), '--resume'])
    args.config = installation.path
    with pytest.raises(ValueError, match='Windows'):
        cli.initialize(args, installation.program)
    assert not cli._secret_file(installation).exists()
    cli._write(installation.data / 'installation-state.json', {'phase': 'configured'})
    (installation.data / 'business.txt').write_text('用户原件', 'utf-8')
    with pytest.raises(ValueError, match='Windows'):
        cli.initialize(args, installation.program)
    assert (installation.data / 'business.txt').read_text('utf-8') == '用户原件'


def test_restored_blocked_history_can_drain_but_local_unknown_work_still_blocks(installation, tmp_path):
    with sqlite3.connect(installation.data / 'jobs.sqlite3') as db:
        db.execute('CREATE TABLE manual_analysis(job_id TEXT,body TEXT)')
        db.execute('INSERT INTO manual_analysis VALUES(?,?)', ('old-job', json.dumps({
            'status': 'queued', 'stage': 'queued', 'generation': 1, 'result_hash': 'a' * 64})))
    saved = cli.backup(installation)
    result = cli.restore(installation, SimpleNamespace(backup=Path(saved['backup']), destination=tmp_path / 'restored DATA'))
    restored = load_install(result['config'])
    assert cli._single_active(restored) == []
    with sqlite3.connect(restored.data / 'jobs.sqlite3') as db:
        body = json.loads(db.execute('SELECT body FROM manual_analysis').fetchone()[0])
        assert body['status'] == 'needs_verification'
        body.update(generation=2, error='本机新发出的请求尚未核实')
        db.execute('UPDATE manual_analysis SET body=?', (json.dumps(body),))
    assert cli._single_active(restored) == [{'ledger': 'jobs.sqlite3', 'table': 'manual_analysis', 'count': 1}]


@pytest.mark.parametrize('provided', [False, True])
def test_https_init_rejects_missing_or_unregistered_tls_before_publication(package, tmp_path, provided):
    data = tmp_path / 'HTTPS DATA'
    arguments = ['init', '--mode', 'multi', '--data', str(data), '--transport', 'https',
                 '--listen', '127.0.0.1', '--secrets', str(tmp_path / 'selected secrets')]
    if provided:
        certificate, key = tmp_path / 'outside-cert.pem', tmp_path / 'outside-key.pem'
        certificate.write_text('public fixture', 'utf-8'); key.write_text('private fixture', 'utf-8')
        arguments += ['--certificate', str(certificate), '--private-key', str(key)]
    args = cli.build_parser().parse_args(arguments)
    args.config = None
    with pytest.raises(ValueError, match='inside'):
        cli.initialize(args, package)
    assert not data.exists()


def test_tls_serve_binding_is_local_pinned_and_diagnoses_missing_pair(installation, monkeypatch):
    certificate = installation.roots['secrets'] / 'certificate.pem'
    key = installation.roots['secrets'] / 'private-key.pem'
    certificate.write_text('public fixture', 'utf-8'); key.write_text('private fixture', 'utf-8')
    network = cli._bind_tls({**installation.network, 'transport': 'https',
        'public_origin': 'https://localhost:8889', 'certificate': str(certificate),
        'private_key': str(key)}, installation.roots['secrets'])
    configured = replace(installation, mode='multi', network=network)
    assert cli._tls_options(configured) == {'ssl_certfile': str(certificate), 'ssl_keyfile': str(key)}
    key.write_text('changed private fixture', 'utf-8')
    with pytest.raises(ValueError, match='identity changed'):
        cli._tls_options(configured)
    monkeypatch.setattr(cli.shutil, 'which', lambda name: None)
    missing = replace(configured, network={**network, 'private_key': None})
    assert any('pair is missing' in error for error in cli.doctor(missing)['errors'])


def test_new_init_cannot_overwrite_another_installations_services_secret(package, tmp_path):
    selected = tmp_path / 'other installation secrets'
    selected.mkdir()
    vault = selected / 'services.dpapi.json'
    original = b'{"installation_id":"another-installation","dpapi":"retained"}'
    vault.write_bytes(original)
    data = tmp_path / 'new DATA'
    args = cli.build_parser().parse_args(['init', '--mode', 'single', '--data', str(data), '--secrets', str(selected)])
    args.config = None
    with pytest.raises(ValueError, match='already contains installation credentials'):
        cli.initialize(args, package)
    assert vault.read_bytes() == original
    assert not data.exists()
