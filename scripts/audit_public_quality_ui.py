"""External Edge review of saved public empty-region outcomes, no inference."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.service import create_app


def main():
    p=argparse.ArgumentParser()
    for name in ('data','output','bundle'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    shutil.copytree(args.data/'workspace',out/'workspace',ignore=shutil.ignore_patterns('engine-sessions','pdf-ipc','__pycache__'))
    token='public-quality-ui-local'
    app=create_app(ROOT,out/'workspace',token,start_queue=False)
    store=app.state.store;app.state.documents.cpu.runtime=args.bundle/'runtimes/pdf/python.exe'
    result=json.loads((args.data/'summary.json').read_text('utf-8'))['rows'][0]['result_id']
    page=store.rows('SELECT p.* FROM pages p JOIN tasks t ON t.image_id=p.image_id JOIN results r ON r.task_id=t.id WHERE r.id=?',(result,))[0]
    app.state.queue.status=lambda:{'healthy':True,'alive':True,'state':'running','task_id':None,'engine':None,'loaded':False}
    app.state.fusion_queue.status=app.state.queue.status
    app.router.routes[:]=[r for r in app.router.routes if not (getattr(r,'path',None)=='' and type(r).__name__=='Mount')]
    app.mount('/',StaticFiles(directory=ROOT/'frontend/dist',html=True))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='warning',access_log=False))
    worker=threading.Thread(target=server.run);worker.start()
    deadline=time.monotonic()+15
    while not server.started:
        if time.monotonic()>deadline:raise TimeoutError('UI service startup')
        time.sleep(.03)
    seed={'base':'http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1]),'token':token,
          'document':page['document_id'],'result':result,'page':page['id']}
    (out/'seed.json').write_text(json.dumps(seed),encoding='utf-8')
    try:
        run=subprocess.run(['node',str(ROOT/'scripts/audit_public_quality_ui.mjs'),str(out)],cwd=ROOT)
    finally:
        server.should_exit=True;worker.join(timeout=15);app.state.documents.stop()
    if worker.is_alive():raise RuntimeError('UI service did not close')
    raise SystemExit(run.returncode)


if __name__=='__main__':main()
