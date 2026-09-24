"""Compare pinned/current metadata and summarize fetched evidence."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / 'sources'
old = json.loads((SOURCES/'glm-pinned.json').read_text('utf-8'))
new = json.loads((SOURCES/'glm-current.json').read_text('utf-8'))
before = {s['rfilename']:s for s in old['siblings']}
after = {s['rfilename']:s for s in new['siblings']}
changes = []
for name in sorted(before.keys() | after.keys()):
    if before.get(name) != after.get(name):
        changes.append({'file':name,'before':before.get(name),'after':after.get(name)})
result = {'pinned_revision':old['sha'],'upstream_revision':new['sha'],'changed_files':changes}
(ROOT/'glm-revision-comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps({'glm_revision_changes':result},ensure_ascii=False))
for name in ['paddlevl16','teleocr','ovisocr2','qwen38-27b','qwen35-9b','unlimited']:
    data = json.loads((SOURCES/(name+'-metadata.json')).read_text('utf-8'))
    card = data.get('cardData',{})
    weights = [s for s in data['siblings'] if s['rfilename'].endswith('.safetensors')]
    print(json.dumps({'name':name,'id':data['id'],'revision':data['sha'],
        'license':card.get('license'),'pipeline':data.get('pipeline_tag'),
        'safetensors':data.get('safetensors'),'weight_bytes':sum(s.get('size',0) for s in weights),
        'weight_files':[s['rfilename'] for s in weights]},ensure_ascii=False))
