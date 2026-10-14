"""One isolated local service and external Edge; deterministic interaction only."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'tests')]
from PIL import Image, ImageDraw, ImageFont
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.service import create_app
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.structure_store import record_candidates, refresh_proposals
from ocr_workbench.tables import parse_tables
from ocr_workbench.editing import tables_html


def table(rows):
    return {'rows':len(rows),'columns':len(rows[0]),'cells':[
        {'row':r,'column':c,'row_span':1,'column_span':1,'text':value}
        for r,row in enumerate(rows) for c,value in enumerate(row)]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--bundle',type=Path,required=True)
    a = p.parse_args()
    out = a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    token = 'isolated-structure-workflow-audit'
    app = create_app(ROOT,out/'workspace',token,start_queue=False)
    manager,store = app.state.documents,app.state.store
    manager.cpu.runtime = a.bundle/'runtimes/pdf/python.exe'
    project = store.project('结构复核实验')
    pages = []
    values = [['Item','Amount'],['Revenue','001.00'],['Tax','-0.01']]
    for n in range(2):
        image = Image.new('RGB',(700,420),'white')
        draw = ImageDraw.Draw(image);font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',25)
        draw.text((35,20),f'Structure review fixture / Page {n+1}',font=font,fill='#213b2c')
        for r,values_row in enumerate(values):
            for c,text in enumerate(values_row):
                box=[40+c*300,85+r*90,40+(c+1)*300,85+(r+1)*90]
                draw.rectangle(box,outline='#365e42',width=2)
                draw.text((box[0]+12,box[1]+27),text,font=font,fill='#1d2b22')
        pages.append(image)
    pdf=out/'synthetic-review.pdf';pages[0].save(pdf,save_all=True,append_images=pages[1:],resolution=72.)
    doc=manager.import_document(project['id'],'结构流程演示.pdf',pdf,dpi=72)
    seed_pages=[]
    for page in store.document_pages(doc['id']):
        manager.process(page['id'],'native')
        manager.step()
        current=store.one('pages',page['id'])
        photo=store.one('images',current['image_id']);version=store.one('versions',photo['active_version'])
        task=store.enqueue(project['id'],[version['id']],['paddlevl'])[0];store.claim()
        original=table(values[:2]);html=tables_html([original]);original['source']=parse_tables(html)[0]['source']
        store.complete(task,{'engine':'paddlevl','text':html,'tables':[original],
            'blocks':[{'kind':'table','text':html,'polygon':box_polygon([40,85,640,355])}],
            'image':{'width':700,'height':420},'project_image_version':version['id']})
        result=store.one('tasks',task)['result_id']
        cells,blocks=[],[]
        for r,row in enumerate(values):
            for c,text in enumerate(row):
                box=[40+c*300,85+r*90,40+(c+1)*300,85+(r+1)*90]
                cells.append({'cell_id':str(len(cells)),'row_id':r,'column_id':c,'bbox':box,'rowspan_val':1,'colspan_val':1})
                blocks.append({'id':f'p{page["page_number"]}-token-{r}-{c}','text':text,'kind':'text','granularity':'word',
                    'polygon':box_polygon([box[0]+12,box[1]+27,box[0]+260,box[1]+60])})
        prediction={'tf_table_cells':cells,'model_sha256':'synthetic-ui-only','source_semantics':'raw_structure'}
        with store.transaction() as db:
            db.execute('UPDATE selections SET result_id=? WHERE image_id=?',(result,photo['id']))
            record_candidates(db,result,version,prediction,blocks)
        refresh_proposals(store,result,0)
        seed_pages.append({'page':page['page_number'],'result':result,'image':photo['id'],'version':version['id']})
    app.state.queue.status=lambda:{'healthy':True,'alive':True,'state':'running','task_id':None,'engine':None,'loaded':False}
    app.state.fusion_queue.status=app.state.queue.status
    app.router.routes[:]=[r for r in app.router.routes if not (getattr(r,'path',None)=='' and type(r).__name__=='Mount')]
    app.mount('/',StaticFiles(directory=ROOT/'frontend/dist',html=True))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='warning',access_log=False))
    worker=threading.Thread(target=server.run);worker.start()
    deadline=time.monotonic()+15
    while not server.started:
        if time.monotonic()>deadline:raise TimeoutError('UI startup failed')
        time.sleep(.03)
    seed={'base':'http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1]),'token':token,'project':project['id'],
        'document':doc['id'],'pages':seed_pages,'scope':'Synthetic OCR/structure and raster PDF for interaction only; no accuracy/human efficiency claim'}
    (out/'seed.json').write_text(json.dumps(seed,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        completed=subprocess.run(['node',str(ROOT/'scripts/audit_structure_ui.mjs'),str(out)],cwd=ROOT)
        (out/'review-timings.json').write_text(json.dumps(store.rows('SELECT * FROM review_timings'),ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        server.should_exit=True;worker.join(timeout=15);manager.stop()
    if worker.is_alive():raise RuntimeError('UI service did not stop')
    sys.exit(completed.returncode)


if __name__=='__main__':main()
