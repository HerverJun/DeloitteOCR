"""Machine bindings are mutable; resource content identities are not.

Unmanaged development deliberately retains the local F-drive policy. A managed
installation accepts fixed local Windows disks, but only its registered roots.
This module has no dependency on the platform web stack or any inference engine.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
import uuid

READ_ROOTS = ('program', 'assets', 'runtimes', 'tools')
WRITE_ROOTS = ('data', 'cache', 'temp', 'paddle_temp', 'secrets', 'backup', 'postgres')
ALL_ROOTS = READ_ROOTS + WRITE_ROOTS


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f'Duplicate installation setting: {key}')
        result[key] = value
    return result


def _local(value: Path, label: str) -> Path:
    path = Path(value).absolute()
    if os.name == 'nt':
        # UNC, device namespace and mapped network drives are not installations.
        if not path.drive or len(path.drive) != 2 or path.drive[1] != ':':
            raise ValueError(f'{label} requires an ordinary local drive: {path}')
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        get_type.argtypes, get_type.restype = [ctypes.c_wchar_p], ctypes.c_uint
        if get_type(path.anchor) != 3:  # DRIVE_FIXED
            raise ValueError(f'{label} requires a fixed local drive: {path}')
    elif str(path).startswith('//'):
        raise ValueError(f'{label} cannot be a network path')
    return path


def _reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _no_reparse(path: Path, label: str) -> None:
    for ancestor in (path, *path.parents):
        if _reparse(ancestor):
            raise ValueError(f'{label} cannot traverse a reparse point: {ancestor}')


@dataclass(frozen=True)
class InstallConfig:
    path: Path
    installation_id: str
    roots: dict[str, Path]
    mode: str
    network: dict
    resources: dict[str, Path]
    policy: dict
    mappings: tuple[tuple[Path, Path], ...] = ()

    @property
    def program(self):
        return self.roots['program']

    @property
    def data(self):
        return self.roots['data']

    def check(self, value, label='Project path', *, role='write') -> Path:
        if role not in {'read', 'write'}:
            raise ValueError('Unknown storage role: ' + role)
        requested = Path(value).absolute()
        # Frozen result/request snapshots keep their exact original bytes. Only
        # read views of a registered old DATA prefix follow a restore mapping;
        # old model/runtime references are never redirected to today's default.
        if role == 'read' and '..' not in requested.parts:
            for original, member in self.policy.get('legacy_file_map', {}).items():
                if requested == Path(original):
                    requested = self.data / member
                    break
            if not any(requested.is_relative_to(root) for root in self.roots.values()):
                for old in self.policy.get('legacy_data_roots', []):
                    try:
                        relative = requested.relative_to(Path(old))
                    except ValueError:
                        continue
                    requested = self.data / relative
                    break
        path = _local(requested, label)
        actual = _local(path.resolve(), label)
        allowed_drives = self.policy.get('allowed_drives')
        if allowed_drives and any(p.drive.upper() not in allowed_drives for p in (path, actual)):
            raise ValueError(f'{label} is outside this installation drive policy: {path}')
        roots = WRITE_ROOTS if role == 'write' else ALL_ROOTS
        if not any(path.is_relative_to(self.roots[key]) for key in roots) or not any(actual.is_relative_to(self.roots[key]) for key in roots):
            raise ValueError(f'{label} is outside registered {role} roots: {path}')
        # Only explicitly registered read-only Paddle aliases are permitted.
        for ancestor in ((path, *path.parents) if path != actual else ()):
            if _reparse(ancestor):
                permitted = role == 'read' and (any(ancestor == link and ancestor.resolve() == target
                    and actual.is_relative_to(target) for link, target in self.mappings)
                    or self.trusted_model_alias(ancestor, ancestor.resolve()))
                if not permitted:
                    raise ValueError(f'{label} traverses an unregistered reparse point: {ancestor}')
        return actual

    def trusted_model_alias(self, link, target):
        model_root = self.resources.get('ocr_models')
        return bool(model_root and link.is_relative_to(self.roots['paddle_temp'])
            and link.name == 'models' and link.parent.name.startswith('offlineocr-paddle-')
            and target == model_root.resolve() and not any(_reparse(p) for p in (target, *target.parents)))

    def resource(self, name: str) -> Path:
        if name not in self.resources:
            raise ValueError(f'Missing installation resource {name}; rebuild or repair this offline package')
        path = self.check(self.resources[name], f'Resource {name}', role='read')
        if not path.exists():
            raise ValueError(f'Installation resource {name} is missing: {path}')
        return path

    def environment(self, data_root: Path | None = None, *, create=True, strict_resources=True) -> dict[str, str]:
        data = self.check(data_root or self.data, 'DATA')
        if not data.is_relative_to(self.data):
            raise ValueError('Child DATA must be beneath installation DATA')
        values = _environment(data, self.roots['cache'], self.roots['temp'], self.roots['paddle_temp'])
        def bound_resource(name):
            return self.resource(name) if strict_resources else self.check(self.resources[name], name, role='read')
        values.update(CLEAR_INSTALL_CONFIG=str(self.path), CLEAR_INSTALLATION_ID=self.installation_id,
            CLEAR_RUNTIME_PACKAGE_ROOT=str(bound_resource('platform_source')),
            CLEAR_PACKAGE_ROOT=str(self.program), WORKBENCH_DATA_ROOT=str(data),
            CLEAR_NATIVE_TEMP_ROOT=str(self.roots['paddle_temp']),
            CLEAR_LAYOUT_TEMP_ROOT=str(self.roots['paddle_temp'] / 'layout'),
            CLEAR_SECRETS_ROOT=str(self.roots['secrets']), CLEAR_BACKUP_ROOT=str(self.roots['backup']))
        values['CLEAR_READ_ROOTS'] = json.dumps([str(self.roots[key]) for key in ALL_ROOTS])
        values['CLEAR_WRITE_ROOTS'] = json.dumps([str(self.roots[key]) for key in WRITE_ROOTS])
        if self.policy.get('isolation_guard'):
            values['CLEAR_IO_GUARD'] = '1'
        env_resources = {
            'platform_python': 'CLEAR_PLATFORM_PYTHON', 'data_python': 'WORKBENCH_DATA_PYTHON',
            'documents_python': 'WORKBENCH_DOCUMENTS_PYTHON', 'libreoffice': 'WORKBENCH_LIBREOFFICE',
            'forensics_python': 'WORKBENCH_FORENSICS_PYTHON', 'node': 'WORKBENCH_HARNESS_NODE',
            'agent_layer': 'WORKBENCH_AGENT_LAYER', 'ocr_source': 'WORKBENCH_OCR_SOURCE',
            'mineru_python': 'WORKBENCH_MINERU_PYTHON', 'mineru_model': 'WORKBENCH_MINERU_MODEL',
            'aigc': 'CLEAR_AIGC_ASSET_ROOT', 'safire': 'CLEAR_SAFIRE_ASSET_ROOT',
            'program_assets': 'WORKBENCH_PROGRAM_ROOT', 'portal_dist': 'WORKBENCH_PORTAL_DIST',
        }
        for resource, variable in env_resources.items():
            if resource in self.resources:
                values[variable] = str(bound_resource(resource))
        for resource_name, variable in (('mineru_python', 'CLEAR_MINERU_PRO_PYTHON'),
                ('mineru_model', 'CLEAR_MINERU_PRO_MODEL'), ('safire_python', 'WORKBENCH_SAFIRE_PYTHON'),
                ('mineru_manifest', 'CLEAR_MINERU_PRO_RUNTIME_MANIFEST'), ('postgres_bin', 'CLEAR_POSTGRES_BIN')):
            if resource_name in self.resources:
                values[variable] = str(bound_resource(resource_name))
        if 'mineru_manifest' in self.resources and (strict_resources or self.resources['mineru_manifest'].is_file()):
            import hashlib
            values['CLEAR_MINERU_PRO_RUNTIME_MANIFEST_SHA256'] = hashlib.sha256(self.resource('mineru_manifest').read_bytes()).hexdigest()
        if 'program_assets' in self.resources:
            native = bound_resource('program_assets')
            for family in ('ocr', 'classification'):
                values['CLEAR_' + family.upper() + '_CONTROL_PYTHON'] = str(
                    self.check(native/'bundles'/family/'runtimes/control/python.exe', 'Native control runtime', role='read'))
        if create:
            for key in _DIRECTORIES:
                directory = self.check(values[key], key)
                directory.mkdir(parents=True, exist_ok=True)
        return values


def load_install(path, *, package_root: Path | None = None) -> InstallConfig:
    path = _local(Path(path), 'Installation configuration')
    _no_reparse(path, 'Installation configuration')
    value = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_pairs)
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') != 1:
        raise ValueError('Unsupported clear.install.json schema_version (expected 1)')
    for name in ('paths', 'network', 'resources'):
        if not isinstance(value.get(name), dict):
            raise ValueError(f'Installation {name} must be an object')
    if not isinstance(value.get('policy', {}), dict):
        raise ValueError('Installation policy must be an object')
    if not isinstance(value.get('installation_id'), str):
        raise ValueError('Installation installation_id must be a UUID string')
    if any(not isinstance(member, str) or not member for member in value['paths'].values()):
        raise ValueError('Installation paths must be nonempty local path strings')
    if any(not isinstance(member, str) or not member for member in value['resources'].values()):
        raise ValueError('Installation resources must be nonempty member strings')
    if not isinstance(value.get('mode'), str):
        raise ValueError('Installation mode must be single or multi')
    identity = str(uuid.UUID(value['installation_id']))
    paths = dict(value['paths'])
    paths.setdefault('postgres', str(Path(paths['data']) / '.postgres'))
    if set(paths) != set(ALL_ROOTS):
        raise ValueError('Installation paths must define exactly: ' + ', '.join(ALL_ROOTS))
    roots = {}
    for name in ALL_ROOTS:
        raw = Path(paths[name])
        base = (package_root if name in READ_ROOTS and package_root else path.parent)
        requested = _local(raw if raw.is_absolute() else base / raw, name)
        _no_reparse(requested, name)
        roots[name] = requested.resolve()
    if not roots['paddle_temp'].as_posix().isascii():
        raise ValueError('paddle_temp must be an ASCII path; select a local ASCII temporary root')
    for writer in WRITE_ROOTS:
        if any(roots[writer].is_relative_to(roots[reader]) or roots[reader].is_relative_to(roots[writer]) for reader in READ_ROOTS):
            raise ValueError(f'Writable {writer} overlaps immutable program/resources')
    mode = value.get('mode')
    if mode not in {'single', 'multi'}:
        raise ValueError('Installation mode must be single or multi')
    network = value['network']
    if any(not isinstance(network.get(key), str) for key in ('public_origin','listen','transport')):
        raise ValueError('Network listen, transport and public_origin must be strings')
    for port in ('portal_port', 'worker_port', 'postgres_port'):
        if type(network.get(port)) is not int or not 1 <= network[port] <= 65535:
            raise ValueError('Invalid network ' + port)
    if len({network[key] for key in ('portal_port', 'worker_port', 'postgres_port')}) != 3:
        raise ValueError('Portal, private worker and PostgreSQL require distinct ports')
    origin = urlsplit(network['public_origin'])
    if origin.scheme not in {'http', 'https'} or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        raise ValueError('public_origin must be one exact HTTP(S) origin without path')
    if (origin.port or (443 if origin.scheme == 'https' else 80)) != network['portal_port']:
        raise ValueError('public_origin port differs from portal_port')
    transport = network.get('transport')
    if transport not in {'loopback-http', 'lan-http', 'https'}:
        raise ValueError('network transport must be loopback-http, lan-http or https')
    if mode == 'single' and transport != 'loopback-http':
        raise ValueError('Single-user mode requires loopback HTTP; initialize multi-user mode for LAN access')
    if transport == 'loopback-http' and (network.get('listen') != '127.0.0.1' or origin.hostname != '127.0.0.1' or origin.scheme != 'http'):
        raise ValueError('loopback-http requires 127.0.0.1')
    if (transport == 'https') != (origin.scheme == 'https'):
        raise ValueError('Network transport and Origin scheme differ')
    if transport == 'lan-http':
        import ipaddress
        try:
            listen = ipaddress.ip_address(network.get('listen', ''))
            host = ipaddress.ip_address(origin.hostname)
        except ValueError as exc:
            raise ValueError('lan-http requires an explicit private LAN IP address') from exc
        lan_ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')
        if not any(listen in ipaddress.ip_network(net) for net in lan_ranges) or listen != host:
            raise ValueError('lan-http listen and Origin must name the same private LAN IP')
    resources = {}
    for name, member in value.get('resources', {}).items():
        raw = Path(member)
        resources[name] = raw if raw.is_absolute() else roots['program'] / raw
    policy = value.get('policy', {})
    if 'allowed_drives' in policy:
        policy = {**policy, 'allowed_drives': [drive.upper() for drive in policy['allowed_drives']]}
        if path.drive.upper() not in policy['allowed_drives']:
            raise ValueError('Installation configuration is outside this installation drive policy')
    for old in policy.get('legacy_data_roots', []):
        source = Path(old)
        if not source.is_absolute() or len(source.drive) != 2 or '..' in source.parts:
            raise ValueError('Legacy DATA mappings require an exact local absolute prefix')
        if any(roots[key].is_relative_to(source) for key in (*READ_ROOTS, 'data')):
            raise ValueError('Legacy DATA mapping cannot shadow current installation roots')
    for original, member in policy.get('legacy_file_map', {}).items():
        source, relative = Path(original), Path(member)
        if (not source.is_absolute() or len(source.drive) != 2 or '..' in source.parts
                or relative.is_absolute() or relative.drive or '..' in relative.parts):
            raise ValueError('Legacy file mappings require an exact local source and a relative DATA member')
        if any(source.is_relative_to(roots[key]) for key in (*READ_ROOTS, 'data')):
            raise ValueError('Legacy file mappings cannot shadow current program or DATA members')
    mappings = []
    for item in value.get('model_mappings', []):
        link, target = Path(item['link']).absolute(), Path(item['target']).absolute()
        if not link.is_relative_to(roots['paddle_temp']) or not target.is_relative_to(roots['assets']):
            raise ValueError('Model aliases require a Paddle temp link and registered assets target')
        _no_reparse(target, 'Model target')
        if link.exists() and link.resolve() != target.resolve():
            raise ValueError('Registered model alias target differs')
        mappings.append((link, target.resolve()))
    config = InstallConfig(path.resolve(), identity, roots, mode, network, resources, policy, tuple(mappings))
    for resource in resources.values():
        config.check(resource, 'Resource member', role='read')
    for name, root in roots.items():
        config.check(root, name, role='read' if name in READ_ROOTS else 'write')
    return config


_current_cache = None


def current_install() -> InstallConfig | None:
    global _current_cache
    configured = os.environ.get('CLEAR_INSTALL_CONFIG')
    if not configured:
        return None
    path = Path(configured).absolute()
    info = path.stat()  # Missing bindings never fall back to development.
    stamp = (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if _current_cache is None or _current_cache[0] != stamp:
        _current_cache = (stamp, load_install(path))
    return _current_cache[1]


def require_path(value, label='Project path', *, role='write') -> Path:
    config = current_install()
    if config is not None:
        return config.check(value, label, role=role)
    path = _local(Path(value), label)
    actual = _local(path.resolve(), label)
    if path.drive.upper() != 'F:' or actual.drive.upper() != 'F:':
        raise ValueError(f'{label} must remain on the local F drive in unmanaged development: {path}')
    # Legacy development asset junctions are allowed only within F. Managed
    # installations above reject them unless explicitly registered model aliases.
    return actual


def require_model_mapping(link, target) -> tuple[Path, Path]:
    config = current_install()
    target = require_path(target, 'Paddle model target', role='read')
    link = Path(link).absolute()
    if config:
        if (link, target) not in config.mappings and not config.trusted_model_alias(link, target):
            raise ValueError('Paddle alias is not registered in this installation')
        if link.exists() and link.resolve() != target:
            raise ValueError('Paddle alias target differs')
    else:
        require_path(link, 'Paddle alias', role='read')
    return link, target


def resource_path(name, *, environment=None) -> Path:
    config = current_install()
    if config:
        return config.resource(name)
    if environment and os.environ.get(environment):
        return require_path(os.environ[environment], name, role='read')
    raise ValueError(f'Missing {name}; initialize clear.install.json or explicitly configure {environment or name}')


_CACHE_VARIABLES = {
    'APPDATA': 'appdata', 'LOCALAPPDATA': 'localappdata', 'PIP_CACHE_DIR': 'pip',
    'npm_config_cache': 'npm', 'npm_config_store_dir': 'pnpm-store', 'PNPM_HOME': 'pnpm',
    'HF_HOME': 'huggingface', 'TORCH_HOME': 'torch', 'XDG_CACHE_HOME': '.',
    'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'data', 'PADDLE_PDX_CACHE_HOME': 'paddlex',
    'PADDLEOCR_HOME': 'paddleocr', 'MPLCONFIGDIR': 'matplotlib',
    'HYPOTHESIS_STORAGE_DIRECTORY': 'hypothesis', 'JUPYTER_CONFIG_DIR': 'jupyter',
    'JUPYTER_DATA_DIR': 'jupyter-data', 'PLAYWRIGHT_BROWSERS_PATH': 'playwright',
    'CUDA_CACHE_PATH': 'cuda', 'TORCHINDUCTOR_CACHE_DIR': 'torchinductor',
    'TRITON_CACHE_DIR': 'triton', 'NUMBA_CACHE_DIR': 'numba', 'MODELSCOPE_CACHE': 'modelscope',
    'DSH_HOME': 'dsh', 'DSH_RESOURCE_CACHE': 'dsh-resources', 'NODE_COMPILE_CACHE': 'node-compile',
    'UV_CACHE_DIR': 'uv', 'TIKTOKEN_CACHE_DIR': 'tiktoken',
}
_DIRECTORIES = (*_CACHE_VARIABLES, 'TEMP', 'TMP', 'TMPDIR', 'CLEAR_PADDLE_TEMP')


def _environment(data, cache, temporary, paddle):
    return {**{key: str(cache / suffix) for key, suffix in _CACHE_VARIABLES.items()},
        'TEMP': str(temporary), 'TMP': str(temporary), 'TMPDIR': str(temporary),
        'CLEAR_PADDLE_TEMP': str(paddle), 'CLEAR_NATIVE_TEMP_ROOT': str(paddle),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
        'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD': '1',
        'NODE_OPTIONS': '', 'NODE_PATH': '', 'DSH_PRIMARY_RUNTIME': '',
        'PIP_NO_INDEX': '1', 'UV_OFFLINE': '1', 'npm_config_offline': 'true',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
        'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK': 'True', 'DO_NOT_TRACK': '1',
        'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'}


def storage_environment(data_root) -> dict[str, str]:
    config = current_install()
    if config:
        return config.environment(Path(data_root))
    data = require_path(data_root, 'Data directory')
    paddle = require_path(Path(os.environ.get('CLEAR_PADDLE_TEMP', str(data / 'temp/paddle'))), 'Paddle temporary directory')
    if not str(paddle).isascii():
        raise ValueError('Configure an ASCII CLEAR_PADDLE_TEMP before using a non-ASCII DATA directory')
    values = _environment(data, data / 'cache', paddle, paddle)
    # Preserve the established local native configuration location.
    values['LOCALAPPDATA'] = str(data / 'native-local')
    for key in _DIRECTORIES:
        require_path(values[key], key).mkdir(parents=True, exist_ok=True)
    return values
