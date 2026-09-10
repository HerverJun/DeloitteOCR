"""Index upstream license files without replacing their original contents."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

p=argparse.ArgumentParser()
p.add_argument('--bundle',type=Path,required=True)
p.add_argument('--llama-source',type=Path,required=True)
a=p.parse_args()
dest=a.bundle/'licenses'
dest.mkdir(exist_ok=True)
shutil.copy2(a.llama_source/'LICENSE',dest/'llama.cpp-LICENSE')
records=[]
for top in ['runtimes','models','licenses']:
    for path in sorted((a.bundle/top).rglob('*')):
        if path.is_file() and any(word in path.name.upper() for word in ['LICENSE','NOTICE','COPYING','EULA']):
            with path.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            records.append({'path':path.relative_to(a.bundle).as_posix(),'sha256':digest})
(dest/'index.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Indexed {len(records)} upstream license/notice files')
