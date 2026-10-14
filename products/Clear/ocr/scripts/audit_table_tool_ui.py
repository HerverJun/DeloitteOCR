"""Synthetic failure/retry UI check with one external Edge and local service."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.service import create_app

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True)
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    token='synthetic-tool-lifecycle-local'
    app=create_app(ROOT,out/'workspace',token,start_queue=False)
    manager=app.state.documents;manager.cpu.runtime=a.bundle/'runtimes/pdf/python.exe';manager.gpu=None
    store=app.state.store;project=store.project('通用工具恢复验证')
    doc=manager.import_document(project['id'],'合成原生页面.pdf',ROOT/'build/document-workflow/fixtures/native.pdf',dpi=72)
    page=store.document_pages(doc['id'])[0];manager.ensure_rendered(page['id'])
    stage=manager.process(page['id'],'native')
    with patch.object(manager.cpu,'call',side_effect=TimeoutError('模拟表格工具超时')):manager.step()
    result=store.result(json.loads(store.one('document_stages',stage)['output'])['result_id'])
    edit=result['edited'];edit['text']+='\n人工保留 0007'
    result=store.save(result['id'],edit,result['revision'])
    app.state.queue.status=lambda:{'healthy':True,'alive':True,'state':'running','task_id':None,'engine':None,'loaded':False}
    app.state.fusion_queue.status=app.state.queue.status
    app.router.routes[:]=[r for r in app.router.routes if not (getattr(r,'path',None)=='' and type(r).__name__=='Mount')]
    app.mount('/',StaticFiles(directory=ROOT/'frontend/dist',html=True))
    manager.start()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='warning',access_log=False))
    thread=threading.Thread(target=server.run);thread.start();deadline=time.monotonic()+15
    while not server.started:
        if time.monotonic()>deadline:raise TimeoutError('UI startup')
        time.sleep(.03)
    seed={'base':'http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1]),'token':token,
        'document':doc['id'],'result':result['id'],'before':result,'history':store.rows('SELECT * FROM edits')}
    # History includes bytes; the UI only needs stable content/revision.
    seed.pop('history')
    (out/'seed.json').write_text(json.dumps(seed,ensure_ascii=False),encoding='utf-8')
    try:
        run=subprocess.run(['node',str(ROOT/'scripts/audit_table_tool_ui.mjs'),str(out)],cwd=ROOT)
    finally:
        server.should_exit=True;thread.join(timeout=15);manager.stop()
    if thread.is_alive():raise RuntimeError('UI service did not close')
    raise SystemExit(run.returncode)

if __name__=='__main__':main()
