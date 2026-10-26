"""Seal Clear 0.13.0 from local inputs, with an offline Harness dependency closure.

No downloader or package installer is invoked. The legacy program remains an
explicit, hash-checked shared dependency; live DATA/registration are untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

# The documented -I CLI removes the script directory from import search.
# Resolve companion verification modules from this exact source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


PRODUCT_VERSION = '0.13.0'
HARNESS_VERSION = '0.2.1-alpha.4'
HARNESS_COMMIT = '86a402200d95fecef4529d969836f1fe13c6cb0d'
NODE_VERSION = '24.22.0'
NODE_ARCHIVE_SHA256 = 'f9683a19877e12a9b167305c8db71040d812876359de21f0d2e97c3f321f80c3'
DEFAULT_WORK = Path('F:/OCR/output/agent-harness-upgrade-20261015')
DEFAULT_RELEASE = Path('F:/OCR/release/Clear/versions/0.10.0')
SKIP = {'__pycache__', '.pytest_cache', '.git', '.cache', '.bin'}


def local_f(value, *, exists=False):
    path = Path(value).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('All package inputs/outputs must stay on local F: ' + str(path))
    if any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
        raise ValueError('Linked package path is not permitted: ' + str(path))
    if exists and not path.exists():
        raise FileNotFoundError(path)
    return path.resolve()


def sha(path):
    with local_f(path, exists=True).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    local_f(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def environment(work):
    """Set every packaging subprocess/cache location before any subprocess."""
    work = local_f(work)
    values = {'TEMP': work / 'temp', 'TMP': work / 'temp', 'TMPDIR': work / 'temp',
        'APPDATA': work / 'cache/appdata', 'LOCALAPPDATA': work / 'cache/localappdata',
        'XDG_CACHE_HOME': work / 'cache', 'XDG_CONFIG_HOME': work / 'cache/config',
        'XDG_DATA_HOME': work / 'cache/data', 'PIP_CACHE_DIR': work / 'cache/pip',
        'npm_config_cache': work / 'cache/npm', 'DSH_HOME': work / 'cache/dsh',
        'DSH_RESOURCE_CACHE': work / 'cache/dsh'}
    for value in set(values.values()):
        local_f(value).mkdir(parents=True, exist_ok=True)
    os.environ.update({key: str(value) for key, value in values.items()})
    for name in ('NODE_OPTIONS', 'NODE_PATH'):
        os.environ.pop(name, None)
    os.environ.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                      DSH_PRIMARY_RUNTIME='', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')


def members(root, *, exclude_node_modules=False):
    root = local_f(root, exists=True)
    if root.is_file():
        yield root
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in dirs:
            local_f(parent / name)
        dirs[:] = sorted(name for name in dirs if name not in SKIP and
                         not (exclude_node_modules and name == 'node_modules'))
        for name in sorted(files):
            path = local_f(parent / name)
            if name.endswith(('.pyc', '.pyo')) or name == 'runtime-local.json':
                continue
            yield path


def node_input(work):
    dependencies = local_f(work / 'dependencies', exists=True)
    archive = local_f(dependencies / f'node-v{NODE_VERSION}-win-x64.zip', exists=True)
    sums = local_f(dependencies / 'SHASUMS256.txt', exists=True)
    lines = [line.split() for line in sums.read_text('utf-8').splitlines() if line.split() and line.split()[-1] == archive.name]
    if len(lines) != 1 or lines[0][0] != NODE_ARCHIVE_SHA256 or sha(archive) != NODE_ARCHIVE_SHA256:
        raise ValueError('Node archive does not match the pinned official SHA256')
    root = local_f(dependencies / f'node-v{NODE_VERSION}-win-x64', exists=True)
    with zipfile.ZipFile(archive) as bundle:
        for name in ('node.exe', 'LICENSE'):
            with bundle.open(f'node-v{NODE_VERSION}-win-x64/{name}') as stream:
                expected = hashlib.file_digest(stream, 'sha256').hexdigest()
            if sha(root / name) != expected:
                raise ValueError('Extracted Node input changed: ' + name)
    actual = subprocess.check_output([str(root / 'node.exe'), '--version'], cwd=root, text=True).strip()
    if actual != 'v' + NODE_VERSION:
        raise ValueError('Incorrect Node runtime version')
    return root, {'version': NODE_VERSION, 'archive': archive.name, 'archive_sha256': NODE_ARCHIVE_SHA256,
                  'checksums_sha256': sha(sums), 'node_sha256': sha(root / 'node.exe'),
                  'source': f'https://nodejs.org/dist/v{NODE_VERSION}/SHASUMS256.txt'}


def javascript_input(root):
    root = local_f(root, exists=True)
    package = json.loads((root / 'package.json').read_text('utf-8'))
    lock = json.loads((root / 'package-lock.json').read_text('utf-8'))
    if lock.get('lockfileVersion') != 3 or lock['packages']['']['dependencies'] != package['dependencies']:
        raise ValueError('Harness package declaration and npm lock disagree')
    if package.get('engines', {}).get('node') != NODE_VERSION:
        raise ValueError('Harness Node engine declaration is not pinned')
    entries, declared = [], set()
    for relative, entry in sorted(lock['packages'].items()):
        if not relative:
            continue
        path = local_f(root / relative)
        if not path.is_relative_to(root / 'node_modules') or entry.get('link') or not entry.get('integrity'):
            raise ValueError('Harness lock has an unclosed dependency: ' + relative)
        if not (path / 'package.json').is_file():
            if entry.get('optional'):
                continue
            raise ValueError('Installed Harness dependency missing: ' + relative)
        installed = json.loads((path / 'package.json').read_text('utf-8'))
        name, version = installed['name'], installed['version']
        if version != entry['version'] or (name.startswith('@deepseek-ai/dsh-') and version != HARNESS_VERSION):
            raise ValueError('Harness dependency version mismatch: ' + name)
        declared.add(path)
        notices = [file.relative_to(root).as_posix() for file in path.iterdir()
                   if file.is_file() and file.name.lower().startswith(('license', 'licence', 'copying', 'notice'))]
        entries.append({'name': name, 'version': version, 'path': relative, 'integrity': entry['integrity'],
                        'license': installed.get('license') or entry.get('license'), 'notice_files': notices})
    for manifest in (root / 'node_modules').rglob('package.json'):
        # Dependencies may include fixture package.json files inside their
        # own tests. Only npm package roots count toward the closure.
        parts = manifest.parent.relative_to(root).parts
        tail = parts[max(index for index, name in enumerate(parts) if name == 'node_modules') + 1:]
        if len(tail) == 1 or len(tail) == 2 and tail[0].startswith('@'):
            if local_f(manifest.parent) not in declared:
                raise ValueError('Installed package is outside the npm lock: ' + str(manifest.parent))
    return entries


def shared_worker_identity(program, shared_identity):
    """Pin worker imports and adjacent export provenance from the sealed declaration."""
    asset = local_f(program / 'bundles/classification', exists=True)
    declaration_path = local_f(asset / 'bundle.manifest.json', exists=True)
    if shared_identity.get(str(declaration_path)) != sha(declaration_path):
        raise ValueError('Classification asset declaration is not a pinned shared identity')
    declaration = json.loads(declaration_path.read_text('utf-8'))
    if declaration.get('app_id') != 'classification' or not isinstance(declaration.get('files'), list):
        raise ValueError('Invalid shared classification declaration')
    selected = [item for item in declaration['files'] if item['path'].startswith(('src/', 'schemas/', 'resources/third-party/'))
                or item['path'] == 'resources/weights/manifest.json']
    paths = {item['path'] for item in selected}
    required = {'src/image_platform/__init__.py', 'src/image_platform/settings.py', 'src/image_platform/cli.py',
                'src/image_platform/worker.py', 'src/image_platform/training.py', 'resources/third-party/APACHE-2.0.txt',
                'schemas/model-metadata.schema.json'}
    if not required <= paths or len(paths) != len(selected):
        raise ValueError('Shared classification worker sources or export provenance missing')
    result = {}
    for item in selected:
        path = local_f(asset / item['path'], exists=True)
        if not path.is_relative_to(asset) or sha(path) != item['sha256']:
            raise ValueError('Shared classification worker payload identity changed')
        result[str(path)] = item['sha256']
    for path in (asset / 'src').rglob('*.py'):
        if local_f(path).relative_to(asset).as_posix() not in paths:
            raise ValueError('Shared classification worker has an undeclared module')
    return result


def harness_compatibility(sealed):
    from scripts.product_compatibility import READER_FEATURES
    compatibility = json.loads(json.dumps(sealed['data_compatibility']))
    compatibility['reader_features'] = sorted(set(compatibility['reader_features']) | set(READER_FEATURES))
    formats = compatibility['records']['request_snapshot_formats']
    compatibility['records']['request_snapshot_formats'] = sorted(set(formats) | {'clear-harness-input-v1', 'clear-goal-interpretation-v1'})
    return compatibility


def package(source, sealed_release, work, output=None, *, check_inputs=False, product_version=PRODUCT_VERSION,
            ocr_source=None, classification_source=None, portal_dist=None):
    if not isinstance(product_version, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', product_version):
        raise ValueError('Product version must be a numeric major.minor.patch version')
    source, sealed_release, work = [local_f(path, exists=True) for path in (source, sealed_release, work)]
    native_sources = {'ocr': local_f(ocr_source, exists=True) if ocr_source else sealed_release / 'ocr',
                      'classification': local_f(classification_source, exists=True) if classification_source else sealed_release / 'classification'}
    portal_dist = local_f(portal_dist, exists=True) if portal_dist else source / 'apps/portal/dist'
    environment(work)
    program = local_f(sealed_release / 'program', exists=True)
    sealed = json.loads((sealed_release / 'candidate.json').read_text('utf-8'))
    node, node_receipt = node_input(work)
    npm_packages = javascript_input(source / 'agent_harness')
    local_f(portal_dist / 'index.html', exists=True)
    required = ['agent_harness/host.mjs', 'src/workbench_platform/agent/harness.py',
                'scripts/classification_worker_compat.py',
                'scripts/harness_candidate_launch.py', 'scripts/harness_candidate_smoke.mjs', 'scripts/harness_offline_guard.mjs']
    for name in required:
        local_f(source / name, exists=True)
    shared_identity = {}
    for relative, expected in sealed['shared_identity'].items():
        shared = local_f(sealed_release / relative, exists=True)
        if not shared.is_relative_to(program) or sha(shared) != expected:
            raise ValueError('Sealed shared program identity changed: ' + relative)
        shared_identity[str(shared)] = expected
    shared_identity.update(shared_worker_identity(program, shared_identity))
    from scripts.classification_worker_compat import validate as classification_store_compatibility
    classification_compatibility = classification_store_compatibility(native_sources['classification'], program / 'bundles/classification')
    compatibility_identity = {key: value for key, value in classification_compatibility.items()
                              if key not in {'api_source', 'store_path', 'adapter_path'}}
    if check_inputs:
        return {'checked': True, 'product_version': product_version, 'node': node_receipt,
                'locked_npm_packages': len(npm_packages), 'shared_identity_files': len(shared_identity),
                'portal_sha256': sha(portal_dist / 'index.html'), 'classification_store_compatibility': compatibility_identity,
                'network_requests': 0, 'data_changed': False}
    if output is None:
        raise ValueError('--output is required to package a candidate')
    output = local_f(output)
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output) or output.is_relative_to(sealed_release):
        raise ValueError('Use a new candidate directory outside the source and sealed release')
    sources = {}
    planned = []

    def plan_tree(origin, destination, *, no_node=False, sealed_prefix=None):
        origin = local_f(origin, exists=True)
        for member in members(origin, exclude_node_modules=no_node):
            suffix = member.relative_to(origin) if origin.is_dir() else Path()
            target = local_f(destination / suffix) if origin.is_dir() else local_f(destination)
            fingerprint = sha(member)
            if sealed_prefix is not None:
                key = (Path(sealed_prefix) / suffix).as_posix()
                if sealed['files'].get(key) != fingerprint:
                    raise ValueError('Copied sealed source changed: ' + key)
            planned.append((member, target, fingerprint))

    for name in ('src', 'packages', 'modules', 'config', 'scripts', 'apps/portal', 'docs/agent'):
        if name == 'apps/portal':
            # Keep the tested build as a single explicit input. Source and
            # dependency trees are still copied under their original identity.
            for part in ('src', 'public'):
                plan_tree(source / name / part, output / 'platform' / name / part, no_node=True)
            plan_tree(portal_dist, output / 'platform/apps/portal/dist')
            for file in (source / name).iterdir():
                if file.is_file() and file.suffix not in {'.tsbuildinfo'}:
                    plan_tree(file, output / 'platform' / name / file.name)
        else:
            plan_tree(source / name, output / 'platform' / name, no_node=True)
    for name in ('pyproject.toml', 'LICENSE', 'README.md'):
        if (source / name).is_file():
            plan_tree(source / name, output / 'platform' / name)
    plan_tree(source / 'agent_harness', output / 'platform/agent_harness')
    for family, names in (('ocr', ('src', 'config', 'frontend/dist', 'build/public-quality-20260916/tool/site-packages/pdfplumber')),
                          ('classification', ('src', 'configs', 'apps/web/dist'))):
        for name in names:
            origin = native_sources[family] if not name.startswith('build/') else sealed_release / family
            plan_tree(origin / name, output / family / name,
                      sealed_prefix=family + '/' + name if origin == sealed_release / family else None)
    plan_tree(sealed_release / 'dependencies/agent', output / 'dependencies/agent', sealed_prefix='dependencies/agent')
    for name in ('node.exe', 'LICENSE'):
        plan_tree(node / name, output / 'platform/agent_harness/node' / name)
    plan_tree(source / 'scripts/harness_candidate_launch.py', output / 'launch.py')
    total_bytes = sum(path.stat().st_size for path, _, _ in planned)
    disk_root = output.parent
    while not disk_root.exists():
        disk_root = disk_root.parent
    if shutil.disk_usage(disk_root).free < total_bytes + 256 * 1024 * 1024:
        raise ValueError('Insufficient F-drive space for candidate and verification reserve')
    output.mkdir(parents=True)
    for origin, target, fingerprint in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
        if sha(target) != fingerprint:
            raise ValueError('Copied candidate member changed: ' + str(target))
        sources[target.relative_to(output).as_posix()] = {'source': str(origin), 'sha256': fingerprint}
    # Detect source edits during the complete snapshot, including the portal.
    if any(sha(origin) != fingerprint for origin, _, fingerprint in planned):
        raise ValueError('An input changed during packaging; incomplete candidate retained for diagnosis')
    (output / '启动Clear.cmd').write_text('@echo off\n"' + str(program / 'runtimes/platform/python.exe') +
        '" -X utf8 -I -B -u "%~dp0launch.py" --enable-agent %*\n', encoding='utf-8')
    save(output / 'source-manifest.json', {'format': 'clear-harness-source-manifest-v1', 'files': sources})
    save(output / 'third-party-notices.json', {'format': 'clear-harness-notices-v1', 'harness': {'version': HARNESS_VERSION,
        'source_commit': HARNESS_COMMIT, 'repository': 'https://github.com/deepseek-ai/deepseek-harness', 'license': 'MIT'},
        'node': node_receipt, 'npm_packages': npm_packages,
        'shared_program': {'path': str(program), 'source_candidate_manifest_sha256': sha(sealed_release / 'candidate.json')}})
    (output / 'README.md').write_text(f'''# Clear {product_version} Harness 候选

本候选包含完整锁定的 Harness JavaScript 依赖与 Node 24.22.0，离线安装不下载依赖。
Python、原生服务和专业模型显式复用 candidate.json 的 shared_program；该 F 盘目录必须保留。
这是开发候选包，不表示源码已经定版或正式发布，也不会自动登记或启动为当前程序。源码、前端构建及依赖的逐文件 SHA256 在 candidate.json；业务 DATA 由使用者显式指定。

以共享平台 Python 执行 launch.py --data F:\\明确工作区 --port 8920 --enable-agent。
--check-only 只核对发行和 DATA 兼容性；--offline-smoke --output F:\\新的证据目录 验证本地 Harness 调用和持久历史。
运行日志、缓存、临时文件均写入所选 DATA 的 F 盘目录，发行目录保持只读。
不依赖历史 product-entry.ps1 或 current.json；运行时使用上方显式 launch.py 命令并核对实际源码、前端、共享依赖和 DATA。
未来正式发布须另按用户确定的源码及交付范围准备；当前没有正式发布身份。
更换读取代码前保留启动配置、代码身份及一致性 DATA 备份；先核对 reader_features，不要直接让旧程序打开新格式数据。
''', encoding='utf-8')
    files = {path.relative_to(output).as_posix(): sha(path) for path in members(output)}
    compatibility = harness_compatibility(sealed)
    startup = list(sealed.get('startup_reader_features', [])) + ['agent-harness-v1', 'agent-multimodal-v1']
    manifest = {'format': 1, 'status': 'harness_candidate', 'product_version': product_version,
        'release_name': 'Harness 智能审计', 'ui_contract_version': '1.2.0',
        'created_at': datetime.now(timezone.utc).isoformat(), 'shared_program': str(program),
        'shared_assets_read_only': True, 'copies_models': False, 'shared_identity': shared_identity,
        'classification_store_compatibility': compatibility_identity,
        'data_compatibility': compatibility, 'startup_reader_features': startup,
        'frontend_roots': {'portal': 'platform/apps/portal/dist', 'ocr': 'ocr/frontend/dist', 'classification': 'classification/apps/web/dist'},
        'engine': {'name': 'deepseek-harness', 'version': HARNESS_VERSION, 'source_commit': HARNESS_COMMIT,
                   'node': node_receipt, 'lock_sha256': sha(output / 'platform/agent_harness/package-lock.json'),
                   'locked_npm_packages': len(npm_packages), 'install_network_requests': 0},
        'source_manifest': 'source-manifest.json', 'third_party_notices': 'third-party-notices.json', 'files': files}
    save(output / 'candidate.json', manifest)
    return {'candidate': str(output), 'product_version': product_version, 'files': len(files), 'bytes': total_bytes,
            'manifest_sha256': sha(output / 'candidate.json'), 'shared_program': str(program), 'network_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--sealed-release', type=Path, default=DEFAULT_RELEASE)
    parser.add_argument('--work', type=Path, default=DEFAULT_WORK)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check-inputs', action='store_true')
    parser.add_argument('--product-version', default=PRODUCT_VERSION)
    parser.add_argument('--ocr-source', type=Path)
    parser.add_argument('--classification-source', type=Path)
    parser.add_argument('--portal-dist', type=Path)
    print(json.dumps(package(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
