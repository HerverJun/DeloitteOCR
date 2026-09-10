"""App-local MSVC DLLs from Microsoft's redistributable directory, not System32."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

p=argparse.ArgumentParser()
p.add_argument('--bundle',type=Path,required=True)
p.add_argument('--redist',type=Path,required=True)
a=p.parse_args()
origin=a.redist.resolve()
if 'Redist' not in origin.parts or origin.name not in {'Microsoft.VC143.CRT','Microsoft.VC145.CRT'} or origin.parent.name!='x64':
    raise ValueError('Expected the official MSVC x64 CRT redistributable directory')
files=list(origin.glob('*.dll'))
if not files or not (origin/'msvcp140.dll').is_file():
    raise ValueError('Incomplete Microsoft CRT redistributable')
record=[]
for file in files:
    digest=hashlib.file_digest(file.open('rb'),'sha256').hexdigest()
    for runtime in (a.bundle/'runtimes').iterdir():
        if runtime.is_dir():
            shutil.copy2(file,runtime/file.name)
    record.append({'file':file.name,'sha256':digest})
(a.bundle/'config/msvc-source.json').write_text(json.dumps({'source':str(origin),'files':record},indent=2),encoding='utf-8')
