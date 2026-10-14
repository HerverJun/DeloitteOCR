"""Record local runtime, locked dependencies and an isolated schema-12 database.

Read-only for installed bundles; never opens a user's project or credentials.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess

ROOT=Path(__file__).resolve().parents[2]


def probe(executable):
    code='''import sys,json,importlib.metadata as m
packages={}
for name in ("pydantic","fastapi","httpx","Pillow","pillow-heif","openpyxl","fpdf2","pypdfium2","pikepdf"):
    try: packages[name]=m.version(name)
    except m.PackageNotFoundError: packages[name]=None
print(json.dumps({"python":sys.version,"executable":sys.executable,"packages":packages}))'''
    result=subprocess.run([str(executable),'-c',code],capture_output=True,text=True,check=True)
    return json.loads(result.stdout)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-runtime',type=Path,required=True,help='Existing locked service python.exe')
    parser.add_argument('--pdf-runtime',type=Path,required=True,help='Existing PDF python.exe')
    parser.add_argument('--build',type=Path,required=True,help='Isolated run build root')
    parser.add_argument('--audit',type=Path,required=True,help='Audit receipt directory')
    args=parser.parse_args();args.build.mkdir(parents=True,exist_ok=True);args.audit.mkdir(parents=True,exist_ok=True)
    workspace=(args.build/'baseline-workspace').resolve()
    if (workspace/'workbench.sqlite3').exists(): raise ValueError('Baseline database already exists; preserve it and use a new build directory')
    code='''import sys,json,sqlite3
from contextlib import closing
sys.path.insert(0,sys.argv[1])
from ocr_workbench.store import Store
store=Store(sys.argv[2])
with closing(sqlite3.connect(str(store.root/"workbench.sqlite3"))) as db:
    print(json.dumps({"schema":db.execute("PRAGMA user_version").fetchone()[0],"integrity":db.execute("PRAGMA integrity_check").fetchone()[0],"foreign_key_errors":db.execute("PRAGMA foreign_key_check").fetchall(),"tables":[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]}))'''
    db=subprocess.run([str(args.service_runtime),'-c',code,str(ROOT/'src'),str(workspace)],capture_output=True,text=True,check=True)
    bundles=[]
    for path in (args.service_runtime.resolve().parents[2],args.pdf_runtime.resolve().parents[2]):
        if str(path) in [b['root'] for b in bundles]:continue
        bundles.append({'root':str(path),'runtimes':[str(p.relative_to(path)) for p in sorted((path/'runtimes').glob('*/python.exe'))],
                        'model_locations':[str(p) for p in (path/'models',path/'engine-packages') if p.exists()],
                        'archive_locations':[str(p) for p in sorted(path.parent.glob('*.zip'))],
                        'inference':'not_tested'})
    pins=dict(line.split('==',1) for line in (ROOT/'config/runtime-locks/service.txt').read_text('utf-8').splitlines() if '==' in line)
    service=probe(args.service_runtime)
    mismatch={k:{'installed':v,'locked':pins.get(k)} for k,v in service['packages'].items() if k in pins and v!=pins[k]}
    receipt={'created_utc':datetime.now(timezone.utc).isoformat(),'platform':platform.platform(),'service':service,'pdf':probe(args.pdf_runtime),'service_lock_mismatches':mismatch,'bundles':bundles,
             'database':{**json.loads(db.stdout),'path':str(workspace/'workbench.sqlite3'),'sha256':hashlib.sha256((workspace/'workbench.sqlite3').read_bytes()).hexdigest(),'contains_user_data':False},
             'tool_source':str(ROOT/'build/public-quality-20260916/tool/site-packages'),
             'limitations':['No live user DB inventory or migration performed','No GPU inference or real provider request performed','Source snapshot does not include ignored relative build dependencies']}
    (args.audit/'runtime-baseline.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps({'schema':receipt['database']['schema'],'integrity':receipt['database']['integrity'],'lock_mismatches':mismatch}))


if __name__=='__main__':main()
