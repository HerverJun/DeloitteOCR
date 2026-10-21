"""Verified schema-5 Store bridge for the sealed schema-4 classification worker.

Only the API's persistence module is overlaid. Worker, inference, datasets,
training, model validation, GPU leasing and Windows Job handling remain sealed.
The gated GPU child re-enters the same verified private launcher before reading
the native payload; no database version is disguised or schema check disabled.
"""
from __future__ import annotations

import ast
from contextlib import closing
from copy import deepcopy
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys


FORMAT = 'clear-classification-store-compat-v1'
MIGRATION_5 = '''
ALTER TABLE result_disposition_events ADD COLUMN operation_id TEXT;
ALTER TABLE result_disposition_events ADD COLUMN request_id TEXT;
ALTER TABLE result_disposition_events ADD COLUMN actor_json TEXT;
CREATE TABLE result_disposition_requests (
 project_id TEXT NOT NULL REFERENCES projects(id), request_id TEXT NOT NULL,
 operation_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL REFERENCES jobs(id),
 instance_id TEXT NOT NULL, payload_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(project_id,request_id)
);
PRAGMA user_version=5;
'''


def checked(value):
    path = Path(value).absolute()
    if (path.drive.casefold() != 'f:' or path.resolve().drive.casefold() != 'f:'
            or any(item.is_symlink() or item.is_junction() for item in (path, *path.parents))):
        raise ValueError('Classification compatibility inputs must be real local F paths')
    return path.resolve()


def sha(path):
    return hashlib.sha256(checked(path).read_bytes()).hexdigest()


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _sql(value):
    return [' '.join(statement.split()) for statement in value.split(';') if statement.strip()]


def validate(api_source, asset):
    """Prove the Store change is the supported additive 4-to-5 API upgrade."""
    api_source, asset = checked(api_source), checked(asset)
    api_store = checked(api_source / 'src/image_platform/store.py')
    sealed_store = checked(asset / 'src/image_platform/store.py')
    api_settings = checked(api_source / 'src/image_platform/settings.py')
    sealed_settings = checked(asset / 'src/image_platform/settings.py')
    if api_settings.read_bytes() != sealed_settings.read_bytes():
        raise ValueError('Classification Store settings contract differs from the sealed worker')
    previous = ast.parse(sealed_store.read_text('utf-8'))
    current = ast.parse(api_store.read_text('utf-8'))
    before = next(node for node in previous.body if isinstance(node, ast.ClassDef) and node.name == 'Store')
    after = next(node for node in current.body if isinstance(node, ast.ClassDef) and node.name == 'Store')
    old_methods = {node.name: node for node in before.body if isinstance(node, ast.FunctionDef)}
    new_methods = {node.name: node for node in after.body if isinstance(node, ast.FunctionDef)}
    if set(new_methods) - set(old_methods) != {'create_project', 'disposition_operation'} or set(old_methods) - set(new_methods):
        raise ValueError('Classification Store API additions are outside the compatibility contract')
    for name, method in old_methods.items():
        if name not in {'__init__', 'dispose_results'} and _dump(method) != _dump(new_methods[name]):
            raise ValueError('Classification sealed worker Store method changed: ' + name)
    # Build the one supported initializer from the sealed initializer. Its
    # refusal of unknown versions and transactional migrations stay intact.
    expected_init = deepcopy(old_methods['__init__'])
    migration_4 = expected_init.body[-1].body[-1]
    migration_4.body.append(ast.parse('version = 4').body[0])
    migration_4.orelse = []
    expected_init.body[-1].body.extend(ast.parse('''
if version == 4:
    for statement in MIGRATION_5.split(";"):
        if statement.strip():
            con.execute(statement)
elif version != 5:
    raise RuntimeError(f"Unsupported database version {version}")
''').body)
    if _dump(expected_init) != _dump(new_methods['__init__']):
        raise ValueError('Classification Store schema checks are outside the compatibility contract')
    migrations = [node for node in current.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == 'MIGRATION_5' for target in node.targets)]
    if len(migrations) != 1 or _sql(ast.literal_eval(migrations[0].value)) != _sql(MIGRATION_5):
        raise ValueError('Classification migration 5 is not the supported additive migration')
    # All module globals, migrations 1..4, helpers and remaining class members
    # must be structurally identical, except the explicitly named API additions.
    normalized = deepcopy(current)
    normalized.body = [node for node in normalized.body
                       if node is not migrations[0]
                       and not (isinstance(node, ast.Import) and len(node.names) == 1 and node.names[0].name == 're')
                       and not (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'MIGRATION_5' for target in node.targets))]
    normalized_class = next(node for node in normalized.body if isinstance(node, ast.ClassDef) and node.name == 'Store')
    normalized_class.body = [deepcopy(old_methods[node.name]) if isinstance(node, ast.FunctionDef)
                             and node.name in {'__init__', 'dispose_results'} else node
                             for node in normalized_class.body if not (isinstance(node, ast.FunctionDef)
                             and node.name in {'create_project', 'disposition_operation'})]
    if _dump(normalized) != _dump(previous):
        raise ValueError('Classification persistence module has unsupported structural changes')
    return {'format': FORMAT, 'api_source': str(api_source), 'store_path': str(api_store),
            'store_sha256': sha(api_store), 'settings_sha256': sha(api_settings),
            'sealed_store_sha256': sha(sealed_store),
            'sealed_resources_sha256': sha(asset / 'src/image_platform/platform_resources.py'),
            'adapter_path': str(checked(__file__)), 'adapter_sha256': sha(__file__),
            'schema_version': 5, 'sealed_schema_version': 4}


