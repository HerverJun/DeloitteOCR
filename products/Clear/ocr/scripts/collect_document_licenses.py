"""Build-time receipts and exact upstream sources; no production network calls."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import zipfile
import requests
root=Path(__file__).resolve().parents[1];out=root/'licenses/document-workflow';out.mkdir(parents=True,exist_ok=True)
upstreams=[
 ('paddlex','PaddlePaddle/PaddleX','v3.7.0',['LICENSE','paddlex/inference/pipelines/table_recognition/pipeline_v2.py']),
 ('rapidtable','RapidAI/RapidTable','22592283c1f9d7c5a014c96c4ecc57de0b3ebfce',['LICENSE','rapid_table/main.py']),
 ('docling','docling-project/docling','5ea6490ffdc57b2fd7de5cc436f2d0a22f2214d4',['LICENSE','docling/models/base_ocr_model.py']),
 ('docling-parse','docling-project/docling-parse','1fab490e4e59a39d70f375cd5092f4784036fa93',['LICENSE','docling_parse/pdf_parser.py']),
 ('ocrmypdf','ocrmypdf/OCRmyPDF','ffee83231532f4f67b8c0e756cddec67446570c9',['LICENSE','src/ocrmypdf/_graft.py']),
 ('wtw','wangwen-whu/WTW-Dataset','master',['README.md','LICENSE','License']),
]
def fetch(job):
    name,repo,ref,path=job;target=out/'upstream'/name/path;target.parent.mkdir(parents=True,exist_ok=True)
    url=f'https://raw.githubusercontent.com/{repo}/{ref}/{path}'
    record={'project':name,'repository':repo,'revision':ref,'upstream_path':path,'url':url}
    session=requests.Session();session.trust_env=False
    try:
        response=session.get(url,timeout=(15,35))
        if response.status_code!=200:
            fallback=f'https://api.github.com/repos/{repo}/contents/{path}?ref={ref}'
            response=session.get(fallback,timeout=(15,35));response.raise_for_status()
            data=base64.b64decode(response.json()['content'])
        else:data=response.content
        target.write_bytes(data);record.update(path=target.relative_to(root).as_posix(),sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),status='saved')
    except Exception as error:record.update(status='unavailable',error=str(error))
    return record
with ThreadPoolExecutor(max_workers=4) as pool:receipts=list(pool.map(fetch,[(name,repo,ref,path) for name,repo,ref,paths in upstreams for path in paths]))
for wheel in sorted((root/'build/document-workflow/wheelhouse').glob('*.whl')):
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith('/'):continue
            if '.dist-info/' in name and (name.endswith('/METADATA') or '/licenses/' in name or Path(name).name.lower().startswith(('license','copying','notice'))):
                data=archive.read(name);target=out/'pdf-runtime'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
                receipts.append({'project':'pdf-wheelhouse','wheel':wheel.name,'source_path':name,'path':target.relative_to(root).as_posix(),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'status':'saved'})
(out/'receipts.json').write_text(json.dumps(receipts,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps([r for r in receipts if r['project']!='pdf-wheelhouse'],indent=2))
