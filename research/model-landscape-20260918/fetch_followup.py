"""Retrieve focused public cards and metadata, without downloading model weights."""
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / 'sources'
JOBS = []
for name, repo in [('paddlevl16','PaddlePaddle/PaddleOCR-VL-1.6'),
                   ('teleocr','StarDoc-AI/TeleOCR'), ('ovisocr2','ATH-MaaS/OvisOCR2'),
                   ('qwen38-27b','Qwen/Qwen3.8-27B'), ('qwen35-9b','Qwen/Qwen3.5-9B'),
                   ('unlimited','baidu/Unlimited-OCR')]:
    JOBS.append((name+'-metadata', f'https://huggingface.co/api/models/{repo}?blobs=true', 'json'))
    JOBS.append((name+'-card', f'https://huggingface.co/{repo}/raw/main/README.md', 'text'))
for name, revision in [('glm-pinned','ca5d8b3e287e52589e37c28385d9655ee4372f9d'),
                       ('glm-current','2e85a62840ccac27daa451df36c736c4636b8628')]:
    JOBS.append((name, f'https://huggingface.co/api/models/zai-org/GLM-OCR/revision/{revision}?blobs=true', 'json'))
for name, repo, path in [('teleocr-readme','caipeng328/NaviDC-OCR','README.md'),
                        ('hunyuan-llamacpp','Tencent-Hunyuan/HunyuanOCR','docs/llama_cpp.md'),
                        ('hunyuan-inference','Tencent-Hunyuan/HunyuanOCR','docs/inference/inference.md')]:
    JOBS.append((name, f'https://api.github.com/repos/{repo}/contents/{path}', 'github'))
JOBS.append(('qwen38-config','https://huggingface.co/Qwen/Qwen3.8-27B/raw/main/config.json','json'))

def fetch(job):
    name, url, kind = job
    try:
        request = urllib.request.Request(url, headers={'User-Agent':'OCR-workbench-research'})
        with urllib.request.urlopen(request, timeout=40) as response:
            data, resolved = response.read(), response.url
        extension = '.md' if kind == 'text' else '.json'
        (SOURCES / (name + extension)).write_bytes(data)
        detail = {}
        if kind == 'github':
            parsed = json.loads(data)
            (SOURCES / (name+'.md')).write_bytes(base64.b64decode(parsed['content']))
            detail = {'url':parsed.get('html_url'),'git_blob_sha':parsed.get('sha')}
        elif kind == 'json':
            parsed = json.loads(data)
            detail = {key:parsed.get(key) for key in ('id','sha','lastModified','architectures') if key in parsed}
        return {'name':name,'url':url,'resolved_url':resolved,'retrieved_utc':datetime.now(timezone.utc).isoformat(),
                'ok':True,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'detail':detail}
    except Exception as error:
        return {'name':name,'url':url,'ok':False,'error':str(error)}

results=[]
with ThreadPoolExecutor(max_workers=6) as pool:
    for future in as_completed([pool.submit(fetch,job) for job in JOBS]):
        result=future.result(); results.append(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
(ROOT/'retrieval-followup.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf-8')
