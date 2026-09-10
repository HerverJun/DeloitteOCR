import argparse
import json
from pathlib import Path
import subprocess
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.markers import default_environment

p=argparse.ArgumentParser()
p.add_argument('--bundle',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
root_requirements={
    'control':[], 'ppocr':['paddlepaddle-gpu','paddleocr[doc-parser]','openpyxl','pillow-heif'],
    'paddlevl':['paddlepaddle-gpu','paddleocr[doc-parser]','openpyxl','pillow-heif'],
    'glm':['glmocr[selfhosted]','transformers','torchvision','openpyxl','pillow-heif'],
    'hunyuan':['Pillow','pillow-heif','openpyxl']}
if (a.bundle/'runtimes/service/python.exe').exists():
    root_requirements['service']=['fastapi','uvicorn','python-multipart','Pillow','pillow-heif','openpyxl','numpy','opencv-python-headless']
code='import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]:{"version":d.version,"requires":d.requires or []} for d in m.distributions()}))'
reports=[]
for runtime,roots in root_requirements.items():
    exe=a.bundle/'runtimes'/runtime/'python.exe'
    data=json.loads(subprocess.check_output([str(exe),'-I','-c',code],encoding='utf-8'))
    installed={canonicalize_name(k):v for k,v in data.items()}
    seen=set();queue=list(map(Requirement,roots));errors=[]
    while queue:
        req=queue.pop();name=canonicalize_name(req.name)
        if name not in installed:
            errors.append(f'Missing {req}');continue
        if req.specifier and not req.specifier.contains(installed[name]['version'],prereleases=True):
            errors.append(f'{req}: installed {installed[name]["version"]}')
        key=(name,tuple(sorted(req.extras)))
        if key in seen:continue
        seen.add(key)
        for text in installed[name]['requires']:
            child=Requirement(text)
            if child.marker is None or any(child.marker.evaluate({**default_environment(),'extra':extra}) for extra in ['',*req.extras]):
                queue.append(child)
    reports.append({'runtime':runtime,'status':'failed' if errors else 'passed','checked':len(seen),'errors':sorted(set(errors))})
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(reports,ensure_ascii=False))
raise SystemExit(any(r['errors'] for r in reports))