def verify(binding):
    descriptor = binding.get('store_compatibility', {})
    if descriptor.get('format') != FORMAT:
        raise ValueError('Classification schema compatibility declaration missing')
    expected = validate(binding['source'], binding['asset'])
    if descriptor != expected:
        raise ValueError('Classification schema compatibility identity changed')
    return descriptor


class _GatedSubprocess:
    """A module-local proxy; native reservation and OwnedJob code are unchanged."""
    def __init__(self, original, launcher, binding_hash):
        self.original, self.launcher, self.binding_hash = original, launcher, binding_hash

    def __getattr__(self, name):
        return getattr(self.original, name)

    def Popen(self, command, *args, **kwargs):
        if command != [sys.executable, '-m', 'image_platform.platform_resources']:
            raise ValueError('Unexpected classification GPU child command')
        return self.original.Popen([sys.executable, '-I', '-B', '-u', str(self.launcher),
                                    '--gpu-child', '--binding-sha256', self.binding_hash], *args, **kwargs)


def install(binding, launcher):
    """Install before importing sealed CLI/worker and preserve their import roots."""
    descriptor = verify(binding)
    launcher = checked(launcher)
    if sha(launcher) != binding.get('worker_entry_sha256'):
        raise ValueError('Classification private worker entry identity changed')
    data = checked(Path(binding['data_root']) / 'native/classification')
    if checked(os.environ.get('ICP_DATA_DIR', data)) != data:
        raise ValueError('Classification worker data differs from its private binding')
    database = checked(data / 'platform.sqlite3')
    if database.exists():
        # Refuse future schemas before Settings.prepare or Store's WAL setup.
        # The Store still performs its own real migrations and version checks.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in {0, 1, 2, 3, 4, 5}:
            raise RuntimeError(f'Unsupported database version {version}')
    if 'image_platform.store' in sys.modules or 'image_platform.worker' in sys.modules:
        raise ValueError('Classification compatibility must be installed before worker imports')
    spec = importlib.util.spec_from_file_location('image_platform.store', descriptor['store_path'])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    import image_platform
    image_platform.store = module
    from image_platform import platform_resources
    platform_resources.subprocess = _GatedSubprocess(platform_resources.subprocess, launcher,
                                                    sha(launcher.parent / 'binding.json'))
    return module
