"""Fetch public primary-source text for selection research; never fetch weights or run source code."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import hashlib, json, os, sys

ROOT = Path(__file__).resolve().parent
assert ROOT.resolve().drive.upper() == 'F:'
for key in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA'):
    assert Path(os.environ[key]).resolve().drive.upper() == 'F:', key
INDEX = ROOT / 'sources.json'
ledger = json.loads(INDEX.read_text(encoding='utf-8')) if INDEX.exists() else []

def fetch(item):
    key, url = item
    dest = ROOT / 'sources' / (key + '.txt')
    started = datetime.now(timezone.utc).isoformat()
    record = {'id': key, 'url': url, 'retrieved_at': started}
    try:
        req = Request(url, headers={'User-Agent': 'Clear-Manual-Tools-Research/1.0', 'Accept': 'application/vnd.github+json' if 'api.github.com' in url else '*/*'})
        with urlopen(req, timeout=24) as response:
            data = response.read(4_000_001)
            assert len(data) <= 4_000_000, 'Text response exceeds research limit'
            record.update(status=response.status, final_url=response.url, content_type=response.headers.get('Content-Type'), rate_remaining=response.headers.get('X-RateLimit-Remaining'))
        dest.write_bytes(data)
        record.update(snapshot=str(dest.relative_to(ROOT)).replace('\\','/'), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    except Exception as error:
        record.update(status=getattr(error, 'code', None), error=str(error))
    return record

def run(items):
    global ledger
    previous = {item['id']: item for item in ledger}
    items = [(key, url) for key, url in items if key not in previous or previous[key].get('status') != 200]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(fetch, item) for item in items]):
            record = future.result()
            previous[record['id']] = record
            print(json.dumps({key: record.get(key) for key in ('id','status','bytes','error')}, ensure_ascii=False), flush=True)
    ledger = sorted(previous.values(), key=lambda item: item['id'])
    INDEX.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

repos = {
 'trufor':'grip-unina/TruFor', 'mvss':'dong03/MVSS-Net', 'catnet':'mjkwon2021/CAT-Net',
 'imdl':'scut-dlvc/IMDLBenCo', 'deepfakebench':'SCLBD/DeepfakeBench',
 'insightface':'deepinsight/insightface', 'deepface':'serengil/deepface', 'opencvzoo':'opencv/opencv_zoo',
 'dlib':'davisking/dlib', 'compreface':'exadel-inc/CompreFace',
 'whisper':'openai/whisper', 'fasterwhisper':'SYSTRAN/faster-whisper', 'whispercpp':'ggml-org/whisper.cpp',
 'funasr':'modelscope/FunASR', 'pyannote':'pyannote/pyannote-audio', 'speechbrain':'speechbrain/speechbrain',
 'sherpa':'k2-fsa/sherpa-onnx', 'librosa':'librosa/librosa',
 'paddleocr':'PaddlePaddle/PaddleOCR', 'paddlex':'PaddlePaddle/PaddleX', 'mmocr':'open-mmlab/mmocr',
 'c2pa':'contentauth/c2pa-rs', 'audacity':'audacity/audacity', 'asvspoof':'asvspoof-challenge/2021',
 'ultralytics':'ultralytics/ultralytics'
}
if len(sys.argv) == 1 or sys.argv[1] == 'repos':
    run([(key+'-repo','https://api.github.com/repos/'+repo) for key,repo in repos.items()])
elif sys.argv[1] == 'readmes':
    items = []
    for key, repo in repos.items():
        path = ROOT / 'sources' / (key+'-repo.txt')
        if not path.exists(): continue
        data = json.loads(path.read_text(encoding='utf-8'))
        branch = data['default_branch']
        items.extend([(key+'-readme',f'https://raw.githubusercontent.com/{repo}/{branch}/README.md'), (key+'-license',f'https://raw.githubusercontent.com/{repo}/{branch}/LICENSE')])
    run(items)
elif sys.argv[1] == 'batch':
    run([(item['id'],item['url']) for item in json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))])
else:
    raise SystemExit('Expected repos, readmes or batch FILE')
