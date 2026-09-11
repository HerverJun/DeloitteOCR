"""Record all installed distributions, artifact hashes and model revisions."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument('--bundle', type=Path, required=True)
p.add_argument('--metadata-only', action='store_true', help='Generate locks; package_bundle.py hashes while archiving')
a = p.parse_args()
root = a.bundle.resolve()
locks = root/'locks'
locks.mkdir(exist_ok=True)
code = '''import importlib.metadata as m,json
print(json.dumps(sorted([{'name':d.metadata['Name'],'version':d.version,'requires':d.requires or [],'license':d.metadata.get('License-Expression') or d.metadata.get('License')} for d in m.distributions()],key=lambda d:d['name'].lower())))'''
for name in ['control', 'ppocr', 'paddlevl', 'glm', 'hunyuan']+(['service'] if (root/'runtimes/service/python.exe').exists() else []):
    exe = root/'runtimes'/name/'python.exe'
    info = json.loads(subprocess.check_output([str(exe),'-I','-c',code],text=True,encoding='utf-8'))
    (locks/f'{name}.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    (locks/f'{name}.txt').write_text('\n'.join(f'{d["name"]}=={d["version"]}' for d in info)+'\n',encoding='utf-8')
    (root/'runtimes'/name/'requirements.lock.txt').write_text((locks/f'{name}.txt').read_text(encoding='utf-8'),encoding='utf-8')
models = {}
for path in (root/'models').glob('*/source-manifest.json'):
    info = json.loads(path.read_text(encoding='utf-8'))
    models[path.parent.name] = {'repo':info['repo'],'revision':info['revision']}
(locks/'models.json').write_text(json.dumps(models,ensure_ascii=False,indent=2),encoding='utf-8')
if a.metadata_only:
    raise SystemExit(0)
files = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or path == root/'manifest.json' or '__pycache__' in path.parts:
        continue
    rel = path.relative_to(root)
    if rel.parts[0] in {'runs', 'cache', 'results'}:
        continue
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    files.append({'path':rel.as_posix(),'bytes':path.stat().st_size,'sha256':digest})
    if len(files)%5000==0:print(f'Hashed {len(files)} files',flush=True)
(root/'manifest.json').write_text(json.dumps({'schema_version':1,'files':files},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'files':len(files),'bytes':sum(f['bytes'] for f in files)}))
