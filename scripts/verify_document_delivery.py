"""Final fail-closed gate before declaring delivery complete."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p=argparse.ArgumentParser();p.add_argument('--delivery',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1];delivery=a.delivery.resolve()
read=lambda p:json.loads(p.read_text('utf-8'))
checks=[]
for name in ('archive-verification.json','tested-components.json','source-verification.json',
             'application-full-02/application-audit.json','document-acceptance/document-acceptance.json'):
    report=read(delivery/name);assert report['passed'],name
    checks.append({'path':name,'sha256':hashlib.sha256((delivery/name).read_bytes()).hexdigest(),'passed':True})
audit=root/'audit/document-workflow-20260913'
for name in ('asset-lock-verification.json','unchanged-ocr-runtimes.json','pdf-final-isolated-visual-verification.json','final-mapping-replay.json'):
    assert read(audit/name)['passed'],name
ui=read(audit/'ui-08/ui-results.json');assert len(ui['checks'])==9 and all(c['passed'] for c in ui['checks']) and not ui['errors'] and ui['browser_closed']
for name in ('full-python-final.txt','final-geometry-tests.txt'):
    assert (audit/name).read_text('utf-8-sig').rstrip().endswith('OK'),name
inventory={r['path']:r for r in read(delivery/'bundle/manifest.json')['files']}
sources=[]
for source in (root/'src').rglob('*.py'):
    relative='app/'+source.relative_to(root/'src').as_posix()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==inventory[relative]['sha256'],relative
    sources.append(relative)
git=lambda *args:subprocess.check_output(['git','-C',str(root),*args]).decode().strip()
assert not git('status','--porcelain'),'Source working tree changed after archive'
assert read(delivery/'source-verification.json')['commit']==git('rev-parse','HEAD')
report={'passed':True,'version':'0.9.0rc1','source_commit':git('rev-parse','HEAD'),'app_source_files':len(sources),'checks':checks,
        'geometry_status':'experimental; frozen precise coverage 0.6702%, wrong-cell rate 1.3393%; target not met',
        'not_measured':['A4000','independent Windows','intranet real samples','human correction median/P90/time saved'],
        'scope':'Verifies completed receipts and code lineage; shutdown is a separate user-authorized operating-system action.'}
(delivery/'delivery-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps(report,ensure_ascii=False))
