"""Add hash-locked HTTP dependencies to a staging service runtime, offline.

Only pure Python wheels from config/service-wheelhouse.json are accepted. The
application never invokes this builder or downloads packages at runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ('httpx', 'httpcore', 'certifi')


def prepare(runtime, wheelhouse, *, bundle=None):
    runtime, wheelhouse = Path(runtime).resolve(), Path(wheelhouse).resolve()
    if bundle is not None:
        bundle = Path(bundle).resolve()
        if runtime != bundle / 'runtimes/service':
            raise ValueError('Runtime must be the specified staging bundle service runtime')
    if not (runtime / 'python.exe').is_file():
        raise ValueError('Specify the existing service runtime in a staging bundle')
    lock = json.loads((ROOT / 'config/service-wheelhouse.json').read_text('utf-8'))
    wheels = [w for w in lock['files'] if w['file'].split('-')[0] in PACKAGES]
    if len(wheels) != len(PACKAGES):
        raise ValueError('Incomplete external API dependency lock')
    # Verify every archive and path before changing the staged runtime.
    entries = []
    for item in wheels:
        path = wheelhouse / item['file']
        if not path.name.endswith('-py3-none-any.whl'):
            raise ValueError('Only the locked pure Python HTTP wheels are supported')
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError(f'Wheel SHA256 mismatch: {path.name}')
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative = PurePosixPath(member.filename)
                if (relative.is_absolute() or '..' in relative.parts or ':' in member.filename
                        or '\\' in member.filename or relative.parts[0].endswith('.data')):
                    raise ValueError(f'Unsupported wheel member: {member.filename}')
                entries.append((relative, archive.read(member)))
    site = runtime / 'Lib/site-packages'
    for relative, data in entries:
        destination = site.joinpath(*relative.parts)
        # Different versions must be rebuilt deliberately, never blended together.
        if destination.exists() and destination.read_bytes() != data:
            raise ValueError(f'Existing runtime file differs; use a clean staging copy: {relative}')
    for package in PACKAGES:
        expected = next(w['file'].split('-')[1] for w in wheels if w['file'].startswith(package + '-'))
        if any(p.name != f'{package}-{expected}.dist-info' for p in site.glob(f'{package}-*.dist-info')):
            raise ValueError(f'Another version of {package} is installed; use a clean staging copy')
    for relative, data in entries:
        destination = site.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    probe = subprocess.run([str(runtime / 'python.exe'), '-B', '-I', '-c',
        'import httpx,httpcore,certifi,anyio,h11,idna,json;from importlib.metadata import version;'
        'print(json.dumps({p:version(p) for p in ["httpx","httpcore","certifi","anyio","h11","idna"]}))'],
        capture_output=True, text=True, check=True)
    versions = json.loads(probe.stdout)
    # Version checks below cover HTTP's existing and newly added transitive imports.
    pins = dict(line.strip().split('==') for line in (ROOT / 'config/runtime-locks/service.txt').read_text('utf-8').splitlines()
                if '==' in line)
    if any(versions[name] != pins[name] for name in versions):
        raise ValueError(f'Service runtime dependency versions differ from lock: {versions}')
    receipt = {'wheels': wheels, 'versions': versions, 'network_used': False}
    if bundle is not None:
        destination = bundle / 'wheelhouse/external-review'
        destination.mkdir(parents=True, exist_ok=True)
        for item in wheels:
            shutil.copy2(wheelhouse / item['file'], destination / item['file'])
        shutil.copytree(ROOT / 'licenses/external-review', bundle / 'licenses/external-review', dirs_exist_ok=True)
        (bundle / 'locks').mkdir(exist_ok=True)
        (bundle / 'locks/external-review-runtime.json').write_text(json.dumps(receipt, indent=2) + '\n', 'utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--bundle', type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.runtime, args.wheelhouse, bundle=args.bundle), indent=2))


if __name__ == '__main__':
    main()
