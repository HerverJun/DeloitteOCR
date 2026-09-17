"""Build-time only: resumable downloads of revision/SHA256-locked review assets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, target, expected, *, proxy=None):
    """Never replace another asset; only publish a fully verified partial file."""
    if target.exists():
        if target.stat().st_size != expected['bytes'] or sha256(target) != expected['sha256']:
            raise ValueError(f'Existing file does not match lock; left unchanged: {target}')
        print(f'Verified {target.name}', flush=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy} if proxy else {}))
    for attempt in range(5):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > expected['bytes']:
                raise ValueError(f'Oversized partial file; left unchanged: {partial}')
            if offset < expected['bytes']:
                request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
                with opener.open(request, timeout=60) as response:
                    append = offset > 0 and response.status == 206
                    if append:
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                        if not match or int(match[1]) != offset or int(match[3]) != expected['bytes']:
                            raise ValueError('Invalid resumed download range')
                    count = offset if append else 0
                    last_report = time.monotonic()
                    with partial.open('ab' if append else 'wb') as out:
                        while chunk := response.read(4 * 1024 * 1024):
                            count += len(chunk)
                            if count > expected['bytes']:
                                raise ValueError('Downloaded asset exceeds locked size')
                            out.write(chunk)
                            if time.monotonic() - last_report > 15:
                                print(f'{target.name}: {count}/{expected["bytes"]}', flush=True)
                                last_report = time.monotonic()
            if partial.stat().st_size != expected['bytes']:
                raise OSError('Incomplete download')
            if sha256(partial) != expected['sha256']:
                raise ValueError(f'SHA256 mismatch; partial retained for inspection: {partial}')
            # Exclusive creation protects against a competing preparer publishing
            # a different file during the transfer. Move preserves disk headroom.
            if target.exists():
                raise FileExistsError(f'Asset was concurrently installed: {target}')
            partial.rename(target)
            print(f'Installed and verified {target.name}: {expected["bytes"]} bytes', flush=True)
            return
        except (OSError, urllib.error.URLError) as error:
            if isinstance(error, FileExistsError) or attempt == 4:
                raise
            print(f'Retry {attempt + 1}: {type(error).__name__}', flush=True)
            time.sleep(min(2 ** attempt, 8))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config/multimodal-review.json')
    parser.add_argument('--profile')
    parser.add_argument('--output', type=Path, default=Path('build/multimodal-review-20260917/models'), help='Models directory, not entire bundle')
    parser.add_argument('--proxy')
    parser.add_argument('--endpoint', default='https://huggingface.co')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args(argv)
    if urllib.parse.urlsplit(args.endpoint).scheme != 'https':
        raise ValueError('Build download endpoint must use HTTPS')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from ocr_workbench.multimodal_runtime import load_config, _relative
    config = load_config(args.output.parent, args.profile, args.config)
    profile = config['profile']
    records = []
    for key in ('model', 'projector', 'license_asset'):
        asset = profile[key]
        rel = Path(asset['path'])
        if not rel.parts or rel.parts[0] != 'models':
            raise ValueError('Preparation only writes to the chosen models directory')
        target = _relative(args.output, Path(*rel.parts[1:]).as_posix())
        url = asset.get('url') or (args.endpoint.rstrip('/') + '/' + profile['repo'] + '/resolve/'
               + profile['revision'] + '/' + urllib.parse.quote(asset['remote_path'], safe='/'))
        if args.verify_only:
            if not target.is_file() or target.stat().st_size != asset['bytes'] or sha256(target) != asset['sha256']:
                raise ValueError(f'Asset missing or invalid: {target}')
            print(f'Verified {target.name}', flush=True)
        else:
            fetch(url, target, asset, proxy=args.proxy)
        records.append({'path': target.name, 'url': url, 'bytes': asset['bytes'], 'sha256': asset['sha256']})
    manifest = {'schema_version': 1, 'profile_id': config['profile_id'], 'repo': profile['repo'],
                'revision': profile['revision'], 'base_model': profile['model_id'],
                'base_revision': profile['base_revision'], 'license': profile['license'],
                'config_sha256': config['config_sha256'], 'files': records}
    dest = args.output / Path(profile['model']['path']).parent.name / 'source-manifest.json'
    if not args.verify_only:
        from ocr_workbench.atomic_files import write_json
        write_json(dest, manifest, durable=True)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
