import argparse
import json
from pathlib import Path
import subprocess
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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
    root_requirements['service']=['fastapi','uvicorn','python-multipart','Pillow','pillow-heif','openpyxl','numpy','opencv-python-headless','psutil']
code='''import importlib.metadata as m,json,os,platform,sys
print(json.dumps({"distributions":{d.metadata["Name"]:{"version":d.version,"requires":d.requires or []} for d in m.distributions()},"marker_environment":{"implementation_name":sys.implementation.name,"implementation_version":platform.python_version(),"os_name":os.name,"platform_machine":platform.machine(),"platform_release":platform.release(),"platform_system":platform.system(),"platform_version":platform.version(),"python_full_version":platform.python_version(),"platform_python_implementation":platform.python_implementation(),"python_version":".".join(map(str,sys.version_info[:2])),"sys_platform":sys.platform}}))'''
reports=[]
for runtime,roots in root_requirements.items():
    exe=a.bundle/'runtimes'/runtime/'python.exe'
    data=json.loads(subprocess.check_output([str(exe),'-I','-c',code],encoding='utf-8'))
    installed={canonicalize_name(k):v for k,v in data['distributions'].items()}
    environment=data['marker_environment']
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
            if child.marker is None or any(child.marker.evaluate({**environment,'extra':extra}) for extra in ['',*req.extras]):
                queue.append(child)
    reports.append({'runtime':runtime,'status':'failed' if errors else 'passed','marker_environment':environment,'checked':len(seen),'errors':sorted(set(errors))})
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(reports,ensure_ascii=False))
raise SystemExit(any(r['errors'] for r in reports))
