"""Bind model/font/wheel locks and preserved original models to the tested inventory."""
import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
read=lambda p:json.loads(p.read_text('utf-8'))
inventory={r['path']:r for r in read(Path('E:/OCR-document-workflow-20260913/tested-manifest.json'))['files']}
checks=[]
def check(path,sha,size):
    r=inventory.get(path);assert r and r['sha256']==sha and r['bytes']==size,path
    checks.append(path)
for name,model in read(root/'config/table-model-lock.json').items():
    for r in model['files']:check('models/'+name+'/'+r['name'],r['sha256'],r['bytes'])
for r in read(root/'config/pdf-font-lock.json')['files']:check('fonts/'+r['file'],r['sha256'],r['bytes'])
for r in read(root/'config/pdf-wheelhouse.json')['wheels']:check('wheelhouse/pdf/'+r['file'],r['sha256'],r['bytes'])
old=[r for r in read(Path('E:/OCR-fusion-20260912/bundle/manifest.json'))['files'] if r['path'].startswith('models/')]
for r in old:check(r['path'],r['sha256'],r['bytes'])
report={'passed':True,'scope':'Declared model/font/wheel locks match independently computed tested SHA inventory; all prior model bytes preserved','new_lock_files':len(checks)-len(old),'prior_model_files':len(old),'paths':checks}
(root/'audit/document-workflow-20260913/asset-lock-verification.json').write_text(json.dumps(report,indent=2),'utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='paths'}))
