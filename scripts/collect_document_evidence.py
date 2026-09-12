"""Select distributable audit evidence; never copy research images/transcriptions."""
import json
from pathlib import Path
import shutil
root=Path(__file__).resolve().parents[1];base=root/'build/document-workflow'
source=root/'audit/document-workflow-20260913';out=base/'delivery-evidence';out.mkdir(parents=True,exist_ok=True)
for p in source.iterdir():
    if p.is_file():shutil.copy2(p,out/p.name)
for folder in ('ui-07','ui-08','pdf-visuals'):
    dest=out/folder;dest.mkdir(exist_ok=True)
    for p in (source/folder).iterdir():
        if p.is_file():shutil.copy2(p,dest/p.name)
for origin,name in [('geometry-evaluation/metrics.json','geometry-metrics.json'),('wtw-evaluation/metrics.json','wtw-metrics.json'),('recovery-audit/report.json','recovery-report.json'),('capacity-audit/report.json','capacity-report.json'),('gpu-audit/report.json','gpu-report.json'),('geometry-evaluation/run-lock.json','geometry-run-lock.json'),('geometry-evaluation/rapidtable/run-lock.json','rapid-run-lock.json')]:
    shutil.copy2(base/origin,out/name)
    shutil.copy2(base/origin,source/name)
manifest=json.loads((base/'geometry-holdout/frozen/manifest.json').read_text('utf-8'))
summary={k:v for k,v in manifest.items() if k!='samples'}
summary['samples']=[{k:v for k,v in s.items() if k not in ('fixed_edit','targets')}|{'target_count':len(s['targets'])} for s in manifest['samples']]
(out/'frozen-inventory-without-transcriptions.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
shutil.copy2(base/'geometry-holdout/frozen/manifest.sha256',out/'frozen-manifest.sha256')
print(out)
