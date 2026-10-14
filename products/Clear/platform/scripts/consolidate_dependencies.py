"""Copy sealed local dependencies into one physical development program tree."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import stat
import time

_checked_directories: set[Path] = set()


def physical(path: Path) -> Path:
    path = path.absolute()
    if path.drive.upper() != 'F:' or '..' in path.parts:
        raise ValueError(f'Project paths must stay on F: {path}')
    for ancestor in (path, *path.parents):
        if ancestor in _checked_directories:
            break
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f'Dependency copies must not traverse directory links: {ancestor}')
        if stat.S_ISDIR(info.st_mode):
            _checked_directories.add(ancestor)
    return path


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or path.drive or '\\' in value or any(p in {'', '.', '..'} for p in value.split('/')):
        raise ValueError('Invalid sealed relative path')
    return path


def save(path: Path, value: dict) -> None:
    physical(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--forensics', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    source, forensic, destination = map(physical, (args.source, args.forensics, args.destination))
    if any(destination == root or destination.is_relative_to(root) for root in (source, forensic)):
        raise ValueError('Destination must be separate from the preserved source packages')
    sources, plan = {}, {}
    for label, root, prefixes in (
        ('native', source, ('bundles/ocr/', 'bundles/classification/', 'runtimes/platform/', 'runtimes/data/')),
        ('forensics', forensic, ('runtimes/forensics/', 'app/modules/image_forensics/')),
    ):
        declaration = physical(root / 'candidate-manifest.json')
        payload = json.loads(declaration.read_text(encoding='utf-8'))
        sources[label] = {'root': str(root), 'manifest_sha256': sha(declaration)}
        for name, identity in payload['files'].items():
            if not name.startswith(prefixes):
                continue
            target = name.removeprefix('app/') if name.startswith('app/modules/') else name
            if target in plan:
                raise ValueError('Duplicate dependency target')
            relative(target)
            plan[target] = {'source': label, 'source_path': name, **identity}
    total = sum(item['bytes'] for item in plan.values())
    if shutil.disk_usage(destination.parent).free < total + 5 * 1024**3:
        raise ValueError('Insufficient F-drive space for a physical dependency copy')
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / 'development-binding.json').exists():
        raise ValueError('This dependency tree is already bound for development; use a fresh destination')
    expected_manifest = {'schema': 1, 'sources': sources, 'files': plan}
    previous_manifest = destination / 'dependency-manifest.json'
    if any(destination.iterdir()) and (not previous_manifest.is_file() or
            json.loads(previous_manifest.read_text(encoding='utf-8')) != expected_manifest):
        raise ValueError('Nonempty destination belongs to another dependency copy')
    provenance = destination / 'provenance'
    provenance.mkdir(exist_ok=True)
    for label, root in (('native', source), ('forensics', forensic)):
        shutil.copyfile(physical(root / 'candidate-manifest.json'), physical(provenance / f'{label}-candidate-manifest.json'))
    for app in ('ocr', 'classification'):
        shutil.copyfile(physical(source / f'bundles/{app}/bundle.manifest.json'),
                        physical(provenance / f'{app}-bundle.manifest.json'))
    save(destination / 'dependency-manifest.json', expected_manifest)
    started = previous = time.monotonic()
    copied = checked_bytes = 0
    def copy_one(entry):
        name, identity = entry
        root = source if identity['source'] == 'native' else forensic
        original = physical(root / relative(identity['source_path']))
        target = physical(destination / relative(name))
        if original.stat().st_size != identity['bytes']:
            raise ValueError(f'Source size differs from sealed dependency: {name}')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == identity['bytes'] and sha(target) == identity['sha256']:
            return identity['bytes'], 0
        shutil.copyfile(original, target)
        if target.stat().st_size != identity['bytes'] or sha(target) != identity['sha256']:
            raise ValueError(f'Copied dependency differs from sealed bytes: {name}')
        return identity['bytes'], 1
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='dependency-copy') as pool:
        for index, (size, count) in enumerate(pool.map(copy_one, plan.items()), 1):
            copied += count
            checked_bytes += size
            if time.monotonic() - previous >= 15:
                print(f'Verified {index}/{len(plan)} files; {checked_bytes / 1024**3:.2f}/{total / 1024**3:.2f} GiB', flush=True)
                previous = time.monotonic()
    save(destination / 'copy-verification.json', {
        'schema': 1, 'completed_at': datetime.now(timezone.utc).isoformat(),
        'program_root': str(destination), 'dependency_manifest_sha256': sha(destination / 'dependency-manifest.json'),
        'files': len(plan), 'bytes': total, 'new_copies': copied, 'elapsed_seconds': time.monotonic() - started,
        'verification': 'Every destination file checked against original sealed size and SHA256',
        'directory_links': 0,
    })
    print(f'Physical copy complete: {len(plan)} files, {total / 1024**3:.2f} GiB; all SHA256 checks passed', flush=True)


if __name__ == '__main__':
    main()
