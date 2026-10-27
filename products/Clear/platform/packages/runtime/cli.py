"""Local lifecycle of one portable Clear installation.

Only the configuration/path layer is imported before the selected installation
has supplied its runtime environment. Database and web dependencies stay lazy.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
import uuid

from .config import load_install, _local, _no_reparse


def _json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate manifest/configuration key: ' + key)
            value[key] = item
        return value
    return json.loads(Path(path).read_text('utf-8-sig'), object_pairs_hook=pairs)


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write(path, value):
    path = Path(path)
    _no_reparse(path, 'State file')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.new')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _physical(value, label):
    requested = _local(value, label)
    _no_reparse(requested, label)
    return requested.resolve()


_MACHINE_DIRS = {'gpu-owners', 'gpu-requests', 'gpu-launches', 'native-launches',
                 '.clear-services', 'installation', 'agent-native'}
_MACHINE_FILES = {'gpu.sqlite3', 'gpu-qualification.json', 'safire-gpu-qualification.json',
    'aigc-gpu-qualification.json', 'multimodal-cpu-validation.json', 'task-fingerprint.key',
    'ocr-token.txt', 'candidate-instance.json', 'startup-verification.json', '.clear-drain.json',
    'gpu.sqlite3-wal', 'gpu.sqlite3-shm', 'gpu.sqlite3-journal'}
_SINGLE_LEDGERS = (
    ('jobs.sqlite3', 'manual_analysis', 'body'),
    ('documents/documents.sqlite3', 'documents', 'body'),
    ('comparisons/comparisons.sqlite3', 'comparison_jobs', 'body'),
    ('forensics/forensics.sqlite3', 'inspections', 'body'),
    ('batches/batches.sqlite3', 'batches', 'body'),
    ('agent/operations.sqlite3', 'runs', 'status'),
    ('data-workbench/sessions.sqlite3', 'jobs', 'status'),
    ('native/ocr/workbench.sqlite3', 'tasks', 'status'),
    ('native/classification/platform.sqlite3', 'jobs', 'state'),
)


def _single_rows(config):
    for member, table, column in _SINGLE_LEDGERS:
        path = config.check(config.data / member, 'Active task ledger', role='read')
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                db.row_factory = sqlite3.Row
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    for row in db.execute(f'SELECT * FROM "{table}"'):
                        yield member, table, column, row
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as error:
            raise RuntimeError(f'Unable to confirm active tasks in {member}; service retained') from error


def _row_state(row, column):
    value = json.loads(row[column]) if column == 'body' else None
    return value.get('status', value.get('state')) if value is not None else row[column]


def _blocked_fingerprint(member, table, column, row):
    names = set(row.keys())
    fields = {name: row[name] for name in names if name in {
        column, 'id', 'job_id', 'document_id', 'inspection_id', 'batch_id',
        'generation', 'revision', 'updated', 'updated_at', 'error', 'owner', 'lease_token'}}
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return member + ':' + table + ':' + digest


def _restored_blocked_rows(config):
    return [_blocked_fingerprint(member, table, column, row)
            for member, table, column, row in _single_rows(config)
            if _row_state(row, column) == 'needs_verification']


def _machine_member(relative):
    parts = Path(relative).parts
    return bool(parts and (parts[0] in _MACHINE_DIRS
        or len(parts) == 1 and parts[0] in _MACHINE_FILES
        or parts[:2] == ('migration', 'retired-machine-state')
        or Path(parts[-1]).suffix in {'.pid', '.lock'}))


def _assert_gpu_idle(config):
    ledger = config.check(config.data / 'gpu.sqlite3', 'GPU ownership ledger', role='read')
    if not ledger.is_file():
        if any((config.data / name).is_dir() and any((config.data / name).glob('*.json'))
               for name in ('gpu-owners', 'gpu-requests')):
            raise RuntimeError('GPU ownership evidence exists without its ledger; resolve ownership before backup/rebind')
        return
    try:
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            owner = db.execute('SELECT request_id,owner_json,token_sha256 FROM gpu_state WHERE id=1').fetchone()
            queued = db.execute("SELECT count(*) FROM gpu_requests WHERE status IN ('pending','granted')").fetchone()[0]
        if owner is None or any(value is not None for value in owner) or queued:
            raise RuntimeError('GPU ownership or pending requests remain unresolved; no lease was cleared or transplanted')
    except sqlite3.Error as error:
        raise RuntimeError('Unable to verify the existing GPU ledger; original data and ownership retained') from error


def _archive_machine_state(config, *, preserve_current_locks=False):
    retained = config.data / 'migration/retired-machine-state'
    moved = []
    for name in sorted(_MACHINE_DIRS | _MACHINE_FILES):
        source = config.check(config.data / name, 'Legacy machine state')
        if not source.exists():
            continue
        target = config.check(retained / name, 'Retained machine evidence')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = target.with_name(target.name + '-' + secrets.token_hex(6))
        # Both resolved endpoints were checked against this new DATA, and links
        # are rejected before moving any legacy directory.
        _no_reparse(source, 'Legacy machine state')
        source.rename(target)
        moved.append(name)
    for source in [*config.data.rglob('*.pid'), *config.data.rglob('*.lock')]:
        relative = source.relative_to(config.data)
        if source.is_relative_to(retained):
            continue
        if preserve_current_locks and relative.as_posix() in {'.live-preview.lock', '.clear-lifecycle.lock'}:
            continue
        source = config.check(source, 'Legacy process state')
        target = config.check(retained / relative, 'Retained process evidence')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = target.with_name(target.name + '-' + secrets.token_hex(6))
        _no_reparse(source, 'Legacy process state')
        source.rename(target)
        moved.append(relative.as_posix())
    return moved


@contextmanager
def _data_idle_lease(config):
    from scripts.candidate_bootstrap import _FileLease
    lease = _FileLease(config.data / '.live-preview.lock')
    if not lease.acquire():
        raise ValueError('Another platform process owns DATA; stop it before changing the installation binding')
    try:
        yield
    finally:
        lease.close()


def _ascii_temp(data, identity):
    ancestor = Path(data)
    while not str(ancestor).isascii():
        if ancestor == ancestor.parent:
            raise ValueError('Specify an ASCII --paddle-temp directory on a local drive')
        ancestor = ancestor.parent
    return ancestor / '.clear-temp' / identity


def _package_resources(package):
    _no_reparse(package, 'Offline package')
    value = _json(package / 'manifests/resources.json')
    if value.get('format') != 'clear-portable-resources-v1':
        raise ValueError('Unsupported offline resource manifest')
    resources = value['resources']
    if not isinstance(resources, dict) or not resources:
        raise ValueError('Offline resource manifest is empty or invalid')
    for name, member in resources.items():
        if not isinstance(name, str) or not isinstance(member, str) or not member:
            raise ValueError('Resource names and members must be nonempty strings')
        relative = Path(member)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Resource must be a package-relative member: ' + name)
    return resources


def _network(args):
    transport = args.transport or ('loopback-http' if args.mode == 'single' else 'lan-http')
    if args.mode == 'single' and transport != 'loopback-http':
        raise ValueError('单机模式只允许回环访问；局域网共享请使用带账号和项目权限的 multi 模式')
    if transport == 'https' and (not args.certificate or not args.private_key):
        raise ValueError('HTTPS requires --certificate and --private-key inside the selected --secrets directory')
    if transport != 'https' and (args.certificate or args.private_key):
        raise ValueError('TLS certificate options require --transport https')
    listen = args.listen or ('127.0.0.1' if transport == 'loopback-http' else None)
    if listen is None:
        raise ValueError('多人部署请用 --listen 指定服务器局域网 IP；例如 --transport lan-http --listen 192.168.1.8')
    scheme = 'https' if transport == 'https' else 'http'
    authority = '[' + listen + ']' if ':' in listen else listen
    origin = args.public_origin or f'{scheme}://{authority}:{args.portal_port}'
    network = dict(transport=transport, listen=listen, public_origin=origin,
                   portal_port=args.portal_port, worker_port=args.worker_port,
                   postgres_port=args.postgres_port)
    if args.certificate:
        network['certificate'] = str(Path(args.certificate).absolute())
    if args.private_key:
        network['private_key'] = str(Path(args.private_key).absolute())
    return network


def _initial_private(config):
    value = {'worker_token': secrets.token_urlsafe(48)}
    if config.mode == 'multi':
        value.update(pg_username='clear_owner', pg_password=secrets.token_urlsafe(48),
                     database='clear_tasks_' + config.installation_id.replace('-', '')[:12])
    return value


def _bind_tls(network, secrets_root):
    network = dict(network)
    if network['transport'] != 'https':
        return network
    secrets_root = _physical(secrets_root, 'TLS secrets directory')
    for field in ('certificate', 'private_key'):
        if not network.get(field):
            raise ValueError('HTTPS requires certificate and private_key paths')
        file = _physical(network[field], 'TLS ' + field)
        if not file.is_file() or not file.is_relative_to(secrets_root):
            raise ValueError('Place TLS certificate and private key inside --secrets before init; no configuration was published')
        network[field] = str(file)
        network[field + '_sha256'] = _hash(file)
    return network


def _may_seed_initial_vault(config, args):
    state_path = config.data / 'installation-state.json'
    state = _json(state_path) if state_path.is_file() else {}
    if state.get('phase') not in {None, 'configured'}:
        return False
    postgres = _pg_cluster_identity(config)
    if postgres.exists() and any(postgres.iterdir()):
        return False
    supplied_inputs = {Path(value).absolute() for value in
                       (args.admin_password_file, args.certificate, args.private_key,
                        config.network.get('certificate'), config.network.get('private_key')) if value}
    if config.roots['secrets'].exists() and any(path.is_file() and path not in supplied_inputs
            for path in config.roots['secrets'].rglob('*')):
        return False
    ephemeral = tuple(root for name, root in config.roots.items()
                      if name in {'cache','temp','paddle_temp','secrets','backup','postgres'}
                      and root != config.data and root.is_relative_to(config.data))
    return not config.data.exists() or all(child == config.path or child.name in {'.clear-lifecycle.lock', 'installation-state.json'}
               or any(child.is_relative_to(root) for root in ephemeral)
               for child in config.data.iterdir())


def initialize(args, package):
    package = _physical(package, 'Package')
    data = _physical(args.data, 'DATA')
    config_path = Path(args.config).absolute() if args.config else data / 'clear.install.json'
    if args.development_f_only:
        requested_paths = [package, data, config_path]
        requested_paths.extend(getattr(args, name) for name in
            ('cache', 'temp', 'paddle_temp', 'secrets', 'backup', 'postgres_data', 'certificate', 'private_key')
            if getattr(args, name, None) is not None)
        for requested in requested_paths:
            if _physical(requested, 'F-only development path').drive.upper() != 'F:':
                raise ValueError('All development installation files must remain on F:')
    if config_path.exists():
        if not args.resume:
            raise ValueError('安装配置已存在；使用现有 --config，或用 init --resume 继续未完成的初始化')
        config = load_install(config_path)
        if config.data != data or config.program != package or config.mode != args.mode:
            raise ValueError('Resume configuration does not match the package, DATA and mode')
        if args.certificate or args.private_key:
            if config.network['transport'] != 'https' or not (args.certificate and args.private_key):
                raise ValueError('TLS reconfiguration requires an existing HTTPS installation and both certificate/key flags')
            document = _json(config.path)
            document['network'] = _bind_tls({**config.network,
                'certificate': str(args.certificate), 'private_key': str(args.private_key)}, config.roots['secrets'])
            _write(config.path, document)
            config = load_install(config.path)
        _tls_options(config)
        _activate(config)
        if not _secret_file(config).is_file() and _may_seed_initial_vault(config, args):
            private = _initial_private(config)
            _save_secrets(config, private)
        else:
            private = _read_secrets(config)
    else:
        if data.exists() and any(data.iterdir()):
            raise ValueError('init requires an empty DATA directory; use restore to import existing business data')
        identity = str(uuid.uuid4())
        paths = dict(program=str(package), assets=str(package / 'assets'),
                     runtimes=str(package / 'runtimes'), tools=str(package / 'tools'),
                     data=str(data), cache=str(Path(args.cache).absolute() if args.cache else data / 'cache'),
                     temp=str(Path(args.temp).absolute() if args.temp else data / 'temp'),
                     paddle_temp=str(Path(args.paddle_temp).absolute() if args.paddle_temp else _ascii_temp(data, identity)),
                     secrets=str(Path(args.secrets).absolute() if args.secrets else data / '.secrets'),
                     backup=str(Path(args.backup).absolute() if args.backup else data / 'backups'),
                     postgres=str(Path(args.postgres_data).absolute() if args.postgres_data else data / '.postgres'))
        resources = _package_resources(package)
        missing = []
        for name, member in resources.items():
            resource = _physical(package / member, 'Package resource ' + name)
            if not resource.is_relative_to(package) or not resource.exists():
                missing.append(name)
        if missing:
            raise ValueError('Offline package resources are missing; repair before init: ' + ', '.join(missing))
        network = _bind_tls(_network(args), paths['secrets'])
        selected_secrets = _physical(paths['secrets'], 'Installation secrets directory')
        if (selected_secrets / 'services.dpapi.json').exists():
            raise ValueError('The selected secrets directory already contains installation credentials; choose a separate secrets directory or resume its own configuration')
        document = dict(schema_version=1, installation_id=identity, paths=paths,
                        mode=args.mode, network=network, resources=resources,
                        policy={'allowed_drives': ['F:']} if args.development_f_only else {})
        # Validate before creating writable roots. The temporary configuration is
        # next to the explicitly selected configuration, never in system TEMP.
        _local(config_path, 'Configuration')
        _no_reparse(config_path, 'Configuration')
        if config_path.is_relative_to(package):
            raise ValueError('Place clear.install.json outside the immutable offline program directory')
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _write(config_path, document)
        try:
            config = load_install(config_path)
        except Exception:
            config_path.unlink()
            raise
        _activate(config)
        private = _initial_private(config)
        _save_secrets(config, private)
        _write(config.data / 'installation-state.json', {
            'installation_id': config.installation_id, 'phase': 'configured', 'created_at': _stamp()})
    with _lifecycle(config):
        if config.mode == 'multi':
            _initialize_multi(config, private, args)
        _write(config.data / 'installation-state.json', {
            'installation_id': config.installation_id, 'phase': 'initialized', 'updated_at': _stamp()})
    return {'config': str(config.path), 'mode': config.mode, 'origin': config.network['public_origin'],
            'initialized': True, 'qualification': 'pending',
            'next': f'Clear.cmd --config "{config.path}" verify'}


def _activate(config, *, strict_resources=True):
    os.environ.update(config.environment(create=strict_resources, strict_resources=strict_resources))
    def bound(name):
        return config.resource(name) if strict_resources else config.check(config.resources[name], name, role='read')
    source = bound('platform_source') if 'platform_source' in config.resources else None
    for root in (source / 'src' if source else None, source,
                 config.resources.get('task_layer'), config.resources.get('agent_layer')):
        if root is not None:
            root = config.check(root, 'Dependency layer', role='read')
            if root.exists() and str(root) not in sys.path:
                sys.path.insert(0, str(root))
    net = config.network
    os.environ.update(WORKBENCH_DATA_DIR=str(config.data), CLEAR_TASK_ROOT=str(config.data),
        CLEAR_PUBLIC_ORIGIN=net['public_origin'],
        CLEAR_WORKER_URL=f"http://127.0.0.1:{net['worker_port']}",
        CLEAR_ALLOW_HTTP_LOOPBACK='1' if net['transport'] == 'loopback-http' else '0',
        CLEAR_ALLOW_LAN_HTTP='1' if net['transport'] == 'lan-http' else '0',
        PGPASSFILE=str(config.roots['secrets'] / 'pgpass.conf'),
        PGSERVICEFILE=str(config.roots['secrets'] / 'pg_service.conf'),
        PSQL_HISTORY=str(config.roots['cache'] / 'psql_history'))
    if 'task_layer' in config.resources:
        os.environ['CLEAR_TASK_LAYER'] = str(bound('task_layer'))
    if 'postgres_bin' in config.resources:
        os.environ['CLEAR_POSTGRES_BIN'] = str(bound('postgres_bin'))
    os.environ['WORKBENCH_AGENT_ENABLED'] = '1' if 'agent_layer' in config.resources else '0'
    return config


def _secret_file(config):
    return config.check(config.roots['secrets'] / 'services.dpapi.json')


def _save_secrets(config, value):
    from workbench_platform.agent.credentials import CredentialVault
    root = config.roots['secrets']
    root.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        result = subprocess.run(['icacls', str(root), '/inheritance:r', '/grant:r',
            getpass.getuser() + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'],
            capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('Unable to protect the installation credential directory')
    encrypted = CredentialVault._crypt(json.dumps(value, ensure_ascii=False).encode())
    _write(_secret_file(config), {'format': 'clear-service-secrets-v1',
        'installation_id': config.installation_id, 'account': getpass.getuser(),
        'dpapi': base64.b64encode(encrypted).decode()})


def _read_secrets(config):
    from workbench_platform.agent.credentials import CredentialVault
    try:
        value = _json(_secret_file(config))
        if value['installation_id'] != config.installation_id:
            raise ValueError('Credential installation mismatch')
        return json.loads(CredentialVault._crypt(base64.b64decode(value['dpapi'], validate=True), decrypt=True))
    except (KeyError, OSError, ValueError, UnicodeError):
        raise ValueError('本 Windows 账户无法读取服务凭据；请在目标机用运行服务的账户重新初始化或恢复凭据') from None


@contextmanager
def _lifecycle(config):
    path = config.check(config.data / '.clear-lifecycle.lock')
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Another lifecycle command is using this installation') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def verify(config, *, use_cache=False):
    from .integrity import verify_content
    result = verify_content(config, use_cache=use_cache)
    result['errors'].extend(_resource_binding_errors(config))
    result['ok'] = not result['errors']
    _write(config.roots['cache'] / 'package-verification.json', result)
    return result


def _resource_binding_errors(config):
    errors = []
    try:
        expected = _package_resources(config.program)
    except (OSError, ValueError, KeyError) as error:
        return [{'error': 'Cannot read the offline resource identity map: ' + str(error)}]
    for name in sorted(set(expected) | set(config.resources)):
        if name not in expected or name not in config.resources:
            errors.append({'resource': name, 'error': 'Installation resource IDs differ from the offline resource manifest'})
        elif config.resources[name].resolve() != (config.program / expected[name]).resolve():
            errors.append({'resource': name, 'error': 'Installation binding differs from its pinned package-relative resource member'})
    return errors


def _machine():
    gpu = []
    binary = shutil.which('nvidia-smi')
    if binary:
        result = subprocess.run([binary, '--query-gpu=uuid,driver_version', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        gpu = result.stdout.strip().splitlines() if result.returncode == 0 else ['unavailable']
    return dict(host=socket.gethostname(), account=getpass.getuser(), machine=platform.machine(), gpu=gpu)


def doctor(config):
    errors = [((item['resource'] + ': ') if 'resource' in item else '') + item['error']
              for item in _resource_binding_errors(config)]
    for name, root in config.roots.items():
        try:
            config.check(root, name, role='write' if name in {'data','cache','temp','paddle_temp','secrets','backup','postgres'} else 'read')
        except (ValueError, OSError) as error:
            errors.append(str(error))
    if os.name != 'nt' or platform.machine().upper() not in {'AMD64', 'X86_64'}:
        errors.append('This offline distribution requires Windows x64')
    try:
        _tls_options(config)
    except (ValueError, OSError) as error:
        errors.append(str(error))
    resources = {}
    for name in config.resources:
        try:
            resources[name] = {'path': str(config.resource(name)), 'available': True}
        except ValueError as error:
            resources[name] = {'available': False, 'repair': str(error)}
            errors.append(str(error))
    gpu = {'available': False}
    binary = shutil.which('nvidia-smi')
    if binary:
        result = subprocess.run([binary, '--query-gpu=name,driver_version,uuid', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        gpu = {'available': result.returncode == 0, 'devices': result.stdout.strip().splitlines()}
    return dict(ok=not errors, machine=_machine(), resources=resources, gpu=gpu,
                free_bytes=shutil.disk_usage(config.data).free, errors=errors,
                note='GPU presence is diagnostic only; every delivered engine requires a local real-task qualification')


def qualify(config, args):
    verification = verify(config, use_cache=True)
    if not verification['ok']:
        return dict(status='blocked', reason='Offline resource integrity/closure failed', verification=verification)
    path = config.roots['cache'] / 'qualification.json'
    if 'qualify_script' not in config.resources:
        result = dict(status='pending', reason='Package has no real-task qualification runner; resource presence does not qualify engines',
                      diagnostic=doctor(config))
    else:
        script = config.resource('qualify_script')
        output = config.roots['cache'] / ('qualification-run-' + secrets.token_hex(12))
        argv = [str(config.resource('platform_python')), '-B', str(script), '--config', str(config.path), '--output', str(output)]
        argv += ['--engine', args.engine or 'all']
        code = _run(config, argv, 'qualification.log')
        report = output / 'results.json'
        result = _json(report) if report.is_file() else {'complete': False, 'reason': 'Qualification runner produced no result'}
        local_report = (result.get('installation_id') == config.installation_id
                        and result.get('package_manifest_sha256') == verification['package_sha256'])
        result['status'] = 'passed' if not code and result.get('complete') is True and local_report else 'blocked'
        result['evidence'] = str(report)
    result.update(scope=args.engine or 'all', installation_id=config.installation_id, package_sha256=verification['package_sha256'],
                  machine=_machine(), checked_at=_stamp())
    _write(path, result)
    return result


def _port_free(host, port):
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((host, port))
    except OSError:
        raise RuntimeError(f'地址 {host}:{port} 无法监听或已被其他实例占用；请修复配置或明确停止占用者') from None


def _run(config, argv, log_name, *, env=None, timeout=None):
    logs = config.data / 'debug-logs'
    config.check(logs).mkdir(parents=True, exist_ok=True)
    with (logs / log_name).open('ab') as stream:
        result = subprocess.run(argv, cwd=config.data, env=env or os.environ.copy(),
            stdout=stream, stderr=subprocess.STDOUT, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.returncode


def _database_url(config, private, *, admin=False, database=None):
    if not admin:
        if private.get('runtime_database_url'):
            return private['runtime_database_url']
        raise ValueError('Limited database runtime credentials are missing; complete init --resume before starting services')
    return ('postgresql+psycopg://' + quote(private['pg_username'], safe='') + ':'
            + quote(private['pg_password'], safe='') + '@127.0.0.1:'
            + str(config.network['postgres_port']) + '/' + (database or private['database']))


def _pg_environment(config, private):
    return dict(os.environ, PGPASSWORD=private['pg_password'])


def _pg_cluster_identity(config):
    return config.roots.get('postgres', config.data / '.postgres')


def _postgres_status(config):
    root = _pg_cluster_identity(config)
    expected = {'installation_id': config.policy.get('postgres_owner_installation_id', config.installation_id),
                'postgres_root': str(root)}
    identity = root / '.clear-installation.json'
    if not identity.is_file() or _json(identity) != expected:
        return {'running': None, 'error': 'PostgreSQL cluster ownership is missing or differs'}
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', config.network['postgres_port'])) != 0:
            return {'running': False, 'root': str(root)}
    engine = None
    try:
        from sqlalchemy import create_engine, text
        private = _read_secrets(config)
        engine = create_engine(_database_url(config, private), connect_args={'connect_timeout': 3})
        with engine.connect() as db:
            actual = config.check(db.execute(text('SHOW data_directory')).scalar_one())
        if actual != root:
            return {'running': None, 'error': 'PostgreSQL port belongs to a different cluster'}
        return {'running': True, 'root': str(root), 'port': config.network['postgres_port'],
                'shared_owner': config.policy.get('postgres_owner_installation_id')}
    except Exception:
        return {'running': None, 'error': 'Cannot authenticate and verify the owned PostgreSQL endpoint'}
    finally:
        if engine is not None:
            engine.dispose()


def _start_pg(config, private):
    root = _pg_cluster_identity(config)
    identity_path = root / '.clear-installation.json'
    if not identity_path.is_file() or _json(identity_path) != {
        'installation_id': config.policy.get('postgres_owner_installation_id', config.installation_id), 'postgres_root': str(root)}:
        raise RuntimeError('PostgreSQL directory does not have this installation ownership binding')
    import sqlalchemy
    from sqlalchemy import text
    engine = sqlalchemy.create_engine(_database_url(config, private, admin=True, database='postgres'),
                                       connect_args={'connect_timeout': 3})
    def inspect():
        with engine.connect() as db:
            actual = config.check(db.execute(text('SHOW data_directory')).scalar_one())
            if actual != root:
                raise RuntimeError('PostgreSQL port belongs to a different cluster')
    try:
        with socket.socket() as probe:
            occupied = probe.connect_ex(('127.0.0.1', config.network['postgres_port'])) == 0
        if not occupied:
            _port_free('127.0.0.1', config.network['postgres_port'])
            code = _run(config, [str(config.resource('postgres_bin') / 'pg_ctl.exe'), 'start', '-D', str(root),
                '-l', str(config.data / 'debug-logs/postgres.log'), '-w', '-t', '30'], 'postgres-control.log', timeout=45)
            if code:
                raise RuntimeError('PostgreSQL startup failed; inspect DATA/debug-logs/postgres-control.log')
        inspect()
    finally:
        engine.dispose()


def _initialize_multi(config, private, args):
    root = _pg_cluster_identity(config)
    config.check(root)
    if not (root / 'PG_VERSION').is_file():
        if root.exists() and any(root.iterdir()):
            raise ValueError('PostgreSQL init requires a new empty cluster directory')
        password_file = config.roots['secrets'] / '.postgres-bootstrap-password'
        password_file.write_text(private['pg_password'], 'utf-8')
        try:
            code = _run(config, [str(config.resource('postgres_bin') / 'initdb.exe'), '-D', str(root),
                '-U', private['pg_username'], '--pwfile=' + str(password_file), '--encoding=UTF8', '--locale=C',
                '--data-checksums', '--auth-local=scram-sha-256', '--auth-host=scram-sha-256'], 'postgres-init.log', timeout=180)
            if code:
                raise RuntimeError('PostgreSQL initialization failed; inspect DATA/debug-logs/postgres-init.log')
        finally:
            password_file.unlink(missing_ok=True)
        with (root / 'postgresql.conf').open('a', encoding='utf-8') as stream:
            stream.write("\nlisten_addresses = '127.0.0.1'\nport = " + str(config.network['postgres_port'])
                         + "\nfsync = on\nsynchronous_commit = on\nfull_page_writes = on\nlogging_collector = on\nlog_directory = 'log'\n")
        _write(root / '.clear-installation.json', {
            'installation_id': config.installation_id, 'postgres_root': str(root)})
    _start_pg(config, private)
    from sqlalchemy import create_engine, text
    from workbench_platform.tasks.database import migrate, check_schema
    owner = create_engine(_database_url(config, private, admin=True, database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with owner.connect() as db:
            exists = db.execute(text('SELECT 1 FROM pg_database WHERE datname=:name'), {'name': private['database']}).scalar()
            if not exists:
                db.execute(text('CREATE DATABASE "' + private['database'] + '"'))
    finally:
        owner.dispose()
    engine = create_engine(_database_url(config, private, admin=True))
    try:
        with engine.connect() as db:
            has_schema = db.execute(text("SELECT to_regclass('clear_alembic_version')")).scalar_one() is not None
        if not has_schema:
            migrate(engine)  # Explicit init of this new database, never a start-time migration.
        check_schema(engine)
        from workbench_platform.tasks.store import Ledger
        with engine.connect() as db:
            has_admin = bool(db.execute(text('SELECT 1 FROM users WHERE is_admin=true LIMIT 1')).scalar())
        if not has_admin:
            password = (config.check(args.admin_password_file, 'Admin password file', role='read').read_text('utf-8').strip()
                        if args.admin_password_file else getpass.getpass('Clear administrator password: '))
            Ledger(engine).create_user(args.admin_username, args.admin_name, password, admin=True)
        if not private.get('runtime_database_url'):
            _provision_runtime(config, private)
    except Exception as error:
        if isinstance(error, (RuntimeError, ValueError)):
            raise
        raise RuntimeError('Central initialization failed; use init --resume after repairing the installation') from None
    finally:
        engine.dispose()


def _provision_runtime(config, private):
    from scripts.provision_task_runtime import provision
    admin_file = config.roots['secrets'] / '.provision-admin.json'
    runtime_file = config.roots['secrets'] / '.provision-runtime.json'
    try:
        _write(admin_file, {'database_url': _database_url(config, private, admin=True), 'worker_token': private['worker_token']})
        provision(admin_file, runtime_file)
        private['runtime_database_url'] = _json(runtime_file)['database_url']
        _save_secrets(config, private)
    finally:
        admin_file.unlink(missing_ok=True); runtime_file.unlink(missing_ok=True)


def _process_identity(pid):
    from workbench_platform.resources.recovery import process_identity
    return process_identity(pid)


def _alive(record):
    from workbench_platform.resources.recovery import owner_active
    return owner_active(SimpleNamespace(pid=record['pid'], creation_time=record['creation_time']))


def _state_path(config, role):
    return config.check(config.data / '.clear-services' / (role + '.json'))


def _owned(config, role):
    path = _state_path(config, role)
    if not path.is_file():
        return None
    record = _json(path)
    if (record.get('installation_id') != config.installation_id
            or record.get('data') != str(config.data) or record.get('role') != role):
        raise RuntimeError('Service ownership differs; no process was stopped or reused')
    try:
        return record if _alive(record) else None
    except OSError as error:
        raise RuntimeError('Unable to verify the exact Windows process owner; no process was stopped') from error


def status(config):
    roles = ['single'] if config.mode == 'single' else ['portal', 'worker']
    services = {role: {'running': bool(record), 'pid': record['pid'] if record else None}
                for role in roles for record in [_owned(config, role)]}
    result = dict(installation_id=config.installation_id, config=str(config.path), services=services,
                  origin=config.network['public_origin'])
    if config.mode == 'multi':
        result['postgres'] = _postgres_status(config)
        result['ok'] = result['postgres']['running'] is not None
    return result


def _start_role(config, role):
    running = _owned(config, role)
    launch_identity = {'package_sha256': _hash(config.program / 'manifests/package.json'),
                       'config_sha256': _hash(config.path)}
    if running:
        if any(running.get(name) != value for name, value in launch_identity.items()):
            raise ValueError('服务的源码或安装配置已改变；请排空并明确停止该服务后再启动')
        return {'running': True, 'reused': True}
    port = config.network['worker_port' if role == 'worker' else 'portal_port']
    address = '127.0.0.1' if role == 'worker' else config.network['listen']
    _port_free(address, port)
    launch_id = secrets.token_hex(16)
    source = config.resource('platform_source')
    entry = config.check(source / 'scripts/clear_portable.py', role='read')
    argv = [str(config.resource('platform_python')), '-B', str(entry), '--package-root', str(config.program),
            '--config', str(config.path), '_serve', '--role', role, '--launch-id', launch_id]
    logs = config.data / 'debug-logs'
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / (role + '.log')).open('ab') as output:
        child = subprocess.Popen(argv, cwd=config.data, env=os.environ.copy(), stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    record = dict(installation_id=config.installation_id, data=str(config.data), role=role,
                  pid=child.pid, creation_time=_process_identity(child.pid), launch_id=launch_id, started_at=_stamp(),
                  **launch_identity)
    _write(_state_path(config, role), record)
    ready = _state_path(config, role).with_name(role + '-' + launch_id + '.ready')
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f'{role} failed to start; inspect {logs / (role + ".log")}')
        if ready.is_file() and _json(ready).get('launch_id') == launch_id:
            return {'running': True, 'pid': child.pid}
        time.sleep(.25)
    raise RuntimeError(f'{role} startup is still pending; inspect its owned status/log before retrying')


def start(config, args):
    _tls_options(config)
    initialized = config.data / 'installation-state.json'
    state = _json(initialized) if initialized.is_file() else {}
    if state.get('installation_id') != config.installation_id or state.get('phase') != 'initialized':
        raise ValueError('Installation initialization is incomplete; complete init --resume before starting services')
    verification = verify(config, use_cache=True)
    if not verification['ok']:
        raise ValueError('Offline package is incomplete or changed; run verify and repair its listed members')
    qualification_path = config.roots['cache'] / 'qualification.json'
    qualification = _json(qualification_path) if qualification_path.is_file() else {}
    qualified = (qualification.get('status') == 'passed'
        and qualification.get('scope') == 'all'
        and qualification.get('package_sha256') == verification['package_sha256']
        and qualification.get('installation_id') == config.installation_id
        and qualification.get('machine') == _machine())
    if not qualified and not args.allow_unqualified:
        raise ValueError('This installation has no matching local real-task qualification; run qualify (isolated candidate testing may explicitly use --allow-unqualified)')
    with _lifecycle(config):
        private = _read_secrets(config)
        os.environ['CLEAR_WORKER_TOKEN'] = private['worker_token']
        if config.mode == 'multi':
            _start_pg(config, private)
            os.environ['CLEAR_DATABASE_URL'] = _database_url(config, private)
            from workbench_platform.tasks.database import engine_for, check_schema
            engine = engine_for()
            try:
                check_schema(engine)  # No implicit migration.
            finally:
                engine.dispose()
            worker = _start_role(config, 'worker')
            portal = _start_role(config, 'portal')
            return dict(worker=worker, portal=portal, origin=config.network['public_origin'], qualified=qualified)
        return dict(single=_start_role(config, 'single'), origin=config.network['public_origin'], qualified=qualified)


def _stop_role(config, role, timeout):
    record = _owned(config, role)
    if record is None:
        return {'running': False}
    path = _state_path(config, role).with_name(role + '-' + record['launch_id'] + '.stop')
    _write(path, {'installation_id': config.installation_id, 'launch_id': record['launch_id']})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _owned(config, role):
            return {'running': False, 'stopped': True}
        time.sleep(.25)
    raise RuntimeError(f'{role} is still stopping; no forced termination was performed')


class DrainGate:
    """Reject new writes while one installation is explicitly draining."""
    def __init__(self, app, data):
        self.app = app
        self.marker = Path(data) / '.clear-drain.json'

    async def __call__(self, scope, receive, send):
        if (scope['type'] == 'http' and scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}
                and self.marker.is_file()):
            from starlette.responses import JSONResponse
            response = JSONResponse({'detail': '正在等待已开始的任务完成并停止服务，请稍后再提交'}, status_code=503)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _single_active(config):
    live = {'queued', 'running', 'processing', 'importing', 'rendering', 'waiting_gpu',
            'waiting_resource', 'waiting_job', 'pausing', 'cancelling', 'stopping', 'needs_verification'}
    migration = config.data / 'path-migration.json'
    record = _json(migration) if migration.is_file() else {}
    blocked = set(record.get('restored_blocked_rows', [])) if record.get('installation_id') == config.installation_id else set()
    counts = {}
    for member, table, column, row in _single_rows(config):
        state = _row_state(row, column)
        if state == 'needs_verification' and _blocked_fingerprint(member, table, column, row) in blocked:
            continue  # Unchanged imported history has never run in this installation.
        if state in live:
            counts[member, table] = counts.get((member, table), 0) + 1
    return [{'ledger': member, 'table': table, 'count': count} for (member, table), count in counts.items()]


def stop(config, args):
    with _lifecycle(config):
        if config.mode == 'single':
            if not _owned(config, 'single'):
                return {'single': {'running': False}}
            marker = config.data / '.clear-drain.json'
            if marker.exists():
                raise ValueError('Another drain operation owns this installation')
            _write(marker, {'installation_id': config.installation_id, 'phase': 'draining', 'started_at': _stamp()})
            try:
                deadline = time.monotonic() + args.timeout
                while _single_active(config):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Active or uncertain tasks have not drained; the single-user service was retained')
                    time.sleep(.5)
                _assert_gpu_idle(config)
                return {'single': _stop_role(config, 'single', args.timeout)}
            finally:
                marker.unlink(missing_ok=True)
        result = {'portal': _stop_role(config, 'portal', args.timeout)}
        if not args.all:
            result['worker_retained'] = True
            return result
        private = _read_secrets(config)
        postgres = _postgres_status(config)
        if postgres['running'] is None:
            raise RuntimeError(postgres['error'] + '; no cluster was stopped')
        if postgres['running'] is False:
            if _owned(config, 'worker'):
                raise RuntimeError('Worker remains active while PostgreSQL is unavailable; no unverified drain/stop was performed')
            result.update(worker={'running': False}, postgres_stopped=True)
            return result
        os.environ['CLEAR_DATABASE_URL'] = _database_url(config, private)
        from workbench_platform.tasks.database import engine_for
        from sqlalchemy import text
        engine = engine_for()
        flag = config.data / 'central-maintenance.json'
        if flag.exists():
            raise ValueError('Another maintenance operation owns the drain marker')
        _write(flag, {'phase': 'draining', 'installation_id': config.installation_id, 'started_at': _stamp()})
        try:
            deadline = time.monotonic() + args.timeout
            while True:
                with engine.connect() as db:
                    from workbench_platform.tasks.store import digest
                    active = db.execute(text("SELECT count(*) FROM task_dispatches d JOIN task_runs r ON r.id=d.run_id WHERE (d.state IN ('leased','observing') AND (d.kind='control' OR r.status NOT IN ('paused','waiting_user'))) OR (d.state='needs_verification' AND EXISTS (SELECT 1 FROM task_workers w WHERE w.id=d.worker_id AND w.root_identity=:root))"), {'root': digest(str(config.data))}).scalar_one()
                if not active:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Active or uncertain tasks have not drained; worker and database retained')
                time.sleep(.5)
            _assert_gpu_idle(config)
            result['worker'] = _stop_role(config, 'worker', args.timeout)
            if config.policy.get('postgres_owner_installation_id') not in {None, config.installation_id}:
                result['postgres_retained_shared_owner'] = config.policy['postgres_owner_installation_id']
                return result
            postgres = _postgres_status(config)
            if postgres['running'] is not True:
                if postgres['running'] is None:
                    raise RuntimeError(postgres['error'] + '; no cluster was stopped')
                result['postgres_stopped'] = True
                return result
            root = _pg_cluster_identity(config)
            code = _run(config, [str(config.resource('postgres_bin') / 'pg_ctl.exe'), 'stop', '-D', str(root),
                '-m', 'fast', '-w', '-t', str(min(args.timeout, 300))], 'postgres-control.log', timeout=args.timeout + 10)
            if code:
                raise RuntimeError('PostgreSQL shutdown pending; no foreign process was terminated')
            result['postgres_stopped'] = True
            return result
        finally:
            engine.dispose()
            flag.unlink(missing_ok=True)


async def serve(config, args):
    lease = None
    if args.role == 'single':
        from scripts.candidate_bootstrap import _FileLease
        lease = _FileLease(config.data / '.live-preview.lock')
        if not lease.acquire():
            raise RuntimeError('Another platform process already owns this DATA directory')
    try:
        await _serve_app(config, args)
    finally:
        if lease:
            lease.close()


async def _serve_app(config, args):
    import uvicorn
    private = _read_secrets(config)
    os.environ['CLEAR_WORKER_TOKEN'] = private['worker_token']
    if args.role == 'single':
        from scripts.portable_native import create_app
        app = create_app(config)
    else:
        os.environ['CLEAR_DATABASE_URL'] = _database_url(config, private)
        from workbench_platform.tasks.database import engine_for, check_schema
        engine = engine_for(); check_schema(engine)
        if args.role == 'worker':
            from scripts.portable_native import configure
            from workbench_platform.tasks.worker import create_worker_app
            app = create_worker_app(engine, initializer=lambda: configure(config))
        else:
            from workbench_platform.tasks.server import create_app
            app = create_app(engine)
    host = '127.0.0.1' if args.role == 'worker' else config.network['listen']
    port = config.network['worker_port' if args.role == 'worker' else 'portal_port']
    tls = _tls_options(config) if args.role != 'worker' else {}
    wrapped = DrainGate(app, config.data) if args.role == 'single' else app
    server = uvicorn.Server(uvicorn.Config(wrapped, host=host, port=port, access_log=False, **tls))
    app.state.shutdown_callback = lambda: setattr(server, 'should_exit', True)
    task = asyncio.create_task(server.serve())
    control = _state_path(config, args.role).parent
    ready = control / (args.role + '-' + args.launch_id + '.ready')
    stop_path = control / (args.role + '-' + args.launch_id + '.stop')
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError('Server stopped before startup completed')
            await asyncio.sleep(.1)
        _write(ready, {'installation_id': config.installation_id, 'launch_id': args.launch_id})
        while not task.done():
            if stop_path.is_file():
                request = _json(stop_path)
                if request == {'installation_id': config.installation_id, 'launch_id': args.launch_id}:
                    server.should_exit = True
            await asyncio.sleep(.25)
        await task
    finally:
        ready.unlink(missing_ok=True); stop_path.unlink(missing_ok=True)


def _tls_options(config):
    if config.network['transport'] != 'https':
        return {}
    result = {}
    for field, option in (('certificate', 'ssl_certfile'), ('private_key', 'ssl_keyfile')):
        if not config.network.get(field):
            raise ValueError('HTTPS certificate/private_key pair is missing; place both files inside the installation secrets root')
        file = config.check(config.network[field], 'TLS ' + field, role='read')
        if not file.is_file() or not file.is_relative_to(config.roots['secrets']):
            raise ValueError('TLS certificate/private key must exist inside this installation secrets root')
        if _hash(file) != config.network.get(field + '_sha256'):
            raise ValueError('TLS file identity changed; verify the replacement and explicitly update its installation hash binding')
        result[option] = str(file)
    return result


def backup(config):
    with _lifecycle(config):
        if config.mode == 'multi':
            private = _read_secrets(config)
            os.environ['CLEAR_DATABASE_URL'] = _database_url(config, private)
            from workbench_platform.tasks.database import engine_for
            from workbench_platform.tasks.maintenance import backup as central_backup
            engine = engine_for()
            try:
                return central_backup(engine, config.data)
            finally:
                engine.dispose()
        if _owned(config, 'single'):
            raise ValueError('Stop the single-user service before taking a consistent backup')
        from scripts.candidate_bootstrap import _FileLease
        lease = _FileLease(config.data / '.live-preview.lock')
        if not lease.acquire():
            raise ValueError('Another platform process owns DATA; stop it before backup')
        try:
            _assert_gpu_idle(config)
            destination = config.roots['backup'] / ('single-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4))
            config.check(destination).mkdir(parents=True)
            excluded = {'.clear-services', '.clear-lifecycle.lock', '.live-preview.lock', '.central-worker.lock',
                        'debug-logs', 'installation-state.json', 'clear.install.json', 'agent-native',
                        'installation', 'ocr-token.txt', 'path-migration.json'}
            excluded_paths = {config.roots[key] for key in ('cache', 'temp', 'paddle_temp', 'secrets', 'backup', 'postgres') if key in config.roots}
            records = []
            from .snapshots import sqlite_snapshot, external_assets
            for current, dirs, files in os.walk(config.data, followlinks=False):
                current = Path(current)
                dirs[:] = [name for name in dirs if name not in excluded
                           and current / name not in excluded_paths and name != '__pycache__'
                           and not _machine_member((current / name).relative_to(config.data))]
                for name in [*dirs, *files]:
                    config.check(current / name, 'Business backup member', role='read')
                for name in files:
                    source = current / name
                    if (name in excluded or source in excluded_paths
                            or _machine_member(source.relative_to(config.data))
                            or name.endswith(('-wal', '-shm', '-journal', '.lock'))):
                        continue
                    relative = source.relative_to(config.data)
                    target = destination / 'files' / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.suffix == '.sqlite3':
                        sqlite_snapshot(source, target)
                    else:
                        shutil.copy2(source, target)
                    records.append({'path': relative.as_posix(), 'sha256': _hash(target), 'size_bytes': target.stat().st_size})
            external_map = {}
            recorded = {record['path'] for record in records}
            for original in external_assets(config.data):
                registered_original = original
                # The local backup ingress accepts only canonical fixed-local,
                # non-reparse originals explicitly named by the native DB.
                # It does not broaden the live service's generic read roots.
                expected_hash = _hash(original)
                relative = Path('native/classification/external-backup') / (expected_hash + original.suffix.lower())
                target = destination / 'files' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(original, target)
                if _hash(target) != expected_hash or _hash(original) != expected_hash:
                    raise ValueError('External original changed while taking its consistent backup')
                if relative.as_posix() not in recorded:
                    records.append({'path': relative.as_posix(), 'sha256': expected_hash, 'size_bytes': target.stat().st_size})
                    recorded.add(relative.as_posix())
                external_map[str(registered_original)] = relative.as_posix()
            _write(destination / 'manifest.json', dict(format='clear-single-backup-v1', source_root=str(config.data),
                created_at=_stamp(), external_path_map=external_map,
                legacy_data_roots=config.policy.get('legacy_data_roots', []),
                legacy_file_map=config.policy.get('legacy_file_map', {}), files=records))
            return {'backup': str(destination), 'files': len(records), 'verified': True}
        finally:
            lease.close()


def restore(config, args):
    backup_root = config.check(args.backup, 'Backup input', role='read')
    manifest = _json(backup_root / 'manifest.json')
    expected_format = 'clear-central-backup-v1' if config.mode == 'multi' else 'clear-single-backup-v1'
    if manifest.get('format') != expected_format:
        raise ValueError('Backup mode/format differs from this installation')
    destination = _physical(args.destination, 'Restore destination')
    if destination.exists():
        raise ValueError('Restore requires a new nonexistent DATA directory; originals are retained')
    for record in manifest['files']:
        relative = Path(record['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Backup contains an escaping member')
        path = config.check(backup_root / 'files' / relative, 'Backup member', role='read')
        if not path.is_file() or _hash(path) != record['sha256']:
            raise ValueError('Backup file integrity failed: ' + record['path'])
    with _lifecycle(config):
        value = _json(config.path)
        identity = str(uuid.uuid4())
        state = destination.parent / (destination.name + '.clear-state') / identity
        value['installation_id'] = identity
        if value['network']['transport'] == 'https':
            for field in ('certificate', 'private_key', 'certificate_sha256', 'private_key_sha256'):
                value['network'].pop(field, None)
        # Caches and binding configuration initially live beside the new DATA,
        # keeping the destination nonexistent until consistent restore creates it.
        for name, path in dict(data=destination, cache=state / 'cache', temp=state / 'temp',
                               secrets=state / 'secrets', paddle_temp=_ascii_temp(state, identity)).items():
            value['paths'][name] = str(path)
        value.pop('model_mappings', None)
        value['policy'] = {**value.get('policy', {})}
        value['policy']['legacy_data_roots'] = list(dict.fromkeys([
            manifest['source_root'], *manifest.get('legacy_data_roots', []), *value['policy'].get('legacy_data_roots', [])]))
        value['policy']['legacy_file_map'] = {
            **value['policy'].get('legacy_file_map', {}), **manifest.get('legacy_file_map', {}),
            **manifest.get('external_path_map', {})}
        if config.mode == 'multi':
            value['policy']['postgres_owner_installation_id'] = config.policy.get('postgres_owner_installation_id', config.installation_id)
        else:
            value['paths']['postgres'] = str(state / 'postgres')
        new_path = state / 'clear.install.json'
        _write(new_path, value)
        restored_config = load_install(new_path)
        private = {'worker_token': secrets.token_urlsafe(48)}
        if config.mode == 'multi':
            old_private = _read_secrets(config)
            _start_pg(config, old_private)
            private.update({name: old_private[name] for name in ('pg_username', 'pg_password', 'database')})
            from sqlalchemy import create_engine
            from workbench_platform.tasks.maintenance import restore as central_restore
            engine = create_engine(_database_url(config, old_private, admin=True))
            _activate(restored_config)
            try:
                result = central_restore(engine, backup_root, destination)
            finally:
                engine.dispose()
            _archive_machine_state(restored_config)
            record = _json(destination / 'central-restore-private.json')
            private['database'] = urlsplit(record['database_url']).path.lstrip('/')
            _provision_runtime(restored_config, private)
            (destination / 'central-restore-private.json').unlink()
        else:
            _activate(restored_config)
            destination.mkdir(parents=True)
            for record in manifest['files']:
                target = restored_config.check(destination / record['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_root / 'files' / record['path'], target)
            retired = _archive_machine_state(restored_config)
            from .migration import relocate_sqlite
            migrations = []
            for path in destination.rglob('*.sqlite3'):
                restored_config.check(path)
                with closing(sqlite3.connect(path)) as db, db:
                    report = relocate_sqlite(db, manifest['source_root'], str(destination), {
                        **manifest.get('legacy_file_map', {}), **manifest.get('external_path_map', {})})
                migrations.append({'path': path.relative_to(destination).as_posix(), **report})
            _write(destination / 'path-migration.json', {
                'schema_version': 1, 'installation_id': restored_config.installation_id,
                'restored_blocked_rows': _restored_blocked_rows(restored_config),
                'source_backup': str(backup_root), 'source_root': manifest['source_root'],
                'destination': str(destination), 'databases': migrations, 'retired_machine_state': retired, 'created_at': _stamp()})
            result = {'destination': str(destination), 'verified': True}
        _save_secrets(restored_config, private)
        _write(destination / 'installation-state.json', {
            'installation_id': identity, 'phase': 'initialized', 'restored_at': _stamp(), 'source_backup': str(backup_root)})
        return {**result, 'config': str(new_path), 'qualification': 'pending',
                'credentials': 'Service tokens regenerated; API credentials require target-account verification/re-entry'}


def rebind(config, package):
    """Relocate the same verified package without rewriting business history."""
    package = _physical(package, 'Relocated package')
    resources = _package_resources(package)
    receipt_path = config.roots['cache'] / 'package-verification.json'
    if not receipt_path.is_file():
        raise ValueError('Rebind requires the original package verification receipt; verify before moving the package')
    receipt = _json(receipt_path)
    new_manifest_hash = _hash(package / 'manifests/package.json')
    if not receipt.get('ok') or receipt.get('package_sha256') != new_manifest_hash:
        raise ValueError('Rebind accepts only the same verified package; a content upgrade requires separate migration and qualification')
    with _lifecycle(config), _data_idle_lease(config):
        roles = ['single'] if config.mode == 'single' else ['portal', 'worker']
        if any(_owned(config, role) for role in roles):
            raise ValueError('Stop all owned services before rebinding the package')
        _assert_gpu_idle(config)
        if config.mode == 'multi':
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', config.network['postgres_port'])) == 0:
                    raise ValueError('Stop the owned PostgreSQL cluster with stop --all before rebinding')
        private = _read_secrets(config)
        previous = _json(config.path)
        document = json.loads(json.dumps(previous))
        document['installation_id'] = str(uuid.uuid4())
        document['resources'] = resources
        for name, path in dict(program=package, assets=package / 'assets', runtimes=package / 'runtimes', tools=package / 'tools').items():
            document['paths'][name] = str(path)
        document.pop('model_mappings', None)
        # A separately shared/restore cluster retains its original explicit owner.
        cluster_sidecar = _pg_cluster_identity(config) / '.clear-installation.json'
        owns_cluster = config.mode == 'multi' and not config.policy.get('postgres_owner_installation_id')
        encrypted_before = _secret_file(config).read_bytes()
        environment_before, paths_before = dict(os.environ), sys.path[:]
        _write(config.path.with_name(config.path.stem + '.before-rebind.json'), previous)
        try:
            _write(config.path, document)
            updated = load_install(config.path)
            _activate(updated)
            verified = verify(updated)
            if not verified['ok']:
                raise ValueError('Relocated package integrity failed; original configuration retained')
            private['worker_token'] = secrets.token_urlsafe(48)
            _save_secrets(updated, private)
            if owns_cluster:
                _write(cluster_sidecar, {'installation_id': updated.installation_id,
                    'postgres_root': str(_pg_cluster_identity(updated))})
            retired = _archive_machine_state(updated, preserve_current_locks=True)
            migration = updated.data / 'path-migration.json'
            if migration.is_file():
                record = _json(migration)
                if record.get('installation_id') == config.installation_id:
                    record['installation_id'] = updated.installation_id
                    _write(migration, record)
            updated.check(updated.roots['cache'] / 'qualification.json').unlink(missing_ok=True)
            _write(updated.data / 'installation-state.json', {
                'installation_id': updated.installation_id, 'phase': 'initialized', 'rebound_at': _stamp(),
                'previous_program': str(config.program), 'program': str(updated.program), 'retired_machine_state': retired})
            return {'rebound': True, 'config': str(updated.path), 'program': str(updated.program),
                    'qualification': 'pending', 'service_tokens': 'regenerated'}
        except Exception:
            _write(config.path, previous)
            _secret_file(config).write_bytes(encrypted_before)
            if owns_cluster:
                _write(cluster_sidecar, {'installation_id': config.installation_id,
                    'postgres_root': str(_pg_cluster_identity(config))})
            os.environ.clear(); os.environ.update(environment_before)
            sys.path[:] = paths_before
            raise


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--package-root', type=Path, default=argparse.SUPPRESS)
    common.add_argument('--config', type=Path, default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog='Clear.cmd', parents=[common])
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', parents=[common])
    init.add_argument('--mode', choices=('single', 'multi'), required=True)
    init.add_argument('--data', type=Path, required=True)
    for name in ('cache', 'temp', 'paddle-temp', 'secrets', 'backup', 'postgres-data'):
        init.add_argument('--' + name, type=Path)
    init.add_argument('--transport', choices=('loopback-http', 'lan-http', 'https'))
    init.add_argument('--listen'); init.add_argument('--public-origin')
    init.add_argument('--portal-port', type=int, default=8889)
    init.add_argument('--worker-port', type=int, default=8890)
    init.add_argument('--postgres-port', type=int, default=55439)
    init.add_argument('--certificate', type=Path); init.add_argument('--private-key', type=Path)
    init.add_argument('--admin-username', default='admin'); init.add_argument('--admin-name', default='管理员')
    init.add_argument('--admin-password-file', type=Path)
    init.add_argument('--development-f-only', action='store_true'); init.add_argument('--resume', action='store_true')
    for command in ('verify', 'doctor', 'status', 'backup', 'migrate', 'rebind'):
        sub.add_parser(command, parents=[common])
    start = sub.add_parser('start', parents=[common]); start.add_argument('--allow-unqualified', action='store_true')
    stop = sub.add_parser('stop', parents=[common]); stop.add_argument('--all', action='store_true'); stop.add_argument('--timeout', type=int, default=300)
    qualify = sub.add_parser('qualify', parents=[common]); qualify.add_argument('--engine')
    restore = sub.add_parser('restore', parents=[common]); restore.add_argument('--backup', type=Path, required=True); restore.add_argument('--destination', type=Path, required=True)
    internal = sub.add_parser('_serve', parents=[common], help=argparse.SUPPRESS)
    internal.add_argument('--role', choices=('single', 'portal', 'worker'), required=True)
    internal.add_argument('--launch-id', required=True)
    return parser


def main(argv=None, *, package_root=None):
    args = build_parser().parse_args(argv)
    args.config = getattr(args, 'config', None)
    package = Path(getattr(args, 'package_root', None) or package_root or Path.cwd()).absolute()
    try:
        if args.command == 'init':
            result = initialize(args, package)
        else:
            if not args.config:
                raise ValueError('指定本机安装配置：Clear.cmd --config <DATA/clear.install.json> ' + args.command)
            config = load_install(args.config)
            if args.command == 'rebind':
                result = rebind(config, package)
                print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
            if config.program != package.resolve():
                raise ValueError('配置属于另一个程序目录；请使用对应离线包，搬迁后请明确执行 rebind')
            _activate(config, strict_resources=args.command not in {'doctor', 'verify', 'status'})
            if args.command in {'verify', 'doctor', 'qualify', '_serve'} and config.policy.get('isolation_guard'):
                from .io_guard import install as install_io_guard
                install_io_guard(config)
            if args.command == '_serve':
                asyncio.run(serve(config, args)); return 0
            if args.command == 'verify': result = verify(config)
            elif args.command == 'doctor': result = doctor(config)
            elif args.command == 'qualify': result = qualify(config, args)
            elif args.command == 'status': result = status(config)
            elif args.command == 'start': result = start(config, args)
            elif args.command == 'stop':
                if not 1 <= args.timeout <= 3600: raise ValueError('timeout must be between 1 and 3600 seconds')
                result = stop(config, args)
            elif args.command == 'backup': result = backup(config)
            elif args.command == 'restore': result = restore(config, args)
            elif args.command == 'migrate':
                if config.mode != 'multi': raise ValueError('Only multi-user mode has a central PostgreSQL schema')
                private = _read_secrets(config)
                os.environ['CLEAR_DATABASE_URL'] = _database_url(config, private, admin=True)
                from workbench_platform.tasks.database import engine_for, migrate
                engine = engine_for()
                try: migrate(engine)
                finally: engine.dispose()
                result = {'migrated': True}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok', True) and result.get('status') not in {'pending', 'blocked', 'failed'} else 1
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(json.dumps({'error': str(error), 'command': args.command}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
