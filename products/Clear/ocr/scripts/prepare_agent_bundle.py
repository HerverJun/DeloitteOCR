"""Build a NEW experimental portable bundle; never alter the prior delivery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def require_fresh_frontend(root):
    """Fail closed when the web directory predates an input to its build."""
    built = root / 'frontend/dist/index.html'
    if not built.is_file():
        raise FileNotFoundError('Frontend dist/index.html is missing; build the frontend first')
    inputs = [path for path in (root / 'frontend').iterdir()
              if path.is_file() and path.suffix in {'.html', '.json', '.ts', '.js'}]
    for folder in ('frontend/src', 'frontend/public'):
        if (root / folder).is_dir():
            inputs.extend(path for path in (root / folder).rglob('*') if path.is_file())
    newer = [path for path in inputs if path.is_file() and path.stat().st_mtime_ns > built.stat().st_mtime_ns]
    if newer:
        raise ValueError('Frontend dist predates build input: ' + str(max(newer, key=lambda path: path.stat().st_mtime_ns)))


def safe_member(base, relative):
    """Reject manifest traversal and reparse points, including parent directories."""
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('Unsafe inherited path: ' + repr(relative))
    member = PurePosixPath(relative)
    if member.is_absolute() or any(part in ('', '.', '..') for part in member.parts):
        raise ValueError('Unsafe inherited path: ' + relative)
    path = base.joinpath(*member.parts)
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)() or (
                candidate.exists() and getattr(candidate.stat(follow_symlinks=False), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Reparse point in inherited path: ' + relative)
        if candidate == base:
            break
    if not path.resolve().is_relative_to(base) or not path.is_file():
        raise ValueError('Unsafe inherited path: ' + relative)
    return path


def hardlink_eligible(relative):
    parts = PurePosixPath(relative).parts
    return bool(parts and (parts[0] == 'models' or
                         len(parts) > 2 and parts[0] == 'runtimes' and parts[1] != 'service'))


def stage_inherited(base, staging, records, *, hardlink_assets=False):
    """Freeze inherited bytes, linking only explicitly eligible immutable assets."""
    linked = copied = linked_bytes = copied_bytes = 0
    last = time.monotonic()
    for index, record in enumerate(records):
        path = safe_member(base, record['path'])
        target = staging.joinpath(*PurePosixPath(record['path']).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Current repository licenses take precedence; old runtime licenses are
        # additionally retained when their paths do not overlap.
        if target.exists():
            continue
        if path.stat().st_size != record['bytes']:
            raise ValueError('Base asset size differs from its manifest: ' + record['path'])
        if hardlink_assets and hardlink_eligible(record['path']):
            if path.stat().st_dev != target.parent.stat().st_dev:
                raise ValueError('Hardlink assets require the same filesystem: ' + record['path'])
            if checksum(path) != record['sha256']:
                raise ValueError('Base asset differs from its manifest: ' + record['path'])
            os.link(path, target)
            if target.stat().st_ino != path.stat().st_ino or target.stat().st_dev != path.stat().st_dev:
                raise ValueError('Hardlink did not refer to the source asset: ' + record['path'])
            linked += 1
            linked_bytes += record['bytes']
        else:
            digest = hashlib.sha256()
            with path.open('rb') as source, target.open('xb') as destination:
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
                    digest.update(chunk)
                    destination.write(chunk)
            if target.stat().st_size != record['bytes'] or digest.hexdigest() != record['sha256']:
                raise ValueError('Base asset differs from its manifest: ' + record['path'])
            copied += 1
            copied_bytes += record['bytes']
        if time.monotonic() - last > 15:
            print(json.dumps({'verified_inherited_assets': index + 1, 'total': len(records),
                              'hardlinked': linked, 'copied': copied}), flush=True)
            last = time.monotonic()
    return {'hardlinked_files': linked, 'hardlinked_logical_bytes': linked_bytes,
            'copied_inherited_files': copied, 'copied_inherited_bytes': copied_bytes}


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-bundle', type=Path, required=True)
    parser.add_argument('--service-runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--hardlink-inherited-assets', action='store_true',
                        help='Same-filesystem staging only: hardlink manifest-verified models/ and runtimes/* except runtimes/service; mutable product files stay independent. The source and candidate then share these asset bytes until packaged into an independent ZIP.')
    args = parser.parse_args()
    base, service, output = args.base_bundle.resolve(), args.service_runtime.resolve(), args.output.resolve()
    require_fresh_frontend(ROOT)
    staging = output.with_name(output.name + '.staging')
    if output.exists() or staging.exists() or output.is_relative_to(base) or base.is_relative_to(output):
        raise ValueError('Destination must be a new directory outside the prior bundle')
    records = json.loads((base / 'manifest.json').read_text('utf-8'))['files']
    allowed = {'models', 'fonts', 'launcher', 'licenses', 'maintenance', 'locks', 'fixtures'}
    if any(not isinstance(r.get('path'), str) or not r['path'] for r in records):
        raise ValueError('Invalid inherited manifest path')
    inherited = [r for r in records if PurePosixPath(r['path']).parts[0] in allowed or
                 r['path'].startswith('runtimes/') and not r['path'].startswith('runtimes/service/') or r['path'] == 'LICENSE']
    if len({r['path'].casefold() for r in inherited}) != len(inherited):
        raise ValueError('Duplicate inherited manifest path')
    for record in inherited:
        safe_member(base, record['path'])
    output.parent.mkdir(parents=True, exist_ok=True)
    linked_budget = sum(r['bytes'] for r in inherited if hardlink_eligible(r['path'])) if args.hardlink_inherited_assets else 0
    if args.hardlink_inherited_assets and base.stat().st_dev != output.parent.stat().st_dev:
        raise ValueError('Hardlink staging must be on the same filesystem as the base bundle')
    if shutil.disk_usage(output.parent).free < sum(r['bytes'] for r in inherited) - linked_budget + 3 * 1024**3:
        raise OSError('Not enough space for candidate copies plus 3 GiB reserve')
    staging.mkdir()
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache')
    shutil.copytree(service, staging / 'runtimes/service', ignore=ignore)
    for origin, destination in [('src', 'app'), ('config', 'config'), ('frontend/dist', 'web'), ('docs', 'docs'), ('licenses', 'licenses'), ('scripts', 'tools')]:
        shutil.copytree(ROOT / origin, staging / destination, dirs_exist_ok=True, ignore=ignore)
    # Preserve the read-only evidence links used by the bundled manuals.
    evidence_root = 'audit/ocr-agent-20260921-langgraph'
    for name in ('latest-development-checks.json', 'NEXT.md', 'supported-configurations.json'):
        source = ROOT / evidence_root / name
        if not source.is_file():
            raise FileNotFoundError('Bundled manual evidence is missing: ' + str(source))
        target = staging / evidence_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # Machine-local developer inventory is not portable runtime configuration.
    (staging / 'config/development-machine.json').unlink(missing_ok=True)
    shutil.copy2(ROOT / 'README.md', staging / 'README.md')
    wheelhouse = ROOT / 'build/ocr-agent-20260921-langgraph/wheelhouse'
    lock = json.loads((ROOT / 'config/runtime-locks/agent.json').read_text('utf-8'))
    for package in lock['packages']:
        wheel = wheelhouse / package['file']
        if checksum(wheel) != package['sha256']:
            raise ValueError('Framework wheel changed: ' + wheel.name)
        destination = staging / 'wheelhouse/agent' / wheel.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wheel, destination)
    for title, extra in [('启动工作台', ''), ('仅校对模式', '--review-only '), ('完整校验并启动', '--verify-startup ')]:
        (staging / (title + '.cmd')).write_text('@echo off\r\nstart "" "%~dp0launcher\\OfflineOCRLauncher.exe" --data "%LOCALAPPDATA%\\OfflineOCR-Agent-Experimental\\Workspace" ' + extra + '%*\r\n', encoding='utf-8')
    (staging / '启动实验助手.cmd').write_text('@echo off\r\nchcp 65001 >nul\r\n"%~dp0runtimes\\service\\python.exe" -X utf8 -B -I "%~dp0tools\\start_agent_candidate.py" --bundle "%~dp0." %*\r\n', encoding='utf-8')
    (staging / 'ocr.cmd').write_text('@echo off\r\n"%~dp0runtimes\\control\\python.exe" -X utf8 -B -I -m ocr_workbench.cli %*\r\n', encoding='ascii')
    metadata = {'tier': 'experimental_candidate_pending_package_validation', 'agent_default_enabled': False,
                'opt_in': '启动实验助手.cmd', 'default_workspace': '%LOCALAPPDATA%/OfflineOCR-Agent-Experimental/Workspace',
                'real_controller': 'not_tested', 'target_machine': 'not_tested', 'business_schema': 14,
                'source_commit_is_not_snapshot_identity': True, 'runtime_assets_inherited_from': str(base),
                'user_data_included': False, 'credentials_included': False, 'development_checkpoints_included': False,
                'source_framework_lock_sha256': checksum(ROOT / 'config/runtime-locks/agent.json')}
    (staging / 'candidate-status.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (staging / '开始前请读.txt').write_text('OCR 文档助手实验候选包\n\n尚未获得真实主控或目标机器资格。普通启动默认关闭助手；启动实验助手.cmd 是显式实验入口。\n使用独立实验工作区，不自动打开旧生产数据库。迁移前备份整个工作区和业务/检查点两库；旧包只能配合对应旧备份回退。\n真实引擎和模型资产沿用历史交付，其质量不是本阶段新成绩。\n详见 docs/ocr-agent-usage.md、ocr-agent-operations.md 和 candidate-status.json。\n', encoding='utf-8')
    source_paths = []
    for folder in ['src', 'config', 'docs', 'scripts', 'tests', 'frontend/src', 'frontend/public', 'frontend/tests', 'frontend/scripts']:
        source_paths.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc' and p.name != 'development-machine.json')
    source_paths.extend(p for pattern in ['frontend/*.json', 'frontend/*.ts', 'frontend/index.html', 'README.md'] for p in ROOT.glob(pattern) if p.is_file())
    source_map = {p.relative_to(ROOT).as_posix(): checksum(p) for p in sorted(set(source_paths))}
    with zipfile.ZipFile(staging / 'source-code.zip', 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for relative in source_map:
            archive.write(ROOT / relative, relative)
    (staging / 'source-manifest.json').write_text(json.dumps(source_map, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    for source_folder, destination_folder in [('src', 'app'), ('config', 'config'), ('docs', 'docs'), ('scripts', 'tools')]:
        for relative, expected in source_map.items():
            if relative.startswith(source_folder + '/'):
                candidate = staging / destination_folder / relative[len(source_folder) + 1:]
                if checksum(candidate) != expected:
                    raise ValueError('Source changed while freezing: ' + relative)
    print(json.dumps({'source_frozen': len(source_map), 'destination': str(output)}), flush=True)
    inherited_result = stage_inherited(base, staging, inherited, hardlink_assets=args.hardlink_inherited_assets)
    files = [{'path': p.relative_to(staging).as_posix(), 'bytes': p.stat().st_size, 'sha256': checksum(p)} for p in sorted(staging.rglob('*')) if p.is_file()]
    (staging / 'manifest.json').write_text(json.dumps({'kind': 'experimental-agent-candidate', 'files': files}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    staging.rename(output)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'status': 'built_not_yet_qualified', 'bundle': str(output), 'files': len(files), 'bytes': sum(p['bytes'] for p in files),
               'manifest_sha256': checksum(output / 'manifest.json'), 'source_sha256': checksum(output / 'source-manifest.json'),
               'base_assets_verified': len(inherited), 'prior_delivery_modified': False, 'real_controller': 'not_tested',
               'staging_storage': 'shared_hardlinks' if args.hardlink_inherited_assets else 'independent_copies',
               'hardlink_roots': ['models/', 'runtimes/* (except runtimes/service/)'] if args.hardlink_inherited_assets else [],
               'hardlinked_assets_are_independent_physical_copies': False,
               'independent_archive_required': bool(args.hardlink_inherited_assets),
               'shared_hardlink_risk': 'In-place changes to either path change both bundles until an independently written ZIP is made; do not mutate inherited model or non-service runtime files.' if args.hardlink_inherited_assets else None,
               **inherited_result}
    args.receipt.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
