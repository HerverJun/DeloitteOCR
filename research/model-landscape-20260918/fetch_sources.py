"""Retrieve public model metadata and official project READMEs; no weights."""
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / 'sources'
SOURCES.mkdir(exist_ok=True)
queries = [
    ('paddle-models', 'PaddlePaddle', 'OCR'), ('glm-models', 'zai-org', 'OCR'),
    ('hunyuan-models', 'tencent', 'HunyuanOCR'), ('mineru-models', 'opendatalab', 'MinerU'),
    ('dots-models', 'rednote-hilab', 'dots'), ('deepseek-models', 'deepseek-ai', 'OCR'),
    ('qwen35-models', 'Qwen', 'Qwen3.5'), ('qwen36-models', 'Qwen', 'Qwen3.6'),
    ('olmocr-models', 'allenai', 'olmOCR'), ('granite-models', 'ibm-granite', 'docling'),
    ('chandra-models', 'datalab-to', 'chandra'), ('surya-models', 'datalab-to', 'surya')]
repos = [
    ('paddle', 'PaddlePaddle/PaddleOCR'), ('glm', 'zai-org/GLM-OCR'),
    ('hunyuan', 'Tencent-Hunyuan/HunyuanOCR'), ('mineru', 'opendatalab/MinerU'),
    ('omnidocbench', 'opendatalab/OmniDocBench'), ('deepseek', 'deepseek-ai/DeepSeek-OCR-2'),
    ('dots', 'rednote-hilab/dots.ocr'), ('olmocr', 'allenai/olmocr'),
    ('surya', 'datalab-to/surya'), ('chandra', 'datalab-to/chandra'),
    ('qwen36', 'QwenLM/Qwen3.6')]
jobs = []
for name, author, search in queries:
    url = 'https://huggingface.co/api/models?' + urllib.parse.urlencode(
        {'author': author, 'search': search, 'limit': 100, 'sort': 'lastModified', 'direction': -1, 'full': 'true'})
    jobs.append((name, url, 'models'))
for name, repo in repos:
    jobs.append((name + '-readme', 'https://api.github.com/repos/' + repo + '/readme', 'readme'))

def fetch(job):
    name, url, kind = job
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'OCR-workbench-research', 'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=40) as response:
            data = response.read()
            resolved = response.url
        (SOURCES / (name + '.json')).write_bytes(data)
        parsed = json.loads(data)
        if kind == 'readme':
            content = base64.b64decode(parsed['content'])
            (SOURCES / (name + '.md')).write_bytes(content)
            detail = {'bytes': len(content), 'url': parsed.get('html_url'), 'git_blob_sha': parsed.get('sha')}
        else:
            detail = [{'id': item['id'], 'sha': item.get('sha'), 'lastModified': item.get('lastModified'),
                       'createdAt': item.get('createdAt')} for item in parsed]
        return {'name': name, 'url': url, 'resolved_url': resolved, 'retrieved_utc': datetime.now(timezone.utc).isoformat(),
                'sha256': hashlib.sha256(data).hexdigest(), 'ok': True, 'detail': detail}
    except Exception as error:
        return {'name': name, 'url': url, 'ok': False, 'error': str(error)}

results = []
with ThreadPoolExecutor(max_workers=6) as pool:
    for future in as_completed([pool.submit(fetch, job) for job in jobs]):
        result = future.result()
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
(ROOT / 'retrieval.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), 'utf-8')
