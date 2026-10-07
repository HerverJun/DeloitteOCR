"""Restore the exact previously locked PDF component without changing runtimes."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

from workbench_platform.supervisor.processes import require_f_path


def prepare(source, cache):
    source, cache = require_f_path(source), require_f_path(cache)
    config = json.loads((source / 'config/pdf-table-tool.json').read_text('utf-8'))
    expected_files = config['files']
    if all((source / name).is_file() and hashlib.sha256((source / name).read_bytes()).hexdigest() == sha
           for name, sha in expected_files.items()):
        return {'source': str(source / config['source']), 'reused': True, 'files': len(expected_files)}
    cache.mkdir(parents=True, exist_ok=True)
    wheel = cache / ('pdfplumber-' + config['version'] + '.whl')
    if not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != config['wheel_sha256']:
        with urllib.request.urlopen('https://pypi.org/pypi/pdfplumber/' + config['version'] + '/json', timeout=30) as response:
            catalog = json.load(response)
        item = next(item for item in catalog['urls'] if item['packagetype'] == 'bdist_wheel'
                    and item['digests']['sha256'] == config['wheel_sha256'])
        with urllib.request.urlopen(item['url'], timeout=30) as response:
            payload = response.read(1024 * 1024)
        if hashlib.sha256(payload).hexdigest() != config['wheel_sha256']:
            raise ValueError('Pinned PDF wheel checksum mismatch')
        wheel.write_bytes(payload)
    target = require_f_path(source / config['source'])
    with zipfile.ZipFile(wheel) as archive:
        for name, sha in expected_files.items():
            member = str(Path(name).relative_to(config['source'])).replace('\\', '/')
            if hashlib.sha256(archive.read(member)).hexdigest() != sha:
                raise ValueError('Pinned PDF file checksum mismatch: ' + name)
        for info in archive.infolist():
            path = require_f_path(target / info.filename)
            if not path.is_relative_to(target) or info.filename.startswith(('/', '\\')):
                raise ValueError('Unsafe wheel entry')
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(info))
    return {'source': str(target), 'reused': False, 'files': len(expected_files),
            'wheel_sha256': config['wheel_sha256'], 'runtimes_unchanged': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.cache), indent=2))
