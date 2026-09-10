"""Install a full resolved wheelhouse into a fresh embedded runtime, offline."""
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile
from packaging.utils import parse_wheel_filename

p = argparse.ArgumentParser()
p.add_argument('--bundle', type=Path, required=True)
p.add_argument('--archive', type=Path, required=True)
p.add_argument('--wheelhouse', type=Path, required=True)
p.add_argument('--name', required=True)
p.add_argument('--overlay-wheels',type=Path)
a = p.parse_args()
root = a.bundle/'runtimes'/a.name
if (root/'Lib/site-packages').exists() and any((root/'Lib/site-packages').iterdir()):
    raise SystemExit('Use a fresh runtime directory; refusing to mix an old installation with new wheels')
root.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(a.archive) as z:
    z.extractall(root)
(root/'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../../app\nimport site\n',encoding='utf-8')
selected={}
for directory in [a.wheelhouse,*([a.overlay_wheels] if a.overlay_wheels else [])]:
    names_in_directory=set()
    for wheel in directory.glob('*.whl'):
        name,version,_,_=parse_wheel_filename(wheel.name)
        if name in names_in_directory:
            raise SystemExit(f'Ambiguous wheel versions for {name} in {directory}')
        names_in_directory.add(name)
        selected[name]=version
requirements = sorted(f'{name}=={version}' for name,version in selected.items())
lock = root/'requirements.lock.txt'
lock.write_text('\n'.join(requirements)+'\n',encoding='utf-8')
subprocess.run([sys.executable,'-m','pip','--isolated','install','--no-index','--no-compile',
    '--find-links',str(a.wheelhouse),'--only-binary=:all:','--no-deps',
    *(['--find-links',str(a.overlay_wheels)] if a.overlay_wheels else []),
    '--target',str(root/'Lib/site-packages'),'-r',str(lock),
    '--report',str(root/'install-report.json')],check=True)
