import json
from pathlib import Path
import tempfile
import unittest
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import uvicorn
from ocr_workbench.service import create_app

ROOT=Path(__file__).resolve().parents[1]
BUNDLE=ROOT/'build/document-workflow/bundle'
FIXTURES=ROOT/'build/document-workflow/fixtures'

class LocalClient:
    """Use the actual HTTP stack without adding dependencies to service runtime."""
    def __init__(self,app,headers):
        self.headers=headers
        self.server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='error',access_log=False))
        self.worker=threading.Thread(target=self.server.run);self.worker.start()
        deadline=time.monotonic()+10
        while not self.server.started:
            if time.monotonic()>deadline:raise TimeoutError('HTTP test server startup')
            time.sleep(.02)
        self.base='http://127.0.0.1:'+str(self.server.servers[0].sockets[0].getsockname()[1])
    def request(self,method,path,**kwargs):
        headers={**self.headers,**kwargs.get('headers',{})};body=None
        if 'params' in kwargs:path+='?'+urllib.parse.urlencode(kwargs['params'])
        if 'json' in kwargs:
            body=json.dumps(kwargs['json']).encode();headers['Content-Type']='application/json'
        if 'files' in kwargs:
            boundary='document-api-fixture';parts=[]
            for key,value in kwargs.get('data',{}).items():parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            for key,(name,data,kind) in kwargs['files']:
                parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{name}"\r\nContent-Type: {kind}\r\n\r\n'.encode(),data,b'\r\n'])
            parts.append(f'--{boundary}--\r\n'.encode());body=b''.join(parts)
            headers['Content-Type']='multipart/form-data; boundary='+boundary
        request=urllib.request.Request(self.base+path,data=body,headers=headers,method=method)
        try:response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=60)
        except urllib.error.HTTPError as error:response=error
        with response:
            from types import SimpleNamespace
            content=response.read()
            return SimpleNamespace(status_code=response.status,content=content,text=content.decode('utf-8',errors='replace'),json=lambda:json.loads(content))
    def get(self,path,**kwargs):return self.request('GET',path,**kwargs)
    def post(self,path,**kwargs):return self.request('POST',path,**kwargs)
    def close(self):
        self.server.should_exit=True;self.worker.join(timeout=15)
        if self.worker.is_alive():raise RuntimeError('Test server did not stop')

@unittest.skipUnless((BUNDLE/'runtimes/pdf/python.exe').exists(),'PDF runtime required')
class DocumentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(ROOT,Path(self.temp.name)/'data','document-test',start_queue=False)
        self.app.state.documents.cpu.runtime=BUNDLE/'runtimes/pdf/python.exe'
        self.client=LocalClient(self.app,headers={'Authorization':'Bearer document-test'})
        self.store=self.app.state.store
        self.project=self.store.project('API文档')

    def tearDown(self):
        self.client.close();self.app.state.documents.stop();self.temp.cleanup()

    def test_auth_import_pages_process_search_and_export_snapshot(self):
        denied=self.client.get('/api/documents/no/pages',headers={'Authorization':'Bearer wrong'})
        self.assertEqual(denied.status_code,401)
        response=self.client.post(f"/api/projects/{self.project['id']}/documents",data={'dpi':'72'},files=[('files',('native.pdf',(FIXTURES/'native.pdf').read_bytes(),'application/pdf'))])
        self.assertEqual(response.status_code,200,response.text)
        doc=response.json()['documents'][0]
        self.assertFalse(response.json()['errors'])
        page=self.client.get(f"/api/documents/{doc['id']}/pages").json()['pages'][0]
        self.assertIsNone(page['image_id'])
        process=self.client.post(f"/api/pages/{page['id']}/process",json={'mode':'native'}).json()['stage_id']
        self.app.state.documents.step()
        self.assertEqual(self.store.one('document_stages',process)['status'],'succeeded')
        again=self.client.post(f"/api/pages/{page['id']}/process",json={'mode':'native'}).json()['stage_id']
        self.assertEqual(process,again)
        hits=self.client.get(f"/api/documents/{doc['id']}/search",params={'q':'00123456789012345678'}).json()['matches']
        self.assertTrue(hits)
        preflight=self.client.post('/api/export/preflight',json={'document_ids':[doc['id']]}).json()
        self.assertTrue(preflight['ready'],preflight)
        exported=self.client.post('/api/export',json={'format':'pdf','document_ids':[doc['id']]})
        self.assertEqual(exported.status_code,200,exported.text if exported.status_code!=200 else '')
        self.assertTrue(exported.content.startswith(b'%PDF-'))
        self.assertEqual(self.client.post(f"/api/pages/{page['id']}/process",json={'mode':'native','force':'true'}).status_code,400)
        self.assertEqual(self.client.post('/api/export/preflight',json={'document_ids':[doc['id']],'page_numbers':[0]}).status_code,400)

    def test_geometry_manual_revision_version_and_native_conflict_decisions(self):
        from ocr_workbench.coordinates import box_polygon
        from ocr_workbench.imaging import add_image
        from PIL import Image
        path=Path(self.temp.name)/'scan.png';Image.new('RGB',(100,100),'white').save(path)
        image=add_image(self.store,self.project['id'],'scan.png',path)
        task=self.store.enqueue(self.project['id'],[image['active_version']],['ppocr'])[0];self.store.claim()
        self.store.complete(task,{'engine':'ppocr','text':'00123','tables':[],'blocks':[],
            'document':{'conflicts':[{'id':'overlap-1','native':{'text':'00123'},'ocr':{'text':'99123'}}]}})
        result=self.store.one('tasks',task)['result_id']
        body={'source':'manual','revision':0,'version_id':image['active_version'],'target':{'kind':'text','start':0,'end':5},'polygon':box_polygon([10,10,90,30])}
        self.assertEqual(self.client.post(f'/api/results/{result}/geometry',json={**body,'revision':9}).status_code,409)
        self.assertEqual(self.client.post(f'/api/results/{result}/geometry',json=body).status_code,200)
        location=self.client.post(f'/api/results/{result}/geometry/location',json={'target':body['target']}).json()
        self.assertEqual(location['evidence'][0]['source'],'manual')
        self.assertEqual(self.client.post(f'/api/results/{result}/document-conflicts/overlap-1',json={'revision':0}).status_code,200)
        decisions=self.store.rows('SELECT * FROM document_conflict_decisions')
        self.assertEqual(len(decisions),1)
        self.store.save(result,{'text':'99123','tables':[]},0)
        self.assertEqual(self.client.post(f'/api/results/{result}/document-conflicts/overlap-1',json={'revision':0}).status_code,409)
        self.assertFalse(self.client.get(f'/api/results/{result}/document-conflicts').json()['conflicts'][0]['reviewed'])
