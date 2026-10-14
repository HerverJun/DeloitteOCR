"""Inventory exact release bytes before application testing, without creating a ZIP."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time

p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
root=a.bundle.resolve();started=time.monotonic()
def record(path):
    with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'path':path.relative_to(root).as_posix(),'bytes':path.stat().st_size,'sha256':digest}
paths=[p for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.relative_to(root).parts and p.relative_to(root).parts[0] not in {'cache','runs','results'} and p.relative_to(root).as_posix()!='manifest.json']
with ThreadPoolExecutor(max_workers=8) as pool: records=list(pool.map(record,paths))
data=json.dumps({'schema_version':1,'files':records},ensure_ascii=False,indent=2).encode('utf-8')
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_bytes(data)
print(json.dumps({'files':len(records),'bytes':sum(r['bytes'] for r in records),'seconds':time.monotonic()-started,'sha256':hashlib.sha256(data).hexdigest()}))
