"""Run actual engines, preserving individual evidence and failure exit codes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

p=argparse.ArgumentParser()
p.add_argument('--bundle',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--engines',nargs='+',default=['ppocr','paddlevl','glm','hunyuan'])
p.add_argument('--fixtures',nargs='+',default=['printed','table'])
p.add_argument('--cycles',type=int,default=1)
a=p.parse_args()
if a.output.exists() and any(a.output.iterdir()):
    raise SystemExit('Use a fresh evidence directory')
a.output.mkdir(parents=True,exist_ok=True)
control=a.bundle/'runtimes/control/python.exe'
records=[]
for cycle in range(a.cycles):
    for engine in a.engines:
        for fixture in a.fixtures:
            output=a.output/f'{cycle:02d}-{engine}-{fixture}'
            command=[str(control),'-X','utf8','-I','-m','ocr_workbench.cli','recognize','--engine',engine,
                     '--image',str(a.bundle/'fixtures'/f'{fixture}.png'),'--output',str(output)]
            started=time.monotonic()
            result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=960)
            record={'engine':engine,'fixture':fixture,'cycle':cycle,'exit_code':result.returncode,
                    'wall_seconds':time.monotonic()-started,'stdout':result.stdout,'stderr':result.stderr}
            if result.returncode==0:
                data=json.loads((output/'result.json').read_text(encoding='utf-8'))
                issues=[]
                if not data['text'].strip(): issues.append('Empty text')
                if fixture=='table' and engine!='ppocr' and not data['tables']: issues.append('No structured table')
                if '00123456789012345678' not in data['text']: issues.append('Long identifier mismatch')
                if (output/'network-blocked.log').exists(): issues.append('Attempted external Python network access')
                record.update(issues=issues,elapsed_seconds=data['elapsed_seconds'],
                              load_seconds=data['load_seconds'],blocks=len(data['blocks']),tables=len(data['tables']))
            record['passed']=result.returncode==0 and not record.get('issues')
            records.append(record)
            summary={'created_at':datetime.now(timezone.utc).isoformat(),'bundle':str(a.bundle.resolve()),
                     'offline_evidence':'Python external sockets blocked; model offline flags; llama.cpp --offline. Not OS disconnection.',
                     'passed':all(r['passed'] for r in records),'records':records}
            (a.output/'acceptance.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(record,ensure_ascii=False),flush=True)
raise SystemExit(not all(r['passed'] for r in records))
