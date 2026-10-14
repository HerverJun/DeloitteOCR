"""Real product UI, queue and two-protocol loopback fixture; no cloud calls."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'build/external-api-review/site-packages')]
from PIL import Image,ImageDraw,ImageFont
import uvicorn
from fastapi.staticfiles import StaticFiles
from test_external_review import ProviderFixture,KEY
from test_structure_workflow import table,prediction
from ocr_workbench import external_review as ext
from ocr_workbench.service import create_app
from ocr_workbench.imaging import add_image
from ocr_workbench.editing import tables_html
from ocr_workbench.structure_store import record_candidates,refresh_proposals


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    provider=ProviderFixture();token='complex-table-loopback-ui'
    app=create_app(ROOT,out/'workspace',token,review_only=True)
    app.state.external_connection.vault=ext.CredentialVault(out/'fixture-credentials')
    store=app.state.store;project=store.project('复杂表格专项验收')
    values=[['Account','Amount'],['Revenue','1.00'],['Tax','-0.01'],['Total','0.99']]
    image=Image.new('RGB',(600,320),'white');draw=ImageDraw.Draw(image);font=ImageFont.load_default(size=24)
    for r,row in enumerate(values):
        for c,text in enumerate(row):
            draw.rectangle((c*300,r*80,(c+1)*300-1,(r+1)*80-1),outline='black')
            draw.text((c*300+18,r*80+24),text,font=font,fill='black')
    path=out/'table.png';image.save(path)
    photo=add_image(store,project['id'],'complex-table.png',path);version=store.one('versions',photo['active_version'])
    task=store.enqueue(project['id'],[version['id']],['ppocr'])[0];store.claim()
    before=table([values[0],values[1],values[3]]);before['caption']='单位：元'
    store.complete(task,{'engine':'ppocr','text':tables_html([before]),'tables':[before],
        'project_image_version':version['id'],'image':{'width':600,'height':320},'blocks':[]})
    result=store.one('tasks',task)['result_id'];pred,blocks=prediction(values)
    for c in pred['tf_table_cells']:c['bbox']=[v*(3 if i%2==0 else 2) for i,v in enumerate(c['bbox'])]
    for b in blocks:b['polygon']=[[x*3,y*2] for x,y in b['polygon']]
    with store.transaction() as db:record_candidates(db,result,version,pred,blocks)
    refresh_proposals(store,result,store.result(result)['revision'])
    buf=io.BytesIO();image.save(buf,format='PNG')
    probe=patch.object(ext,'make_probe',return_value=(buf.getvalue(),provider.probe));probe.start()
    app.state.external_connection.save({'protocol':'openai','base_url':provider.url,'api_key':KEY,'model':'vision-a'})
    provider.requests.clear()
    @app.get('/api/audit/complex-fixture')
    def state():return {'requests':len(provider.requests)}
    @app.post('/api/audit/complex-fixture')
    def change(body:dict):
        provider.mode=body.get('mode','normal')
        if 'protocol' in body:
            app.state.external_connection.save({'protocol':body['protocol'],'base_url':provider.url,'api_key':KEY,'model':'vision-a'})
        return state()
    app.mount('/',StaticFiles(directory=ROOT/'frontend/dist',html=True))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='warning',access_log=False))
    thread=threading.Thread(target=server.run);thread.start()
    try:
        deadline=time.monotonic()+20
        while not server.started:
            if time.monotonic()>deadline:raise TimeoutError('UI service not ready')
            time.sleep(.05)
        seed={'base':'http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1]),'token':token,'result':result,'project':project['id']}
        (out/'seed.json').write_text(json.dumps(seed),'utf-8')
        completed=subprocess.run(['node',str(ROOT/'scripts/audit_complex_table_ui.mjs'),str(out)],cwd=ROOT)
    finally:
        server.should_exit=True;thread.join(20);provider.close();probe.stop()
        (out/'shutdown.json').write_text(json.dumps({'service_closed':not thread.is_alive(),
            'external_queue_closed':not app.state.external_queue.status()['alive'],'cloud_api_tested':False}),'utf-8')
    raise SystemExit(completed.returncode)


if __name__=='__main__':main()
