"""Stage a NEW offline bundle; finalize current source only when explicitly run.

assets copies locked base payloads and the prepared reviewer model. finalize copies
the current application/config/built web/docs and writes a full manifest. verify
checks the complete manifest and reviewer hashes without starting any GPU model.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
LARGE = ('runtimes', 'models', 'build', 'fonts', 'launcher', 'licenses', 'wheelhouse', 'fixtures')
SKIP = {'__pycache__'}
MARKER = 'multimodal-staging.json'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def publish(path, value):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def ordinary(path):
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ValueError(f'Links are not permitted in a standalone bundle: {path}')
    if path.exists() and getattr(path.stat(follow_symlinks=False), 'st_file_attributes', 0) & 0x400:
        raise ValueError(f'Reparse points are not permitted: {path}')


def inside(root, relative):
    rel = Path(relative)
    if not isinstance(relative, str) or not relative or rel.is_absolute() or '..' in rel.parts or ':' in relative or '\\' in relative:
        raise ValueError(f'Unsafe bundle member: {relative}')
    target = root / rel
    if not target.resolve().is_relative_to(root):
        raise ValueError('Bundle path escapes target')
    for path in (target, *target.parents):
        ordinary(path)
        if path == root:
            break
    return target


def files(root):
    ordinary(root)
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name not in SKIP]
        for name in folders:
            ordinary(Path(directory) / name)
        for name in names:
            path = Path(directory) / name
            ordinary(path)
            yield path


def manifest_records(path):
    value = json.loads(path.read_text('utf-8'))
    records = {}
    casefolded = set()
    for item in value['files']:
        rel = item['path']
        if rel.casefold() in casefolded:
            raise ValueError('Duplicate source manifest entry')
        casefolded.add(rel.casefold())
        records[rel] = item
    return records


def copy_checked(source, target, expected):
    """Stream a locked asset to a private partial, hash before atomic publication."""
    ordinary(source)
    ordinary(target)
    if target.exists():
        if target.stat().st_size == expected['bytes'] and digest(target) == expected['sha256']:
            return False
        raise ValueError(f'Existing staged asset differs; left untouched: {target}')
    if source.stat().st_size != expected['bytes']:
        raise ValueError(f'Base asset size differs from manifest: {source}')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.staging-part')
    ordinary(partial)
    signature = source.stat()
    sha = hashlib.sha256()
    with source.open('rb') as stream, partial.open('wb') as output:
        while chunk := stream.read(8 * 1024 * 1024):
            sha.update(chunk)
            output.write(chunk)
    if sha.hexdigest() != expected['sha256'] or source.stat().st_mtime_ns != signature.st_mtime_ns:
        raise ValueError(f'Base asset changed or digest mismatch: {source}')
    shutil.copystat(source, partial)
    partial.rename(target)
    return True


def ownership(args, *, create=False):
    source = args.source_bundle.resolve()
    delivery = args.delivery.resolve()
    bundle = delivery / 'bundle'
    for p in (args.delivery, delivery, bundle, source):
        ordinary(p)
    if delivery == source or delivery.is_relative_to(source) or source.is_relative_to(delivery):
        raise ValueError('Destination must be entirely separate from the source bundle')
    if not (source / 'runtimes/service/python.exe').is_file() or not (source / 'manifest.json').is_file():
        raise ValueError('A complete source bundle with manifest is required')
    identity = {'schema_version': 1, 'source_bundle': str(source),
                'source_manifest_sha256': digest(source / 'manifest.json'),
                'review_models': str(args.review_models.resolve()), 'profile_id': args.profile}
    marker = delivery / MARKER
    if not marker.exists():
        if not create:
            raise ValueError('Run the assets phase first')
        if delivery.exists():
            raise ValueError('Destination already exists without staging ownership; refusing to modify it')
        delivery.mkdir(parents=True, exist_ok=False)
        bundle.mkdir()
        state = {**identity, 'created_utc': datetime.now(timezone.utc).isoformat(), 'phase': 'copying-assets'}
        with marker.open('x', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
    else:
        state = json.loads(marker.read_text('utf-8'))
        if any(state.get(key) != value for key, value in identity.items()):
            raise ValueError('Staging directory belongs to a different source/configuration')
    return source, delivery, bundle, state


def stage_assets(args):
    source, delivery, bundle, state = ownership(args, create=True)
    if state['phase'] == 'finalized':
        raise ValueError('This bundle was finalized; choose a new delivery directory')
    records = manifest_records(source / 'manifest.json')
    jobs = []
    for name in LARGE:
        folder = source / name
        if not folder.exists():
            continue
        for path in files(folder):
            rel = path.relative_to(source).as_posix()
            expected = records.get(rel)
            if expected is None:
                raise ValueError(f'Base asset absent from source manifest: {rel}')
            jobs.append((path, inside(bundle, rel), expected))
    sys.path.insert(0, str(ROOT / 'src'))
    from ocr_workbench.multimodal_runtime import load_config
    profile = load_config(bundle, args.profile, ROOT / 'config/multimodal-review.json')['profile']
    for key in ('model', 'projector', 'license_asset'):
        asset = profile[key]
        rel = Path(asset['path'])
        if rel.parts[0] != 'models':
            raise ValueError('Review profile points outside models directory')
        origin = inside(args.review_models.resolve(), Path(*rel.parts[1:]).as_posix())
        jobs.append((origin, inside(bundle, asset['path']), asset))
    model_dir = Path(profile['model']['path']).parent
    origin_manifest = args.review_models.resolve() / model_dir.name / 'source-manifest.json'
    source_info = {'bytes': origin_manifest.stat().st_size, 'sha256': digest(origin_manifest)}
    jobs.append((origin_manifest, inside(bundle, (model_dir / 'source-manifest.json').as_posix()), source_info))
    needed = sum(expected['bytes'] for _, target, expected in jobs if not target.exists())
    free = shutil.disk_usage(delivery).free
    if free < needed + 2 * 1024 ** 3:
        raise ValueError(f'Insufficient disk space: need {needed} bytes plus 2 GiB, have {free}')
    started = last = time.monotonic()
    total = copied = byte_count = 0
    print(json.dumps({'phase': 'copying-assets', 'files': len(jobs), 'new_bytes': needed, 'free_bytes': free}), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(copy_checked, *job): job[2]['bytes'] for job in jobs}
        for future in as_completed(pending):
            copied += bool(future.result())
            total += 1
            byte_count += pending[future]
            if time.monotonic() - last >= 15:
                print(json.dumps({'verified_copied_files': total, 'bytes': byte_count, 'seconds': round(time.monotonic() - started, 1)}), flush=True)
                last = time.monotonic()
    state.update(phase='assets-ready', asset_files=len(jobs), asset_bytes=sum(j[2]['bytes'] for j in jobs),
                 staged_utc=datetime.now(timezone.utc).isoformat())
    publish(delivery / MARKER, state)
    print(json.dumps({'phase': state['phase'], 'bundle': str(bundle), 'copied': copied,
                      'files': total, 'seconds': time.monotonic()-started}), flush=True)


def overlay(source, target, boundary):
    ordinary(source)
    ordinary(target)
    if not target.resolve().is_relative_to(boundary):
        raise ValueError('Overlay destination escaped the new bundle')
    if source.is_dir():
        if target.exists():
            # Only replace explicitly named application folders in this script's
            # newly owned bundle. Both resolved boundary and reparse checks ran.
            for _ in files(target):
                pass
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def final_manifest(bundle, workers):
    paths = [p for p in files(bundle) if p.relative_to(bundle).as_posix() != 'manifest.json'
             and p.relative_to(bundle).parts[0] not in ('cache', 'runs', 'results')]
    if any(p.name.endswith('.staging-part') for p in paths):
        raise ValueError('Incomplete asset copy remains in bundle')
    def record(path):
        return {'path': path.relative_to(bundle).as_posix(), 'bytes': path.stat().st_size, 'sha256': digest(path)}
    started = last = time.monotonic()
    records = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(record, paths):
            records.append(result)
            if time.monotonic() - last >= 15:
                print(json.dumps({'phase': 'hashing-final-manifest', 'files': len(records), 'seconds': round(time.monotonic()-started, 1)}), flush=True)
                last = time.monotonic()
    publish(bundle / 'manifest.json', {'schema_version': 1, 'files': sorted(records, key=lambda r: r['path'])})
    return records


def finalize(args):
    source, delivery, bundle, state = ownership(args)
    if state['phase'] == 'finalized' and args.refresh_finalized:
        if digest(bundle / 'manifest.json') != state['manifest_sha256']:
            raise ValueError('Cannot refresh a changed manifest')
        previous = delivery / ('draft-manifest-' + state['manifest_sha256'] + '.json')
        if not previous.exists():
            shutil.copy2(bundle / 'manifest.json', previous)
        state['previous_draft_manifest_sha256'] = state['manifest_sha256']
        state['phase'] = 'finalizing'
    if state['phase'] not in ('assets-ready', 'finalizing'):
        raise ValueError('Assets must be complete and bundle must not already be finalized')
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise ValueError('Build frontend/dist before finalizing')
    state['phase'] = 'finalizing'
    publish(delivery / MARKER, state)
    # The base bundle predates external API review. Install only verified wheels
    # into this newly owned staging copy before producing its final manifest.
    from prepare_external_review_runtime import prepare
    prepare(bundle / 'runtimes/service', args.external_wheels, bundle=bundle)
    # The source bundle supplies entry points and unchanged audit metadata.
    # Its old manifest is never reused as the new bundle's integrity claim.
    for path in source.iterdir():
        if path.name in LARGE or path.name in ('app', 'config', 'docs', 'web', 'manifest.json'):
            continue
        overlay(path, bundle / path.name, bundle)
    for origin, destination in (('src', 'app'), ('config', 'config'), ('docs', 'docs'), ('frontend/dist', 'web')):
        overlay(ROOT / origin, bundle / destination, bundle)
    for name in ('README.md', 'LICENSE'):
        overlay(ROOT / name, bundle / name, bundle)
    if args.delivery_info:
        overlay(args.delivery_info, bundle / 'delivery-info', bundle)
    if args.readme_first:
        overlay(args.readme_first, bundle / '开始前请读.txt', bundle)
    if args.source_archive:
        overlay(args.source_archive, bundle / 'source-code.zip', bundle)
    (bundle / 'tools').mkdir(exist_ok=True)
    for path in (ROOT / 'scripts').glob('*.py'):
        overlay(path, bundle / 'tools' / path.name, bundle)
    (bundle / 'locks').mkdir(exist_ok=True)
    overlay(ROOT / 'frontend/package-lock.json', bundle / 'locks/frontend-package-lock.json', bundle)
    model_lock = {}
    for path in (bundle / 'models').glob('*/source-manifest.json'):
        data = json.loads(path.read_text('utf-8'))
        model_lock[path.parent.name] = {'repo': data['repo'], 'revision': data['revision']}
    publish(bundle / 'locks/models.json', model_lock)
    # Record this copy only now, without creating a premature release-source lock.
    copied_source = []
    for origin, destination in (('src', 'app'), ('config', 'config'), ('docs', 'docs'), ('frontend/dist', 'web')):
        for path in files(ROOT / origin):
            sha = digest(path)
            copied = bundle / destination / path.relative_to(ROOT / origin)
            if not copied.is_file() or digest(copied) != sha:
                raise ValueError('Source changed while finalizing; retry before publishing the manifest')
            copied_source.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': sha})
    receipt = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
               'base_bundle': str(source), 'base_manifest_sha256': state['source_manifest_sha256'],
               'profile_id': args.profile, 'source_freeze': False,
               'copied_source': copied_source}
    publish(bundle / 'locks/multimodal-copy-receipt.json', receipt)
    records = final_manifest(bundle, args.workers)
    state.update(phase='finalized', finalized_utc=datetime.now(timezone.utc).isoformat(),
                 manifest_sha256=digest(bundle / 'manifest.json'), files=len(records), bytes=sum(r['bytes'] for r in records))
    publish(delivery / MARKER, state)
    print(json.dumps({'phase': 'finalized', 'bundle': str(bundle), 'files': len(records), 'bytes': state['bytes'],
                      'manifest_sha256': state['manifest_sha256'], 'next': 'run --phase verify'}), flush=True)


def verify(args):
    _, delivery, bundle, state = ownership(args)
    if state['phase'] != 'finalized':
        raise ValueError('Finalize the bundle before verifying')
    if digest(bundle / 'manifest.json') != state['manifest_sha256']:
        raise ValueError('Final manifest changed after publication')
    sys.path.insert(0, str(bundle / 'app'))
    from ocr_workbench.startup import verify_integrity
    from ocr_workbench.multimodal_runtime import load_config, review_readiness
    started = last = time.monotonic()
    def progress(done, total):
        nonlocal last
        if time.monotonic() - last >= 15:
            print(json.dumps({'phase': 'verifying', 'files': done, 'total': total}), flush=True)
            last = time.monotonic()
    errors = verify_integrity(bundle, progress)
    ready = review_readiness(bundle, load_config(bundle, args.profile), verify_hashes=True)
    report = {'passed': not errors and ready['ready'] and ready['hashes_verified'], 'bundle': str(bundle),
              'manifest_sha256': state['manifest_sha256'], 'integrity_errors': errors, 'review_model': ready,
              'seconds': time.monotonic() - started, 'gpu_inference_started': False}
    publish(delivery / 'multimodal-bundle-verification.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report['passed']:
        raise SystemExit(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=('assets', 'finalize', 'verify'))
    parser.add_argument('--source-bundle', type=Path, default=Path('E:/OCR-generic-tool-fixes-20260917/bundle'))
    parser.add_argument('--delivery', type=Path, default=Path('D:/OCR-multimodal-workbench-20260917'))
    parser.add_argument('--review-models', type=Path, default=Path('D:/OCR-multimodal-models-20260917/models'))
    parser.add_argument('--profile', default='qwen35-4b-q4')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--external-wheels', type=Path, default=ROOT / 'build/external-api-review/wheelhouse')
    parser.add_argument('--delivery-info', type=Path, help='Fresh release receipts to replace inherited delivery metadata')
    parser.add_argument('--readme-first', type=Path, help='Version-specific first-run instructions')
    parser.add_argument('--source-archive', type=Path, help='Source-only archive, excluding datasets and credentials')
    parser.add_argument('--refresh-finalized', action='store_true', help='Refresh an unpublished staging bundle; retain its prior draft manifest')
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error('--workers must be 1..8')
    {'assets': stage_assets, 'finalize': finalize, 'verify': verify}[args.phase](args)


if __name__ == '__main__':
    main()
