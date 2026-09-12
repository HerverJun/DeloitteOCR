"""Assemble a clean embedded CPython runtime from core files and locked wheels."""
import hashlib
import json
from pathlib import Path,PurePosixPath
import shutil
import zipfile
root=Path(__file__).resolve().parents[1];base=root/'build/document-workflow'
source=base/'bundle/runtimes/pdf';target=base/'pdf-clean-runtime'
if target.exists():raise SystemExit('Clean runtime already exists; inspect instead of overwriting')
target.mkdir()
core=[]
for path in source.iterdir():
    if path.is_file() and (path.name in ('python.exe','pythonw.exe','python312.zip','LICENSE.txt') or path.suffix.lower() in ('.dll','.pyd')):
        shutil.copy2(path,target/path.name);core.append({'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
(target/'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../../app\nimport site\n','utf-8')
site=target/'Lib/site-packages';site.mkdir(parents=True)
lock=json.loads((root/'config/pdf-wheelhouse.json').read_text('utf-8'));installed=[]
for wheel in lock['wheels']:
    path=base/'wheelhouse'/wheel['file']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==wheel['sha256'],path
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():continue
            rel=PurePosixPath(member.filename)
            if rel.is_absolute() or '..' in rel.parts:raise ValueError('Invalid wheel path')
            parts=rel.parts
            if parts[0].endswith('.data'):
                directory={'purelib':site,'platlib':site,'scripts':target/'Scripts','data':target,'headers':target/'Include'}.get(parts[1])
                if directory is None:raise ValueError('Unknown wheel installation scheme')
                destination=directory.joinpath(*parts[2:])
            else:destination=site.joinpath(*parts)
            destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(archive.read(member))
    installed.append(wheel)
(base/'pdf-clean-runtime-build.json').write_text(json.dumps({'core':core,'wheels':installed,'python_packages_from_previous_runtime':False},indent=2),'utf-8')
print(target,len(installed),'locked wheels',len(core),'CPython core files')
