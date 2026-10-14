"""Review the saved research artifacts; no network calls, imports of candidates or inference."""
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote
import hashlib, json, re

root=Path(__file__).resolve().parent
assert root.resolve().drive.upper()=='F:'
index=root/'sources.json'
records=json.loads(index.read_text(encoding='utf-8'))
by_id={item['id']:item for item in records}
verified=[]
for item in records:
    if 'snapshot' not in item: continue
    path=(root/item['snapshot']).resolve()
    assert path.is_relative_to(root.resolve()) and path.drive.upper()=='F:'
    content=path.read_bytes()
    assert len(content)==item['bytes'],item['id']
    assert hashlib.sha256(content).hexdigest()==item['sha256'],item['id']
    verified.append(item['id'])
for key in ('aliyun-seal','aliyun-stamp'):
    by_id[key]['usable_for_claims']=False
    by_id[key]['content_assessment']='HTTP 200 but body is the provider 404 error page; excluded from findings'
index.write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
required=[
 'catnet-readme','trufor-license-package','mesorch-readme','mesorch-license','segformer-license',
 'doctamper-readme','ostf-readme','imdl-license','forensichub-license-master','deepfakebench-license',
 'nist-mfc','c2pa-explainer','c2pa-license-mit','c2pa-license-apache',
 'opencv-yunet','opencv-yunet-license','opencv-sface','opencv-sface-license','opencv-sface-lfs','opencv-current-yunet-lfs',
 'insightface-readme','deepface-readme','dlib-license-txt','dlib-pretrained-models','compreface-readme',
 'nist-frte','nist-frte-1n','nist-pad-results','nist-fate','faceme-sdk',
 'funasr-license','funasr-model-license','funasr-paraformer-model','funasr-nano-model-card','campplus-model-card',
 'fsmn-model-card','ct-punc-model-card','funasr-sensevoice-model','fasterwhisper-readme','whisper-readme',
 'whispercpp-license','sherpa-license','pyannote-community-card','speechbrain-ecapa-model','aasist-license',
 'asvspoof-readme','whisperx-repo','librosa-license-md','ffmpeg-legal',
 'paddleocr-license','paddle-seal-module','paddle-seal-pipeline','seal-trocr-readme','seal-extract-license',
 'underseal-readme','underseal-license','ultralytics-license','mmocr-repo',
 'amped-authenticate','forensically','mever','atrain-readme','atrain-license','audacity-license-txt',
 'sonicvisualiser','izotope-rx','bce-seal','tencent-seal-sdk','aws-face'
]
for key in required:
    assert by_id[key]['status']==200 and key in verified and by_id[key].get('usable_for_claims',True),key
report=root/'四类扩展选型与竞品分析.md'
text=report.read_text(encoding='utf-8')
links=[]
for markdown in (report,root/'README.md'):
    for match in re.finditer(r'\]\(([^)]+)\)',markdown.read_text(encoding='utf-8')):
        value=unquote(match.group(1))
        if value.startswith(('http://','https://')): continue
        if value.startswith('/F:/'): path=Path(value[1:])
        else: path=markdown.parent/value
        assert path.exists(),str(path)
        links.append(str(path))
review={'passed':True,'reviewed_at':datetime.now(timezone.utc).isoformat(),
 'report':str(report),'report_sha256':hashlib.sha256(report.read_bytes()).hexdigest(),
 'sources_total':len(records),'saved_source_snapshots_verified':len(verified),
 'failed_fetches':len([item for item in records if item.get('status')!=200]),
 'semantically_excluded':['aliyun-seal','aliyun-stamp'],
 'core_primary_sources':required,'local_links_checked':len(links),
 'scope':'Public-source and existing-code research only; no model downloads, installations, inference, runtime changes or commercial product trials'}
(root/'evidence'/'research-review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({key:review[key] for key in ('passed','sources_total','saved_source_snapshots_verified','failed_fetches','local_links_checked','report_sha256')},ensure_ascii=False))
