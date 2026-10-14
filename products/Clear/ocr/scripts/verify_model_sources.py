"""Compare local weights to upstream immutable-revision LFS SHA256."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import requests

p=argparse.ArgumentParser()
p.add_argument('--bundle',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()

def check(manifest):
    saved=json.loads(manifest.read_text(encoding='utf-8'))
    if manifest.parent.name=='HunyuanOCR-GGUF':
        return {'model':manifest.parent.name,'status':'derived','conversion':saved['conversion']}
    session=requests.Session();session.trust_env=False
    url=f'https://hf-mirror.com/api/models/{saved["repo"]}/revision/{saved["revision"]}?blobs=true'
    res=session.get(url,timeout=40);res.raise_for_status();info=res.json()
    checked=[]
    for file in info['siblings']:
        path=manifest.parent/file['rfilename']
        if not path.is_file() or not file.get('lfs'):
            continue
        with path.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=file['lfs']['sha256'] or path.stat().st_size!=file['lfs']['size']:
            raise ValueError(f'Upstream hash mismatch: {path}')
        checked.append({'file':file['rfilename'],'sha256':digest,'bytes':path.stat().st_size})
    if not checked:
        raise ValueError(f'No weights verified: {manifest.parent.name}')
    return {'model':manifest.parent.name,'repo':saved['repo'],'revision':saved['revision'],'status':'verified','files':checked}

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    records=list(pool.map(check,(a.bundle/'models').glob('*/source-manifest.json')))
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Verified {len(records)} model entries')
