"""Compare real HTTP application routes with the replay's direct product calls."""
import argparse
import json
from pathlib import Path
import sys
import threading
import time
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parent))
from structure_repair_run import ROOT,AUDIT,BUILD,read,save


def main():
    import uvicorn
    from ocr_workbench.service import create_app
    from ocr_workbench.structure_store import refresh_proposals
    app=create_app(ROOT,BUILD/'hardened-small/workspace','research-parity-loopback',start_queue=False)
    store=app.state.store
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='error'))
    thread=threading.Thread(target=server.run);thread.start()
    checks=[]
    try:
        deadline=time.monotonic()+20
        while not server.started:
            if time.monotonic()>deadline:raise TimeoutError('HTTP application did not start')
            time.sleep(.05)
        base='http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1])
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for row in read(BUILD/'hardened-small/rows.json')[:3]:
            direct=refresh_proposals(store,row['result_id'],row['revision'])
            request=urllib.request.Request(base+'/api/results/'+row['result_id']+'/structure/check',
                data=json.dumps({'revision':row['revision']}).encode(),headers={
                    'Authorization':'Bearer research-parity-loopback','Content-Type':'application/json'})
            with opener.open(request,timeout=30) as response:web=json.load(response)
            fields=['revision','version_id','proposals','candidates','table_tool','adopted']
            checks.append({'sample':row['id'],'fields':fields,'equal':all(direct[k]==web[k] for k in fields),
                'providers':sorted(c['provider'] for c in web['candidates']),
                'revision':web['revision'],'table_tool':web['table_tool'],
                'proposal_count':len(web['proposals']),'applicable':sum(p['can_apply'] for p in web['proposals'])})
    finally:
        server.should_exit=True;thread.join(20)
    save(AUDIT/'replay-parity.json',{'checks':checks,'passed':len(checks)==3 and all(c['equal'] for c in checks),
        'service_closed':not thread.is_alive(),'source_first':str(ROOT/'src'),
        'source_route':'tableformer and geometry provider keys explicitly supplied, matching corrected historical replay',
        'limitations':'three real inputs via HTTP; frozen stored OCR/predictions; no re-inference or GPU claims'})
    print(json.dumps(checks,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.parse_args();main()
