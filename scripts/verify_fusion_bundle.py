"""Smoke the actual packaged launcher, service, frozen policy and CPU fusion."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from ocr_workbench import __version__
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.tables import parse_tables
from PIL import Image


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    bundle = a.bundle.resolve(); out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    checks = []; report = {'passed': False, 'checks': checks, 'scope': 'Actual packaged launcher and service on this development PC; synthetic OCR sources, CPU fusion. No new OCR accuracy, human timing or clean-machine claim.'}
    try:
        for source in (ROOT/'src').rglob('*.py'):
            target = bundle/'app'/source.relative_to(ROOT/'src')
            assert source.read_bytes() == target.read_bytes(), str(target)
        assert (bundle/'config/fusion-policy.json').read_bytes() == (ROOT/'config/fusion-policy.json').read_bytes()
        report['manifest_sha256'] = hashlib.sha256((bundle/'manifest.json').read_bytes()).hexdigest()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for review_only in (True, False):
            mode = 'review-only' if review_only else 'full'
            data = out/(mode+' 中文工作区')
            store = Store(data)
            project = store.project('候选包融合检查')
            temporary = out/(mode+'.png'); Image.new('RGB',(600,400),'white').save(temporary)
            photo = add_image(store, project['id'], 'fixture.png', temporary)
            store.enqueue(project['id'], [photo['active_version']], ['glm','paddlevl','hunyuan'])
            ids = []
            while task := store.claim():
                text = '<table><tr><td>项目</td><td>编号</td></tr><tr><td>甲</td><td>'+('00123' if task['engine']=='glm' else '00124')+'</td></tr></table>'
                store.complete(task['id'], {'engine':task['engine'],'text':text,'tables':parse_tables(text),'blocks':[],
                     'project_image_version':photo['active_version'],'image':{'width':600,'height':400}})
                ids.append(store.one('tasks',task['id'])['result_id'])
            startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
            process = subprocess.Popen([str(bundle/'launcher/OfflineOCRLauncher.exe'), '--data', str(data), '--no-browser',
                 *(['--review-only'] if review_only else [])], startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
            base = token = None
            def api(path, body=None):
                request = urllib.request.Request(base+'/api'+path, data=None if body is None else json.dumps(body).encode(),
                     headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with opener.open(request, timeout=20) as response: return json.load(response)
            try:
                deadline = time.monotonic()+600
                last_log = 0
                while True:
                    if process.poll() is not None: raise RuntimeError(mode+' launcher exited')
                    if time.monotonic()>deadline: raise TimeoutError(mode+' startup')
                    try:
                        startup_state = json.loads((data/'launcher/startup-state.json').read_text('utf8'))
                        if startup_state.get('status')=='failed': raise RuntimeError(startup_state.get('message'))
                        state = json.loads((data/'launcher/launcher-state.json').read_text('utf8'))
                        if state['pid']==process.pid:
                            base='http://127.0.0.1:'+str(state['port'])
                            token=(data/'launcher/session-token.txt').read_text('utf8').strip()
                            health=api('/health')
                            if health['status']=='ready': break
                    except (OSError, ValueError, urllib.error.URLError): pass
                    if time.monotonic()-last_log>20:
                        print(json.dumps({'mode':mode,'phase':'startup checks'}),flush=True);last_log=time.monotonic()
                    time.sleep(.2)
                assert health['version']==__version__ and health['review_only']==review_only
                assert health['fusion_queue']['healthy']
                policies=api('/fusion/policies')
                submitted=api('/projects/'+project['id']+'/fusion', {'result_ids':ids,'content_type':'table','mode':'conservative',
                       'request_id':'bundle-smoke-'+mode,'expected_engines':['glm','paddlevl','hunyuan']})
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    snapshot=api('/projects/'+project['id'])
                    fused=next((t for t in snapshot['tasks'] if t['kind']=='fusion'),None)
                    if fused and fused['status']=='succeeded':break
                    if fused and fused['status']=='failed':raise RuntimeError(fused['error'])
                    time.sleep(.1)
                assert fused['status']=='succeeded'
                result=api('/results/'+fused['result_id'])
                issues=api('/results/'+fused['result_id']+'/issues')
                assert result['original']['origin']=='fusion'
                assert issues['issues']
                assert snapshot['images'][0]['selected_result'] != fused['result_id']
                with opener.open(base+'/', timeout=10) as response:assert b'<html' in response.read()
                checks.append({'mode':mode,'passed':True,'health':health,'fusion_task':fused['id'],'issues':issues['counts'],
                               'startup_status':startup_state.get('status')})
                print(json.dumps({'mode':mode,'passed':True}),flush=True)
            finally:
                if base and token:
                    try: api('/shutdown',{})
                    except Exception: pass
                try: process.wait(20)
                except subprocess.TimeoutExpired:
                    process.terminate();process.wait(10)
                assert not (data/'launcher/session-token.txt').exists(), 'Launcher token not cleaned'
        report['passed']=True
    finally:
        (out/'bundle-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps({'passed':report['passed'],'report':str(out/'bundle-smoke.json')}),flush=True)


if __name__=='__main__':main()
