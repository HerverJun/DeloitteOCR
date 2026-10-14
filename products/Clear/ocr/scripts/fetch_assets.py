"""Build-time downloads only; immutable revisions and SHA256 evidence."""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import time
import re
import requests


def download(url: str, target: Path, proxy: str | None = None) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies = {'http': proxy, 'https': proxy}
    for attempt in range(4):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            with session.get(url, stream=True, timeout=(30, 90),
                             headers={'Range': f'bytes={offset}-'} if offset else {}) as res:
                res.raise_for_status()
                append = offset > 0 and res.status_code == 206
                if append and not res.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise ValueError('Invalid resumed download range')
                with partial.open('ab' if append else 'wb') as f:
                    for chunk in res.iter_content(1024 * 1024):
                        f.write(chunk)
            partial.replace(target)
            digest = hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()
            return {'url': url, 'file': str(target), 'bytes': target.stat().st_size, 'sha256': digest}
        except (requests.RequestException, OSError):
            if attempt == 3:
                raise
            time.sleep(2)
    raise RuntimeError('Unreachable')


def chunked_download(url: str, target: Path, proxy: str | None = None):
    """Bound each response; some corporate gateways reject large streaming bodies."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies = {'https': proxy}
    offset = partial.stat().st_size if partial.exists() else 0
    total = None
    while total is None or offset < total:
        for attempt in range(5):
            try:
                res = session.get(url, headers={'Range': f'bytes={offset}-{offset+8*1024*1024-1}'}, timeout=(20, 60))
                res.raise_for_status()
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', res.headers.get('Content-Range', ''))
                if res.status_code != 206 or not match or int(match[1]) != offset:
                    raise ValueError('Server did not honor exact byte range')
                total = int(match[3])
                if len(res.content) != int(match[2]) - offset + 1:
                    raise ValueError('Incomplete chunk')
                with partial.open('ab') as out:
                    out.write(res.content)
                offset += len(res.content)
                print(f'{target.name}: {offset}/{total}', flush=True)
                break
            except (requests.RequestException, ValueError):
                if attempt == 4:
                    raise
                time.sleep(1)
    partial.replace(target)
    return {'file': str(target), 'url': url, 'bytes': total,
            'sha256': hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()}


def snapshot(repo: str, root: Path, endpoint: str, proxy: str | None = None, revision: str | None = None):
    session = requests.Session()
    session.trust_env = False
    # Metadata from the mirror is reachable directly; large redirected blobs
    # may need the explicitly supplied build proxy.
    model_dir = root / repo.split('/')[-1]
    local_manifest=model_dir/'source-manifest.json'
    if revision is None and local_manifest.is_file():
        saved=json.loads(local_manifest.read_text(encoding='utf-8'))
        if saved['repo']!=repo:raise ValueError('Model directory belongs to a different repository')
        revision=saved['revision']
    res = session.get(f'{endpoint}/api/models/{repo}'+(f'/revision/{revision}' if revision else ''), timeout=40)
    res.raise_for_status()
    info = res.json()
    revision = info['sha']
    names = [x['rfilename'] for x in info['siblings']
             if not x['rfilename'].startswith(('.git', '.eval_results/', 'v1.0/', 'dflash/'))]
    def fetch(name):
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Unsafe repository path')
        dest = model_dir / rel
        if dest.exists():
            return {'url': f'{endpoint}/{repo}/resolve/{revision}/{name}',
                    'file': str(dest), 'bytes': dest.stat().st_size,
                    'sha256': hashlib.file_digest(dest.open('rb'), 'sha256').hexdigest()}
        result = download(f'{endpoint}/{repo}/resolve/{revision}/{name}', dest,
                          proxy if name.endswith(('.safetensors', '.bin')) else None)
        print(f'Downloaded {repo}/{name}: {result["bytes"]}', flush=True)
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(fetch, names))
    manifest = {'repo': repo, 'revision': revision, 'files': files}
    (model_dir / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo')
    parser.add_argument('--url')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--endpoint', default='https://hf-mirror.com')
    parser.add_argument('--proxy')
    parser.add_argument('--chunked', action='store_true')
    parser.add_argument('--revision')
    args = parser.parse_args()
    if args.repo:
        snapshot(args.repo, args.output, args.endpoint, args.proxy,args.revision)
    else:
        fetch = chunked_download if args.chunked else download
        print(json.dumps(fetch(args.url, args.output, args.proxy), ensure_ascii=False))
