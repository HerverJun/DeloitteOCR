"""Promote verified B00 wheel hashes and complete licenses to an experimental lock."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / 'audit/ocr-agent-20260921-langgraph'
EXTRA = {
    'langsmith': [('LICENSE', 'https://raw.githubusercontent.com/langchain-ai/langsmith-sdk/v0.13.0/LICENSE',
                   '34e0b9842c7a31d34e53bc7eb224e81e07a34996106e029bbc72dea2d449f496')],
    'sqlite-vec': [('LICENSE-MIT', 'https://raw.githubusercontent.com/asg017/sqlite-vec/v0.1.9/LICENSE-MIT',
                    '6ce72bbe12d975bd5286e5ab0a064c069693300c47bccbc57bec18485f1621ea'),
                   ('LICENSE-APACHE', 'https://raw.githubusercontent.com/asg017/sqlite-vec/v0.1.9/LICENSE-APACHE',
                    'a38070a94d4afd9cd710e3ce67bd1de78097cfe1784c1f0109ac95d3c196bfdc')],
}


def main():
    source = json.loads((AUDIT / 'framework-lock-candidate.json').read_text('utf-8'))
    wheelhouse = ROOT / 'build/ocr-agent-20260921-langgraph/wheelhouse'
    manifest = []
    packages = []
    for wheel in source['wheels']:
        wheel_path = wheelhouse / wheel['file']
        assert wheel_path.name == wheel['file'] and hashlib.sha256(wheel_path.read_bytes()).hexdigest() == wheel['sha256']
        folder = ROOT / 'licenses/agent' / (wheel['name'] + '-' + wheel['version'])
        sources = []
        for license_path in wheel['license_files']:
            original = ROOT / license_path
            relative = original.relative_to(AUDIT / 'framework-licenses' / wheel['name'])
            sources.append((relative, original.read_bytes(), 'wheel:' + wheel['file']))
        for name, url, checksum in EXTRA.get(wheel['name'], []):
            target = folder / name
            data = target.read_bytes() if target.exists() else urllib.request.urlopen(url, timeout=30).read()
            if hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError('Upstream license hash changed: ' + url)
            sources.append((Path(name), data, url))
        if not sources:
            raise ValueError('No complete license text: ' + wheel['name'])
        licenses = []
        for relative, data, origin in sources:
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entry = {'path': target.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(data).hexdigest(), 'source': origin}
            manifest.append(entry); licenses.append(entry)
        packages.append({**{key: wheel[key] for key in ['name', 'version', 'file', 'sha256', 'license', 'requires_dist']}, 'licenses': licenses})
    lock = {'status': 'experimental_verified_dependencies', 'python': '3.12', 'platform': 'win_amd64',
            'graph_version': 'ocr-agent-graph-v1', 'state_version': 1, 'serializer': 'strict-jsonplus-v1',
            'business_schema': 14, 'tracing': 'disabled', 'pickle_fallback': False, 'packages': packages,
            'baseline_runtime_dependency_gaps': source['preexisting_pip_check_errors'],
            'qualification': 'B00 isolated offline install; not final package or real controller qualification'}
    (ROOT / 'config/runtime-locks/agent.json').write_text(json.dumps(lock, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    (ROOT / 'config/runtime-locks/agent.txt').write_text(''.join(f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}\n" for p in packages), encoding='utf-8')
    (ROOT / 'licenses/agent/manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (AUDIT / 'framework-license-completion.json').write_text(json.dumps({'status': 'pass', 'packages': len(packages), 'license_files': len(manifest),
        'lock_sha256': hashlib.sha256((ROOT / 'config/runtime-locks/agent.json').read_bytes()).hexdigest(),
        'sources': EXTRA, 'production_runtime_modified': False}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Locked {len(packages)} wheels with {len(manifest)} complete license files; production runtime unchanged.')


if __name__ == '__main__':
    main()
