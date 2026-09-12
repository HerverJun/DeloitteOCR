"""Own one local service and external Chromium; seed actual PDF and synthetic OCR."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from PIL import Image,ImageDraw,ImageFont
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.service import create_app
from ocr_workbench.imaging import add_image
from ocr_workbench.tables import parse_tables
from ocr_workbench.fusion import default_policy
from ocr_workbench.geometry import enqueue_geometry,complete_geometry
p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
token='local-document-ui-fixture';app=create_app(a.bundle,out/'workspace',token,start_queue=False,review_only=True)
store=app.state.store;manager=app.state.documents;project=store.project('文档工作流验收')
source=out/'native-two-pages.pdf'
program="import pikepdf,sys\np=pikepdf.Pdf.new()\nwith pikepdf.open(sys.argv[1]) as s:\n for _ in range(2):p.pages.append(s.pages[0])\np.save(sys.argv[2])"
subprocess.run([str(a.bundle/'runtimes/pdf/python.exe'),'-I','-X','utf8','-c',program,str(root/'build/document-workflow/fixtures/native.pdf'),str(source)],check=True)
doc=manager.import_document(project['id'],source.name,source,dpi=100)
for page in store.document_pages(doc['id']):manager.process(page['id'],'native');manager.step()
encrypted=manager.import_document(project['id'],'encrypted.pdf',root/'build/document-workflow/fixtures/encrypted.pdf',dpi=72)
tiff=manager.import_document(project['id'],'multipage.tiff',root/'build/document-workflow/fixtures/multipage.tiff',dpi=72)
path=out/'review-table.png';img=Image.new('RGB',(900,600),'white');draw=ImageDraw.Draw(img);font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',25)
values=[['Item','Code','Amount'],['Alpha','00124','-12.50'],['Beta','00987','123.45']]
boxes=[]
for r,valuesrow in enumerate(values):
    for c,value in enumerate(valuesrow):
        box=[40+c*270,80+r*120,40+(c+1)*270,80+(r+1)*120];boxes.append(box)
        draw.rectangle(box,outline='#22352c',width=2);draw.text((box[0]+12,box[1]+40),value,fill='#17251f',font=font)
img.save(path);photo=add_image(store,project['id'],'review-table.png',path)
store.enqueue(project['id'],[photo['active_version']],['glm','hunyuan']);ids=[]
while task:=store.claim():
    value='00123' if task['engine']=='glm' else '00124'
    html='<table><tr><td>Item</td><td>Code</td><td>Amount</td></tr><tr><td>Alpha</td><td>'+value+'</td><td>-12.50</td></tr><tr><td>Beta</td><td>00987</td><td>123.45</td></tr></table>'
    store.complete(task['id'],{'engine':task['engine'],'text':html,'tables':parse_tables(html),
        'blocks':[{'kind':'table','text':html,'polygon':[[40,80],[850,80],[850,440],[40,440]]}],'project_image_version':photo['active_version']})
    ids.append(store.one('tasks',task['id'])['result_id'])
fusion=store.enqueue_fusion(project['id'],ids,default_policy('table'),'ui-geometry-fusion-fixture')[0]
app.state.fusion_queue.step();result=store.one('tasks',fusion)['result_id']
with store.transaction() as db:db.execute('UPDATE selections SET result_id=? WHERE image_id=?',(result,photo['id']))
raw=store.result(result)
enqueue_geometry(store,result,0);task=store.claim()
prediction={'model_revisions':{'test':'synthetic-ui-only'},'inference_seconds':.01,'tables':[
    {'table_box':[0,0,900,600],'final':{'pred_html':raw['edited']['text'],'cell_box_list':boxes},
     'raw':{'det':{'boxes':[{'score':1,'coordinate':box} for box in boxes]}}}]}
complete_geometry(store,task,prediction,store.root/'ui-synthetic-geometry.json')
app.router.routes[:]=[r for r in app.router.routes if not (getattr(r,'path',None)=='' and type(r).__name__=='Mount')]
app.mount('/',StaticFiles(directory=root/'frontend/dist',html=True))
server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='warning',access_log=False));thread=threading.Thread(target=server.run);thread.start()
deadline=time.monotonic()+15
while not server.started:
    if time.monotonic()>deadline:raise TimeoutError('UI service startup')
    time.sleep(.03)
seed={'base':'http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1]),'token':token,'project':project['id'],'document':doc['id'],'encrypted':encrypted['id'],'tiff':tiff['id'],'photo':photo,'result':result,
      'scope':'actual PDF import/extraction/export and HTTP/SQLite/UI; seeded OCR+geometry only for deterministic interaction/performance; no human efficiency or model accuracy claim'}
(out/'seed.json').write_text(json.dumps(seed,ensure_ascii=False,indent=2),'utf-8')
manager.start()
app.state.fusion_queue.start()
try:
    code=subprocess.run(['node',str(root/'scripts/audit_document_ui.mjs'),str(out)],cwd=root).returncode
    (out/'review-timings.json').write_text(json.dumps(store.rows('SELECT * FROM review_timings'),ensure_ascii=False,indent=2),'utf-8')
finally:
    server.should_exit=True;thread.join(timeout=15);manager.stop();app.state.fusion_queue.stop()
raise SystemExit(code)
