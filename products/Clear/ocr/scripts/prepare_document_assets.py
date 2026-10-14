"""Build-time only: freeze new table models and CPU wheels without touching OCR runtimes."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from fetch_assets import snapshot

MODELS = ['PP-LCNet_x1_0_table_cls', 'SLANeXt_wired', 'SLANeXt_wireless',
          'RT-DETR-L_wired_table_cell_det', 'RT-DETR-L_wireless_table_cell_det']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--endpoint', default='https://huggingface.co')
    parser.add_argument('--skip-models', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    lock = root / 'config/table-model-lock.json'
    known = json.loads(lock.read_text('utf-8')) if lock.exists() else {}
    if not args.skip_models:
        for name in MODELS:
            manifest = snapshot('PaddlePaddle/' + name, args.output / 'models', args.endpoint,
                                revision=known.get(name, {}).get('revision'))
            known[name] = {'repo': manifest['repo'], 'revision': manifest['revision'],
                           'files': [{'name': Path(f['file']).relative_to(args.output / 'models' / name).as_posix(),
                                      'sha256': f['sha256'], 'bytes': f['bytes']} for f in manifest['files']]}
            lock.write_text(json.dumps(known, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    wheels = []
    for wheel in sorted((args.output / 'wheelhouse').glob('*.whl')):
        with wheel.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        with zipfile.ZipFile(wheel) as archive:
            metadata = archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))).decode('utf-8')
        fields = dict(line.split(': ', 1) for line in metadata.splitlines() if line.startswith(('Name: ', 'Version: ')))
        wheels.append({'name': fields['Name'], 'version': fields['Version'], 'file': wheel.name,
                       'sha256': sha, 'bytes': wheel.stat().st_size})
    (root / 'config/pdf-wheelhouse.json').write_text(json.dumps({'python': 'CPython 3.12 Windows x64', 'wheels': wheels}, indent=2) + '\n', 'utf-8')
    (root / 'config/runtime-locks/pdf.txt').write_text(''.join(f"{w['name']}=={w['version']}\n" for w in sorted(wheels, key=lambda w: w['name'].lower())), 'utf-8')
    print(f'Locked {len(wheels)} CPU wheels and {len(known)} table models', flush=True)


if __name__ == '__main__':
    main()
