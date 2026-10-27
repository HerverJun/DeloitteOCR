"""Build a self-contained offline Clear directory from explicitly pinned inputs.

The input lock is a build-machine file, never a runtime dependency. Distributed
identities contain only package-relative names. This tool never uses a historical
candidate.json and never declares a copied model to be machine-qualified.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile

SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
             '.cache', '.huggingface', '.bin'}
SOURCE_SKIP = SKIP_DIRS | {'node_modules', 'dist', 'build', 'output', '.venv', 'venv',
                          '.hypothesis', '.vite', '.vite-temp'}
SKIP_NAMES = {'runtime-local.json', 'direct_url.json', 'development-machine.json',
              'gpu-qualification.json', '.DS_Store'}
NODE_VERSION = '24.22.0'
NODE_SHA256 = 'f9683a19877e12a9b167305c8db71040d812876359de21f0d2e97c3f321f80c3'
RESOURCES = {
    'platform_source': 'app/platform', 'ocr_source': 'app/ocr',
    'classification_source': 'app/classification', 'program_assets': 'assets/program',
    'platform_python': 'assets/program/runtimes/platform/python.exe',
    'data_python': 'assets/program/runtimes/data/python.exe',
    'documents_python': 'runtimes/documents/python.exe',
    'mineru_python': 'runtimes/mineru-pro/python.exe',
    'mineru_model': 'assets/mineru/MinerU2.5-Pro-2605-1.2B',
    'ocr_models': 'assets/program/bundles/ocr/models',
    'libreoffice': 'tools/libreoffice/program/soffice.com',
    'node': 'tools/node/node.exe', 'agent_layer': 'runtimes/agent',
    'task_layer': 'runtimes/task-layer', 'postgres_bin': 'tools/postgresql/bin',
    'portal_dist': 'app/platform/apps/portal/dist', 'aigc': 'assets/aigc',
    'safire': 'assets/safire',
    'safire_python': 'assets/program/runtimes/catnet/python.exe',
    'forensics_python': 'assets/program/runtimes/forensics/python.exe',
    'qualify_script': 'app/platform/scripts/portable_acceptance.py',
    'pdf_tables': 'app/ocr/resources/pdf-tables/site-packages',
    'pdf_table_config': 'app/ocr/config/pdf-table-tool.json',
    'mineru_manifest': 'manifests/mineru-runtime.json',
    'deployment_guide': 'tools/docs/deployment.md',
}
_HASH_CACHE = {}
_SCAN_STATS = {}


def physical(value, *, exists=False):
    """This build machine is F-only, independently of target deployment policy."""
    path = Path(value).absolute()
    resolved = path.resolve()
    if path.drive.upper() != 'F:' or resolved.drive.upper() != 'F:':
        raise ValueError('Portable build files must remain on local F: ' + str(path))
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError('Portable build paths cannot traverse links: ' + str(path))
    if exists and not path.exists():
        raise FileNotFoundError(path)
    return resolved


def relative_member(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or PurePosixPath(value).is_absolute()
            or any(p in {'', '.', '..'} for p in value.split('/'))):
        raise ValueError('Invalid package member: ' + repr(value))
    return value


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(path):
    before = path.stat()
    key = (str(path), before.st_size, before.st_mtime_ns)
    if key in _HASH_CACHE:
        return dict(_HASH_CACHE[key])
    value = {'bytes': before.st_size, 'sha256': digest(path)}
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Input changed while hashing: ' + str(path))
    _HASH_CACHE[key] = value
    return value


def save(path, value):
    path = physical(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', 'utf-8')


def environment(work):
    work = physical(work)
    for name in ('NODE_OPTIONS', 'NODE_PATH', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE'):
        os.environ.pop(name, None)
    for name, suffix in {
        'TEMP': 'temp', 'TMP': 'temp', 'TMPDIR': 'temp',
        'APPDATA': 'cache/appdata', 'LOCALAPPDATA': 'cache/localappdata',
        'XDG_CACHE_HOME': 'cache', 'XDG_CONFIG_HOME': 'cache/config',
        'XDG_DATA_HOME': 'cache/data', 'PIP_CACHE_DIR': 'cache/pip',
        'npm_config_cache': 'cache/npm', 'DSH_HOME': 'cache/dsh',
        'DSH_RESOURCE_CACHE': 'cache/dsh', 'NODE_COMPILE_CACHE': 'cache/node',
        'HF_HOME': 'cache/huggingface', 'TORCH_HOME': 'cache/torch',
        'MPLCONFIGDIR': 'cache/matplotlib', 'CUDA_CACHE_PATH': 'cache/cuda',
    }.items():
        target = physical(work / suffix)
        target.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(target)
    os.environ.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
        PYTHONIOENCODING='utf-8', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
        HF_HUB_DISABLE_TELEMETRY='1', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1',
        PIP_DISABLE_PIP_VERSION_CHECK='1', DO_NOT_TRACK='1')
    for variable, name in (('npm_config_userconfig', 'npm-user.npmrc'),
                           ('npm_config_globalconfig', 'npm-global.npmrc')):
        config = work / 'cache' / name
        if not config.exists():
            config.write_text('', 'utf-8')
        os.environ[variable] = str(config)


def members(root, *, source=False, exclude=()):
    root = physical(root, exists=True)
    if root.is_file():
        yield root
        return
    skip = (SOURCE_SKIP if source else SKIP_DIRS) | set(exclude)
    pending = [root]
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as scan:
            entries = sorted(scan, key=lambda row: row.name)
        for entry in entries:
            if entry.name in skip or entry.name in SKIP_NAMES:
                continue
            value = entry.stat(follow_symlinks=False)
            if (stat.S_ISLNK(value.st_mode)
                    or getattr(value, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError('Linked package member: ' + entry.path)
            if stat.S_ISDIR(value.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(value.st_mode):
                if (entry.name.endswith(('.pyc', '.pyo', '.tsbuildinfo', '.pending', '.part'))
                        or entry.name.startswith(('__editable__', '.env'))):
                    continue
                result = Path(entry.path)
                _SCAN_STATS[str(result)] = value
                yield result


def default_inputs(repository, work):
    """Explicit local discovery map; these paths never enter the distribution."""
    repository, work = physical(repository, exists=True), physical(work)
    output = repository / 'output'
    program = repository / 'release/Clear/versions/0.10.0/program'
    return {
        'format': 'clear-portable-inputs-v1',
        'repository': str(repository), 'source': str(repository / 'products/Clear'),
        'deployment_guide': str(repository / 'docs/Clear离线部署与迁移.md'),
        'program': str(program),
        'aigc': str(output / 'aigc-image-detection-20261022'),
        'safire': str(output / 'forensics-effective-upgrade-20261020/safire-integration-20261021'),
        'documents': str(output / 'clear-mineru-integration-20261021/dependencies/docvortex'),
        'mineru_runtime': str(output / 'clear-mineru-integration-20261021/dependencies/mineru_pro'),
        'mineru_model': str(output / 'clear-mineru-integration-20261021/models/MinerU2.5-Pro-2605-1.2B'),
        'libreoffice': str(output / 'clear-document-compare-20261022/dependencies/libreoffice'),
        'node': str(output / f'agent-harness-upgrade-20261015/dependencies/node-v{NODE_VERSION}-win-x64'),
        'node_archive': str(output / f'agent-harness-upgrade-20261015/dependencies/node-v{NODE_VERSION}-win-x64.zip'),
        'agent_layer': str(output / 'platform-agent-dev-20261004/dependencies/agent'),
        'harness_modules': str(work / 'dependencies/harness/node_modules'),
        'harness_receipt': str(work / 'dependencies/harness/install-receipt.json'),
        'task_layer': str(output / 'task-history-multiuser-20261015/dependencies/python'),
        'postgres': str(output / 'task-history-multiuser-20261015/dependencies/postgresql/pgsql'),
        'postgres_archive': str(output / 'task-history-multiuser-20261015/dependencies/postgresql-17.11-5.zip'),
        'task_wheelhouse': str(output / 'task-history-multiuser-20261015/dependencies/wheelhouse'),
        'pdf_table_tool': str(repository / 'release/Clear/versions/0.10.0/ocr/build/public-quality-20260916/tool/site-packages/pdfplumber'),
        'frontend': {name: str(work / 'build' / name) for name in ('portal', 'ocr', 'classification')},
        'downloads': [],
    }


def fetch_missing(settings, work):
    """Only explicitly supplied, hash-pinned HTTPS objects may be fetched."""
    for entry in settings.get('downloads', []):
        target = physical(entry['target'])
        expected = entry.get('sha256', '')
        url = entry.get('url', '')
        if (not re.fullmatch('[0-9a-f]{64}', expected) or not url.startswith('https://')
                or type(entry.get('bytes')) is not int or entry['bytes'] <= 0):
            raise ValueError('Downloads require HTTPS URL, positive bytes and fixed SHA256')
        if target.exists():
            if identity(target) != {k: entry[k] for k in ('bytes', 'sha256')}:
                raise ValueError('Existing download differs from its fixed identity: ' + str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + '.part')
        try:
            with urllib.request.urlopen(url, timeout=60) as response, partial.open('xb') as stream:
                shutil.copyfileobj(response, stream, 1024 * 1024)
            if identity(partial) != {k: entry[k] for k in ('bytes', 'sha256')}:
                raise ValueError('Downloaded bytes do not match the fixed identity: ' + url)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)


def prepare_harness(settings, work):
    """Restore npm tarballs using the authored lock, without lifecycle scripts."""
    environment(work)
    source = physical(settings['source'], exists=True) / 'platform/agent_harness'
    destination = physical(Path(settings['harness_modules']).parent)
    if (destination / 'node_modules').exists():
        raise FileExistsError('Preserve existing npm input; use a new input-map harness_modules root for a fresh install')
    archive = physical(settings['node_archive'], exists=True)
    if digest(archive) != NODE_SHA256:
        raise ValueError('Node archive differs from pinned official SHA256')
    node = physical(settings['node'], exists=True)
    with zipfile.ZipFile(archive) as bundle:
        with bundle.open(f'node-v{NODE_VERSION}-win-x64/node.exe') as stream:
            expected = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest(node / 'node.exe') != expected:
        raise ValueError('Node executable differs from the pinned archive')
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(source / name, destination / name)
    locked = digest(destination / 'package-lock.json')
    subprocess.run([str(node / 'node.exe'), str(node / 'node_modules/npm/bin/npm-cli.js'),
        'ci', '--ignore-scripts', '--include=optional', '--no-audit', '--no-fund',
        '--cache', str(physical(work) / 'cache/npm')], cwd=destination, env=os.environ.copy(), check=True)
    if locked != digest(source / 'package-lock.json') or locked != digest(destination / 'package-lock.json'):
        raise ValueError('Authored npm lock changed during installation')
    save(settings['harness_receipt'], {'format': 'clear-npm-ci-receipt-v1',
        'package_lock_sha256': locked, 'node_version': NODE_VERSION,
        'command': ['npm', 'ci', '--ignore-scripts', '--include=optional', '--no-audit', '--no-fund'],
        'tarball_integrity': 'npm ci verified integrity fields from the fixed package-lock.json',
        'lifecycle_scripts_executed': False})


@dataclass(frozen=True)
class Member:
    source: Path
    destination: str
    component: str


def plan_inputs(settings):
    """Return a closed member plan plus stable source-to-package root mappings."""
    if settings.get('format') != 'clear-portable-inputs-v1':
        raise ValueError('Unknown portable input map format')
    settings.setdefault('pdf_table_tool', str(Path(settings['program']).parent / 'ocr/build/public-quality-20260916/tool/site-packages/pdfplumber'))
    settings.setdefault('harness_receipt', str(Path(settings['harness_modules']).parent / 'install-receipt.json'))
    roots, planned, issues = [], {}, []

    def add_root(component, key, destination, *, source=False, selected=None, exclude=()):
        path = physical(settings[key])
        roots.append((path, destination))
        if not path.exists():
            issues.append({'component': component, 'path': str(path), 'reason': 'missing input'})
            return
        try:
            directory = path.is_dir()
            items = ((path / relative_member(name) for name in selected)
                     if selected is not None else members(path, source=source, exclude=exclude))
            for member in items:
                if selected is not None and not member.is_file():
                    issues.append({'component': component, 'path': str(member), 'reason': 'missing member'})
                    continue
                # Directory enumeration already checks every member's lstat.
                # Selected manifest members require their own ancestor check.
                if selected is not None:
                    member = physical(member, exists=True)
                relative = member.relative_to(path).as_posix() if directory else ''
                target = relative_member(destination + ('/' + relative if relative else ''))
                planned[target] = Member(member, target, component)
        except (OSError, ValueError) as error:
            issues.append({'component': component, 'path': str(path), 'reason': str(error)})

    add_root('professional-runtimes-models', 'program', 'assets/program')
    source = physical(settings['source'])
    settings.setdefault('deployment_guide', str(source.parents[1] / 'docs/Clear离线部署与迁移.md'))
    add_root('deployment-guide', 'deployment_guide', 'tools/docs/deployment.md')
    # Remove sealed native sources/configs and inject the current authored tree.
    for family in ('ocr', 'classification'):
        for name in list(planned):
            prefix = f'assets/program/bundles/{family}/'
            if name.startswith(prefix + 'src/') and not name.endswith('/platform_weights.py'):
                del planned[name]
        temporary = dict(settings)
        settings['current_' + family] = str(source / family)
        for folder in ('src', 'config') if family == 'ocr' else ('src', 'configs', 'schemas'):
            path = source / family / folder
            if path.exists():
                settings['overlay'] = str(path)
                add_root(f'{family}-current-source', 'overlay', f'assets/program/bundles/{family}/{folder}', source=True)
        settings.clear()
        settings.update(temporary)
    # App sources include the unified runtime/CLI and current API/worker modules.
    for family in ('platform', 'ocr', 'classification'):
        temporary = dict(settings)
        settings['app_source'] = str(source / family)
        add_root(f'{family}-source', 'app_source', 'app/' + family, source=True)
        settings.clear()
        settings.update(temporary)
    add_root('documents', 'documents', 'runtimes/documents')
    add_root('mineru-runtime', 'mineru_runtime', 'runtimes/mineru-pro')
    add_root('mineru-model', 'mineru_model', 'assets/mineru/MinerU2.5-Pro-2605-1.2B')
    add_root('libreoffice', 'libreoffice', 'tools/libreoffice')
    add_root('node', 'node', 'tools/node')
    add_root('agent-python-layer', 'agent_layer', 'runtimes/agent')
    add_root('harness-npm-lock', 'harness_modules', 'app/platform/agent_harness/node_modules')
    add_root('harness-npm-lock', 'harness_receipt', 'manifests/harness-install.json')
    add_root('multiuser-python-layer', 'task_layer', 'runtimes/task-layer')
    # pgAdmin/StackBuilder are separate applications, including another Python
    # version; the Clear server needs only PostgreSQL's bin/lib/share/doc.
    add_root('postgresql', 'postgres', 'tools/postgresql', exclude={'pgAdmin 4', 'StackBuilder'})
    add_root('pdf-table-tool', 'pdf_table_tool', 'app/ocr/resources/pdf-tables/site-packages/pdfplumber')
    aigc = physical(settings['aigc'])
    if (aigc / 'aigc-inventory.json').is_file():
        declaration = json.loads((aigc / 'aigc-inventory.json').read_text('utf-8-sig'))
        selected = {'aigc-inventory.json', 'aigc-licenses.json'}
        for row in declaration.get('engines', {}).values():
            selected.update(row['resources'])
        licenses = json.loads((aigc / 'aigc-licenses.json').read_text('utf-8-sig'))
        selected.update(row['license_file'] for row in licenses['assets'] if row.get('license_file'))
        # Source declarations already close the dependencies/python import tree.
        add_root('aigc-assets', 'aigc', 'assets/aigc', selected=sorted(selected))
        temporary = dict(settings)
        settings['aigc_runtime'] = str(aigc / 'dependencies/runtime')
        add_root('aigc-runtime', 'aigc_runtime', 'runtimes/aigc')
        roots.append((aigc / 'dependencies/runtime', 'runtimes/aigc'))
        settings.clear()
        settings.update(temporary)
    else:
        issues.append({'component': 'aigc-assets', 'reason': 'aigc-inventory.json missing'})
    safire = physical(settings['safire'])
    safire_names = ('scripts', 'models', 'dependencies', 'upstream')
    for folder in safire_names:
        temporary = dict(settings)
        settings['safire_component'] = str(safire / folder)
        add_root('safire-' + folder, 'safire_component', 'assets/safire/' + folder)
        settings.clear()
        settings.update(temporary)
    required_receipts = ['evidence/safire.pth.download.json', 'evidence/sam_vit_b_01ec64.pth.download.json',
        'evidence/source-integrity.json', 'evidence/upstream.json', 'evidence/dependencies-install.json',
        'evidence/SAFIRE-ec2af6af8c789fb6359493a3e352f89c6fd8107f.zip.download.json']
    add_root('safire-provenance', 'safire', 'assets/safire', selected=required_receipts)
    for family, destination in {
        'portal': 'app/platform/apps/portal/dist', 'ocr': 'app/ocr/frontend/dist',
        'classification': 'app/classification/apps/web/dist',
    }.items():
        temporary = dict(settings)
        settings['frontend_component'] = settings['frontend'][family]
        add_root(f'{family}-frontend', 'frontend_component', destination)
        if not (Path(settings['frontend_component']) / 'index.html').is_file():
            issues.append({'component': f'{family}-frontend', 'reason': 'current production build missing'})
        settings.clear()
        settings.update(temporary)
    # Old relative ._pth source imports resolved beside the historical release.
    legacy_release = physical(settings['program']).parent
    roots.extend((legacy_release / family, 'app/' + family)
                 for family in ('platform', 'ocr', 'classification'))
    roots.extend((source / family, 'app/' + family)
                 for family in ('platform', 'ocr', 'classification'))
    for name, destination in RESOURCES.items():
        if name == 'mineru_manifest':  # generated from the rebased import roots
            continue
        if destination not in planned and not any(member.startswith(destination.rstrip('/') + '/') for member in planned):
            issues.append({'component': name, 'destination': destination,
                           'reason': 'required installed resource is missing from the closed copy plan'})
    return list(sorted(planned.values(), key=lambda row: row.destination)), roots, issues


def mapped_path(path, mappings):
    """Map an old import by its most specific registered physical source root."""
    path = Path(path).resolve()
    for source, target in sorted(mappings, key=lambda row: len(row[0].parts), reverse=True):
        if path.is_relative_to(source):
            return PurePosixPath(target) / path.relative_to(source).as_posix()
    raise ValueError('Embedded Python import is outside registered inputs: ' + str(path))


def rebase_pth(member, output, mappings):
    lines = member.source.read_text('utf-8-sig').splitlines()
    rebased = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line == 'import site':
            continue
        if line.startswith('import '):
            raise ValueError('Embedded Python configuration contains executable code')
        if line in {'python312.zip', '.'}:
            rebased.append(line)
            continue
        requested = Path(line) if Path(line).is_absolute() else member.source.parent / line
        mapped = mapped_path(requested, mappings)
        rebased.append(Path(os.path.relpath(output / str(mapped), (output / member.destination).parent)).as_posix())
    platform = output / 'app/platform'
    for folder in (platform, platform / 'src'):
        relative = Path(os.path.relpath(folder, (output / member.destination).parent)).as_posix()
        if relative not in rebased:
            rebased.append(relative)
    if rebased[:2] != ['python312.zip', '.']:
        raise ValueError('Embedded Python configuration must start with python312.zip and .')
    return ('\n'.join([*rebased, 'import site', ''])).encode('utf-8')


def source_provenance(settings):
    """Check legacy per-file pins without requiring the missing candidate metadata."""
    pins = {}
    origin = json.loads((physical(settings['source']) / 'platform/config/runtime-locks/portable-inputs.json').read_text('utf-8-sig'))
    if origin.get('format') != 'clear-portable-origin-lock-v1' or origin['node']['archive_sha256'] != NODE_SHA256:
        raise ValueError('Portable origin lock is unavailable or disagrees with the fixed Node identity')
    release = physical(settings['program']).parent
    path = release / 'package-files.json'
    if path.is_file():
        original = json.loads(path.read_text('utf-8-sig'))
        for name, row in original.get('files', {}).items():
            pins[release / name] = row
    for family in ('ocr', 'classification'):
        root = physical(settings['program']) / 'bundles' / family
        declaration = json.loads((root / 'bundle.manifest.json').read_text('utf-8-sig'))
        for row in declaration['files']:
            pins[root / row['path']] = row
    aigc = physical(settings['aigc'])
    declaration = json.loads((aigc / 'aigc-inventory.json').read_text('utf-8-sig'))
    for engine in declaration['engines'].values():
        for name, row in engine['resources'].items():
            pins[aigc / relative_member(name)] = row
    notices = json.loads((aigc / 'aigc-licenses.json').read_text('utf-8-sig'))
    for row in notices['assets']:
        if row.get('license_file') and row.get('license_file_identity'):
            pins[aigc / relative_member(row['license_file'])] = row['license_file_identity']
    pins.update({Path(name): row for name, row in declaration['runtime_resources'].items()})
    for model in [physical(settings['mineru_model']), *(
            physical(settings['program']) / 'bundles/ocr/models').iterdir()]:
        if (model / 'source-manifest.json').is_file():
            provenance = json.loads((model / 'source-manifest.json').read_text('utf-8-sig'))
            for row in provenance.get('files', []):
                name = row.get('path')
                if name is None:
                    original = str(row['file']).replace('\\', '/')
                    marker = '/' + model.name + '/'
                    if marker not in original:
                        raise ValueError('Legacy model receipt has no identifiable model member')
                    name = original.split(marker, 1)[1]
                pins[model / relative_member(name)] = row
    libreoffice = physical(settings['libreoffice'])
    declaration = libreoffice.parent / 'libreoffice-manifest.json'
    if declaration.is_file():
        for name, sha in json.loads(declaration.read_text('utf-8-sig'))['files'].items():
            pins[libreoffice / relative_member(name)] = {'sha256': sha}
    safire = physical(settings['safire'])
    for name in ('safire.pth', 'sam_vit_b_01ec64.pth'):
        row = json.loads((safire / 'evidence' / (name + '.download.json')).read_text('utf-8-sig'))
        pins[safire / 'models' / name] = {'bytes': row['size_bytes'], 'sha256': row['sha256']}
    integrity = json.loads((safire / 'evidence/source-integrity.json').read_text('utf-8-sig'))
    if integrity['upstream_commit'] != 'ec2af6af8c789fb6359493a3e352f89c6fd8107f':
        raise ValueError('SAFIRE upstream revision differs from the locked product adapter')
    upstream = safire / 'upstream/SAFIRE-ec2af6af8c789fb6359493a3e352f89c6fd8107f'
    for row in integrity['files']:
        pins[upstream / relative_member(row['path'])] = row
    archive_receipt = json.loads((safire / 'evidence/SAFIRE-ec2af6af8c789fb6359493a3e352f89c6fd8107f.zip.download.json').read_text('utf-8-sig'))
    pins[upstream.with_suffix('.zip')] = {'bytes': archive_receipt['size_bytes'], 'sha256': archive_receipt['sha256']}
    archive = physical(settings['node_archive'], exists=True)
    if digest(archive) != NODE_SHA256:
        raise ValueError('Node archive differs from pinned official SHA256')
    with zipfile.ZipFile(archive) as bundle:
        for name in ('node.exe', 'LICENSE'):
            with bundle.open(f'node-v{NODE_VERSION}-win-x64/' + name) as stream:
                pins[physical(settings['node']) / name] = {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}
    review = json.loads((physical(settings['source']) / 'ocr/config/multimodal-review.json').read_text('utf-8-sig'))
    for profile in review.get('profiles', {}).values():
        for key in ('model', 'projector', 'license_asset'):
            row = profile[key]
            path = physical(settings['program']) / 'bundles/ocr' / relative_member(row['path'])
            if path.is_file():
                pins[path] = row
    table = json.loads((physical(settings['source']) / 'ocr/config/pdf-table-tool.json').read_text('utf-8-sig'))
    prefix = 'build/public-quality-20260916/tool/site-packages/pdfplumber/'
    for name, sha in table['files'].items():
        if not name.startswith(prefix):
            raise ValueError('Unexpected historical PDF table source member')
        pins[physical(settings['pdf_table_tool']) / relative_member(name.removeprefix(prefix))] = {'sha256': sha}
    harness = physical(settings['source']) / 'platform/agent_harness'
    lock = json.loads((harness / 'package-lock.json').read_text('utf-8-sig'))
    receipt = json.loads(physical(settings['harness_receipt'], exists=True).read_text('utf-8-sig'))
    if (receipt.get('format') != 'clear-npm-ci-receipt-v1'
            or receipt.get('package_lock_sha256') != digest(harness / 'package-lock.json')
            or receipt.get('node_version') != NODE_VERSION
            or receipt.get('lifecycle_scripts_executed') is not False):
        raise ValueError('Harness input must have a fresh, matching lock-verified npm ci receipt')
    modules = physical(settings['harness_modules'])
    for relative, row in lock['packages'].items():
        if not relative:
            continue
        if not relative.startswith('node_modules/') or row.get('link') or not row.get('integrity'):
            raise ValueError('Harness lock has an unclosed dependency: ' + relative)
        package = modules / relative.removeprefix('node_modules/') / 'package.json'
        if not package.is_file() and row.get('optional'):
            continue
        if not package.is_file() or json.loads(package.read_text('utf-8-sig'))['version'] != row['version']:
            raise ValueError('Harness installed dependency differs from npm lock: ' + relative)
    postgres = physical(settings['postgres']) / 'bin/postgres.exe'
    version = subprocess.check_output([str(postgres), '--version'], cwd=postgres.parent,
        text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip()
    if not re.fullmatch(r'postgres \(PostgreSQL\) 17\.11', version):
        raise ValueError('PostgreSQL distribution does not match locked version 17.11: ' + version)
    pg_root = physical(settings['postgres'])
    pg_archive = physical(settings.get('postgres_archive') or pg_root.parent.parent / 'postgresql-17.11-5.zip', exists=True)
    if digest(pg_archive) != origin['postgresql']['archive_sha256']:
        raise ValueError('PostgreSQL official ZIP differs from the registered fixed input')
    pins.update(archive_members(pg_archive, pg_root, prefix='pgsql/',
        excluded_roots={'pgAdmin 4', 'StackBuilder'}))
    agent = physical(settings['agent_layer'])
    receipt_path = agent / 'layer-receipt.json'
    if digest(receipt_path) != origin['agent_layer_receipt_sha256']:
        raise ValueError('Qualified Agent Python layer receipt changed')
    for name, sha in json.loads(receipt_path.read_text('utf-8-sig'))['files'].items():
        pins[agent / relative_member(name)] = {'sha256': sha}
    task_layer = physical(settings['task_layer'])
    wheels = physical(settings.get('task_wheelhouse') or task_layer.parent / 'wheelhouse')
    for name, expected in origin['task_wheels'].items():
        wheel = physical(wheels / relative_member(name), exists=True)
        if identity(wheel) != expected:
            raise ValueError('Task Python wheel differs from the authored origin lock: ' + name)
        pins.update(archive_members(wheel, task_layer, wheel=True))
    return pins


def archive_members(archive, root, *, prefix='', excluded_roots=(), wheel=False):
    """Use a pinned archive as the immutable source of expanded member hashes."""
    result = {}
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            if item.is_dir() or not item.filename.startswith(prefix):
                continue
            relative = item.filename[len(prefix):]
            relative_member(relative)
            if relative.split('/')[0] in excluded_roots:
                continue
            if wheel and '.data/' in relative:
                parts = relative.split('/')
                if len(parts) < 3 or parts[1] not in {'purelib', 'platlib'}:
                    continue
                relative = '/'.join(parts[2:])
            # pip regenerates RECORD with generated launchers/bytecode entries;
            # it is not importable code and is covered by the output manifest.
            if wheel and relative.endswith('.dist-info/RECORD'):
                continue
            if '__pycache__' in relative.split('/') or relative.endswith(('.pyc', '.pyo')):
                continue
            with bundle.open(item) as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            result[root / relative] = {'bytes': item.file_size, 'sha256': sha}
    return result


def optional_profiles(settings):
    review = json.loads((physical(settings['source']) / 'ocr/config/multimodal-review.json').read_text('utf-8-sig'))
    root = physical(settings['program']) / 'bundles/ocr'
    result = []
    for name, profile in review.get('profiles', {}).items():
        missing = [profile[key]['path'] for key in ('model', 'projector', 'license_asset')
                   if not (root / profile[key]['path']).is_file()]
        if missing:
            result.append({'id': name, 'status': 'unavailable_not_selectable', 'missing': missing,
                'scope': 'Optional profile is disabled by the product when these assets are unavailable'})
    return result


def source_snapshot(settings, rows):
    source = physical(settings['source'])
    try:
        revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'],
            text=True, encoding='utf-8', stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=normal'],
            text=True, encoding='utf-8', stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    content = {row['destination']: {key: row[key] for key in ('bytes', 'sha256')}
               for row in rows if row['destination'].startswith('app/') and row['component'].endswith(('-source', '-current-source', '-frontend'))}
    return {'format': 'clear-portable-source-snapshot-v1', 'git_commit': revision, 'dirty': dirty,
        'authored_content_sha256': hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'authored_file_count': len(content), 'local_source_paths_distributed': False}


def portable_pdf_tables(output, settings):
    value = json.loads((physical(settings['source']) / 'ocr/config/pdf-table-tool.json').read_text('utf-8-sig'))
    prefix = 'build/public-quality-20260916/tool/site-packages'
    stable = 'resources/pdf-tables/site-packages'
    if value['source'] != prefix or any(not key.startswith(prefix + '/') for key in value['files']):
        raise ValueError('Unknown PDF table source layout; refusing an untyped string replacement')
    value['source'] = stable
    value['files'] = {stable + key.removeprefix(prefix): sha for key, sha in value['files'].items()}
    save(output / 'app/ocr/config/pdf-table-tool.json', value)
    save(output / 'assets/program/bundles/ocr/config/pdf-table-tool.json', value)


def write_readme(output):
    (output / 'README.md').write_text('''# Clear 离线目录

完整保留本目录结构。运行不需要从 GitHub、pip、npm 或模型服务下载文件。
本目录是独立候选；复制成功不等于全部工具或目标 GPU 已通过资格检查。
具体资源范围及未安装的可选模型见 `manifests/resource-scope.json`；
源码身份见 `manifests/source_snapshot.json`；逐文件内容身份见 `manifests/package.json`。
用户选用云端 Agent 或视觉模型时，需要另行配置服务地址和密钥。
完整部署、迁移和回退步骤见 `tools/docs/deployment.md`。

在 Windows x64 普通本地盘解压/复制；程序目录可只读，DATA 应选择可写目录。
下列 F 盘仅为本机示例，目标机可替换为其他普通本地盘。

```powershell
.\\Clear.cmd init --mode single --data F:\\ClearData
.\\Clear.cmd verify --config F:\\ClearData\\clear.install.json
.\\Clear.cmd doctor --config F:\\ClearData\\clear.install.json
.\\Clear.cmd qualify --config F:\\ClearData\\clear.install.json
.\\Clear.cmd start --config F:\\ClearData\\clear.install.json
.\\Clear.cmd status --config F:\\ClearData\\clear.install.json
.\\Clear.cmd stop --all --config F:\\ClearData\\clear.install.json
```

多人模式在同一 Windows 服务器运行门户、私有 worker、PostgreSQL 及业务文件。
例如 `init --mode multi --data F:\\ClearData --transport lan-http --listen 192.168.1.8`。
门户默认 8889；私有 worker 和 PostgreSQL 始终只绑定回环地址。
局域网 HTTP 明文传输登录和业务内容；如需要加密，可显式配置 HTTPS 证书。
管理员在初始化时创建；凭据由当前运行服务的 Windows 账户重新加密，包内没有通用密码。

搬迁同一程序包前停止全部服务，使用 `rebind --package-root <新目录> --config <原配置>`
重新生成本机路径绑定和服务 token，再执行 verify/doctor/qualify。
业务数据迁移使用 backup/restore；不要手工全局替换业务 JSON 中的路径，
不要复制旧 PID、GPU 租约、目标机资格或跨账户 DPAPI 密文作为可复用状态。

GPU 驱动及所选引擎的硬件要求需在目标机核实。跨机器、跨盘符、不同 GPU、
不同 Windows 账户的实机验收由随包验收脚本执行；没有运行的检查不视为通过。
''', encoding='utf-8')


def refresh_sources(settings, work, output):
    """Refresh authored payload without rereading/copying 47 GB of fixed assets.

    The previous expected hashes are preserved for unchanged assets, never
    recomputed from potentially changed bytes. The independent verify command
    must still read and verify all of them before runtime admission.
    """
    work, output = physical(work), physical(output, exists=True)
    environment(work)
    manifest = json.loads((output / 'manifests/package.json').read_text('utf-8-sig'))
    if manifest.get('format') != 'clear-portable-package-v1':
        raise ValueError('Source refresh requires an owned portable package manifest')
    # Seed expected identities rather than calling this operation a verification.
    # A same-size asset corruption retains its original expected hash and will
    # therefore fail the separate content verifier.
    actual_members = {path.relative_to(output).as_posix(): path for path in members(output)}
    for name, expected in manifest['files'].items():
        relative_member(name)
        path = actual_members.get(name)
        if path is None:
            raise ValueError('Unchanged candidate member is missing: ' + name)
        info = path.stat()
        if info.st_size != expected['bytes']:
            raise ValueError('Unchanged candidate member size changed: ' + name)
        _HASH_CACHE[(str(path), info.st_size, info.st_mtime_ns)] = expected
    planned, mappings, issues = plan_inputs(settings)
    if issues:
        save(work / 'refresh-report.json', {'status': 'blocked', 'missing': issues})
        raise ValueError('Input map is incomplete; see refresh-report.json')
    selected = [row for row in planned if row.component.endswith(('-source', '-current-source', '-frontend', '-tool'))
                or row.component in {'safire-provenance', 'harness-npm-lock', 'deployment-guide'}]
    names = {row.destination for row in selected}
    prefixes = ['app/platform/', 'app/ocr/', 'app/classification/',
        'assets/program/bundles/ocr/src/', 'assets/program/bundles/ocr/config/',
        'assets/program/bundles/classification/src/', 'assets/program/bundles/classification/configs/',
        'assets/program/bundles/classification/schemas/']
    for name in manifest['files']:
        if (any(name.startswith(prefix) for prefix in prefixes) and name not in names
                and not name.endswith('/platform_weights.py')):
            physical(output / name).unlink(missing_ok=True)
    rows = input_lock(selected, pins=source_provenance(settings))
    for member, row in zip(selected, rows):
        target = physical(output / member.destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(member.source, target)
        if identity(target) != {key: row[key] for key in ('bytes', 'sha256')}:
            raise ValueError('Refreshed authored payload changed during copy: ' + member.destination)
    save(output / 'manifests/resources.json', {'format': 'clear-portable-resources-v1', 'resources': RESOURCES})
    write_readme(output)
    portable_pdf_tables(output, settings)
    seal_native(output)
    portable_aigc(output, settings, mappings)
    portable_mineru(output)
    save(output / 'manifests/source_snapshot.json', source_snapshot(settings, rows))
    component_path = output / 'manifests/components.json'
    if component_path.exists():
        component_manifest = json.loads(component_path.read_text('utf-8-sig'))
        changed = {}
        for row in rows:
            changed.setdefault(row['component'], {})[row['destination']] = {key: row[key] for key in ('bytes', 'sha256')}
        for component in component_manifest['components']:
            files = changed.get(component['id'])
            if files is not None:
                component.update(version='sha256:' + hashlib.sha256(json.dumps(files, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest(), file_count=len(files),
                    bytes=sum(row['bytes'] for row in files.values()))
        save(component_path, component_manifest)
    save(output / 'manifests/resource-scope.json', {'format': 'clear-portable-resource-scope-v1',
        'scope': 'All currently installed/selectable product routes; no research-only alternatives',
        'optional_unavailable': optional_profiles(settings)})
    result = seal(output)
    report = {'status': 'sources_refreshed_unqualified', 'output': str(output),
        'authored_members': len(selected), 'files': result['file_count'], 'bytes': result['bytes'],
        'unchanged_asset_hashes': 'Prior expected hashes preserved; independent verify still required'}
    save(work / 'refresh-report.json', report)
    return report


def input_lock(planned, *, pins=None, workers=8):
    pins = pins or {}
    def row(member):
        actual = identity(member.source)
        expected = pins.get(member.source)
        if expected and (actual['sha256'] != expected['sha256'] or
                expected.get('bytes', expected.get('size', actual['bytes'])) != actual['bytes']):
            raise ValueError('Historical content pin mismatch: ' + str(member.source))
        return {'source': str(member.source), 'destination': member.destination,
                'component': member.component, **actual}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='clear-build-hash') as pool:
        return list(pool.map(row, planned))


def stream_member(member, target, pins, output, mappings):
    """Read source bytes once, checking pins while writing the owned candidate.

    The saved expected output hash comes from this byte stream. Disk readback is
    intentionally left to the independent package verifier, so a damaged target
    can never be made authoritative by recomputing a new expected hash from it.
    """
    source = member.source
    before = source.stat()
    expected = pins.get(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    if source.suffix == '._pth':
        raw = source.read_bytes()
        hasher.update(raw)
        content = rebase_pth(member, output, mappings)
        target.write_bytes(content)
        output_identity = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    else:
        with source.open('rb') as stream, target.open('wb') as destination:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
                destination.write(chunk)
        output_identity = {'bytes': before.st_size, 'sha256': hasher.hexdigest()}
    after = source.stat()
    actual = {'bytes': before.st_size, 'sha256': hasher.hexdigest()}
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Input changed during streaming copy: ' + str(source))
    if expected and (actual['sha256'] != expected['sha256'] or
            expected.get('bytes', expected.get('size', actual['bytes'])) != actual['bytes']):
        raise ValueError('Historical content pin mismatch: ' + str(source))
    info = target.stat()
    if info.st_size != output_identity['bytes']:
        raise ValueError('Copied target has an unexpected size: ' + str(target))
    _HASH_CACHE[(str(target), info.st_size, info.st_mtime_ns)] = output_identity
    return {'source': str(source), 'destination': member.destination, 'component': member.component,
        'source_mtime_ns': before.st_mtime_ns, **actual}


def resume_member(member, target, prior, pins, source_info, target_info):
    if not (prior is not None and member.source.suffix != '._pth' and target_info is not None
            and prior.get('source') == str(member.source)
            and prior.get('source_mtime_ns') == source_info.st_mtime_ns
            and prior.get('bytes') == source_info.st_size and target_info.st_size == prior['bytes']):
        return None
    expected = pins.get(member.source)
    if expected and (prior['sha256'] != expected['sha256'] or
            expected.get('bytes', expected.get('size', prior['bytes'])) != prior['bytes']):
        raise ValueError('Checkpoint content differs from a fixed source pin: ' + member.destination)
    _HASH_CACHE[(str(target), target_info.st_size, target_info.st_mtime_ns)] = {key: prior[key] for key in ('bytes', 'sha256')}
    return dict(prior)


def portable_aigc(output, settings, mappings):
    source = physical(settings['aigc']) / 'aigc-inventory.json'
    value = json.loads(source.read_text('utf-8-sig'))
    root = output / 'assets/aigc'
    runtime = output / str(mapped_path(value['runtime'], mappings))
    value['format'] = 'clear-aigc-assets-v2'
    value['runtime_base'] = 'asset-root'
    value['runtime'] = Path(os.path.relpath(runtime, root)).as_posix()
    for engine in value['engines'].values():
        if engine.get('runtime'):
            engine['runtime'] = Path(os.path.relpath(output / str(mapped_path(engine['runtime'], mappings)), root)).as_posix()
    lines = (runtime.parent / 'python312._pth').read_text('utf-8').splitlines()
    closed = {runtime.parent.resolve(), *[(runtime.parent / line).resolve() for line in lines[2:-1]]}
    for directory in closed:
        if not directory.is_relative_to(output.resolve()):
            raise ValueError('Rebased AIGC import root escapes package')
    value['runtime_closed_directories'] = sorted(Path(os.path.relpath(path, root)).as_posix() for path in closed)
    value['runtime_resources'] = {}
    for directory in sorted(closed):
        for path in members(directory):
            value['runtime_resources'][Path(os.path.relpath(path, root)).as_posix()] = identity(path)
    save(root / 'aigc-inventory.json', value)


def portable_mineru(output):
    runtime = output / 'runtimes/mineru-pro'
    lines = [line.strip() for line in (runtime / 'python312._pth').read_text('utf-8').splitlines()
             if line.strip() and not line.startswith('#')]
    if lines[:2] != ['python312.zip', '.'] or lines[-1] != 'import site':
        raise ValueError('MinerU embedded imports are not explicit')
    roots = {runtime.resolve(), *[(runtime / line).resolve() for line in lines[2:-1]]}
    sources = []
    for root in sorted(roots):
        if not root.is_relative_to(output.resolve()) or not root.is_dir():
            raise ValueError('MinerU import root is outside the offline package')
        files = {path.relative_to(root).as_posix(): identity(path) for path in members(root)
                 if path.suffix.lower() in {'.dll', '.pyd', '.exe'}}
        if files:
            sources.append({'root': Path(os.path.relpath(root, output / 'manifests')).as_posix(), 'files': files})
    save(output / 'manifests/mineru-runtime.json', {'schema_version': 2,
        'root_base': 'manifest', 'sources': sources})


def seal_native(output):
    for family in ('ocr', 'classification'):
        root = output / 'assets/program/bundles' / family
        files = [{'path': path.relative_to(root).as_posix(), **identity(path)}
                 for path in members(root) if path.name not in {'bundle.manifest.json', 'manifest.json'}]
        save(root / 'bundle.manifest.json', {'app_id': family, 'format': 'clear-native-current-source-v1', 'files': files})
        if family == 'ocr':
            save(root / 'manifest.json', {'schema_version': 1, 'files': files})


def seal(output):
    """Reseal after intentional source refresh; qualification remains separate."""
    manifest = output / 'manifests/package.json'
    old = json.loads(manifest.read_text('utf-8')) if manifest.exists() else {}
    files = {path.relative_to(output).as_posix(): identity(path)
             for path in members(output) if path != manifest}
    value = {'format': 'clear-portable-package-v1', 'status': 'unqualified', 'standalone': True,
        'created_at': old.get('created_at', datetime.now(timezone.utc).isoformat()),
        'resource_index': 'manifests/resources.json', 'files': files,
        'file_count': len(files), 'bytes': sum(row['bytes'] for row in files.values()),
        'dependency_downloads_required_at_runtime': False,
        'optional_network_features': 'User-configured cloud Agent/vision providers require their own service connection and credentials',
        'qualification': {'local_isolation': 'pending', 'all_engines_smoke': 'pending',
                          'independent_windows': 'pending', 'different_drive': 'pending',
                          'different_gpu': 'pending', 'different_windows_account': 'pending'}}
    save(manifest, value)
    return value


def package(settings, work, output, *, inventory_only=False, refresh=False, resume_lock=None):
    work, output = physical(work), physical(output)
    environment(work)
    planned, mappings, issues = plan_inputs(settings)
    def size(member):
        cached = _SCAN_STATS.get(str(member.source))
        return cached.st_size if cached is not None else member.source.stat().st_size
    bytes_needed = sum(size(member) for member in planned)
    try:
        unavailable = optional_profiles(settings)
    except (OSError, KeyError, ValueError) as error:
        unavailable = []
        issues.append({'component': 'review-profiles', 'reason': str(error)})
    inventory = {'format': 'clear-portable-input-report-v1', 'files': len(planned),
        'bytes': bytes_needed, 'missing': issues, 'required_resource_keys': RESOURCES,
        'optional_unavailable': unavailable,
        'status': 'blocked' if issues else 'inputs_discovered',
        'qualification': 'Not a functional or target-machine qualification'}
    save(work / 'input-report.json', inventory)
    if issues:
        raise ValueError('Portable input closure is incomplete; see ' + str(work / 'input-report.json'))
    if inventory_only:
        return inventory
    if not refresh and output.exists():
        raise FileExistsError('Output already exists; explicitly use --refresh for the owned candidate')
    reserve = 8 * 1024 ** 3
    # Reserve one full copy conservatively rather than stat every nonexistent
    # target on Windows. The candidate is never duplicated for relocation.
    actual_new = bytes_needed
    disk_parent = output.parent
    while not disk_parent.exists():
        disk_parent = disk_parent.parent
    if shutil.disk_usage(disk_parent).free < actual_new + reserve:
        raise ValueError('Insufficient F-drive space for one candidate plus 8 GiB working reserve')
    print('Checking existing source provenance...', flush=True)
    try:
        pins = source_provenance(settings)
    except (OSError, KeyError, ValueError) as error:
        save(work / 'input-report.json', {**inventory, 'status': 'blocked',
            'missing': [{'component': 'source-provenance', 'reason': str(error)}]})
        raise
    inventory['historical_pin_count'] = len(pins)
    lock_path = work / 'input-lock.json'
    record_path = physical(resume_lock, exists=True) if resume_lock else lock_path
    previous = json.loads(record_path.read_text('utf-8-sig')) if refresh and record_path.exists() else {}
    resumed = {row['destination']: row for row in previous.get('files', [])} if resume_lock else {}
    if output.exists():
        # Validate existing targets before any source refresh can write through
        # a newly added junction in the owned candidate.
        for _path in members(output):
            pass
        planned_names = {row.destination for row in planned}
        for old in previous.get('files', []):
            if old['destination'] not in planned_names:
                stale = physical(output / relative_member(old['destination']))
                if stale.is_file():
                    stale.unlink()
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, member in enumerate(planned):
        # The output root and every pre-existing descendant were checked before
        # copying; names were validated by relative_member during planning.
        target = output / member.destination
        prior = resumed.get(member.destination)
        source_info = _SCAN_STATS.get(str(member.source)) or member.source.stat()
        target_info = _SCAN_STATS.get(str(target))
        cached = resume_member(member, target, prior, pins, source_info, target_info)
        rows.append(cached if cached is not None else stream_member(member, target, pins, output, mappings))
        if index % 5000 == 0:
            print(f'Copied/verified {index}/{len(planned)} files', flush=True)
            save(work / 'input-lock.partial.json', {'format': 'clear-portable-input-lock-v1', 'files': rows})
    save(lock_path, {'format': 'clear-portable-input-lock-v1', 'files': rows})
    save(output / 'manifests/resources.json', {'format': 'clear-portable-resources-v1', 'resources': RESOURCES})
    write_readme(output)
    (output / 'Clear.cmd').write_text('@echo off\nsetlocal\n"%~dp0assets\\program\\runtimes\\platform\\python.exe" -I -B -X utf8 "%~dp0app\\platform\\scripts\\clear_portable.py" --package-root "%~dp0." %*\n', 'utf-8')
    portable_pdf_tables(output, settings)
    seal_native(output)
    portable_aigc(output, settings, mappings)
    portable_mineru(output)
    components = {}
    for row in rows:
        component = components.setdefault(row['component'], {'id': row['component'], 'files': {}})
        component['files'][row['destination']] = identity(output / row['destination'])
    for component in components.values():
        component['version'] = 'sha256:' + hashlib.sha256(json.dumps(component['files'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        component['file_count'] = len(component['files'])
        component['bytes'] = sum(row['bytes'] for row in component['files'].values())
        del component['files']
    save(output / 'manifests/components.json', {'format': 'clear-portable-components-v1',
        'components': list(components.values()), 'upstream_provenance': 'Original model/source/license receipts are retained beside their assets',
        'notices': {'node': {'version': NODE_VERSION, 'source': f'https://nodejs.org/dist/v{NODE_VERSION}/',
            'archive_sha256': NODE_SHA256, 'license': 'tools/node/LICENSE'},
            'postgresql': {'version': '17.11', 'license': 'tools/postgresql/server_license.txt',
                'source': 'https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64-binaries.zip',
                'archive_sha256': '80379b2c04d51c30225532e0ae04509899141e9957ed096fe749d7fd9df8f82f'},
            'harness': {'version': '0.2.1-alpha.4', 'lock': 'app/platform/agent_harness/package-lock.json'}}})
    save(output / 'manifests/source_snapshot.json', source_snapshot(settings, rows))
    save(output / 'manifests/resource-scope.json', {'format': 'clear-portable-resource-scope-v1',
        'scope': 'All currently installed/selectable product routes; no research-only alternatives',
        'optional_unavailable': inventory['optional_unavailable']})
    value = seal(output)
    save(work / 'input-report.json', {**inventory, 'status': 'copied_unqualified', 'output': str(output)})
    return {'output': str(output), 'files': value['file_count'], 'bytes': value['bytes'],
            'status': value['status'], 'missing': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--source-root', type=Path, help='Current authored Clear tree containing platform, ocr, classification (e.g. a clean checkout products/Clear)')
    parser.add_argument('--write-input-map', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--inventory-only', action='store_true')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--prepare-harness', action='store_true', help='Fresh npm ci from the fixed authored lock into F work dependencies; may fetch only lock-pinned npm tarballs')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--resume-lock', type=Path, help='Resume an interrupted owned copy using its frozen input-lock.partial.json; expected hashes are preserved and independent verification remains required')
    parser.add_argument('--refresh-sources-only', action='store_true',
        help='Refresh current sources in the existing owned candidate, retaining expected large-asset hashes; full verify remains required')
    args = parser.parse_args()
    settings = (json.loads(physical(args.inputs, exists=True).read_text('utf-8-sig'))
                if args.inputs else default_inputs(args.repository, args.work))
    if args.source_root:
        settings['source'] = str(physical(args.source_root, exists=True))
        settings['deployment_guide'] = str(Path(settings['source']).parents[1] / 'docs/Clear离线部署与迁移.md')
    environment(args.work)
    if args.write_input_map:
        save(args.write_input_map, settings)
    if args.fetch:
        fetch_missing(settings, args.work)
    if args.prepare_harness:
        prepare_harness(settings, args.work)
    output = args.output or args.work / 'delivery/Clear'
    result = (refresh_sources(settings, args.work, output) if args.refresh_sources_only else
        package(settings, args.work, output, inventory_only=args.inventory_only,
                refresh=args.refresh, resume_lock=args.resume_lock))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
