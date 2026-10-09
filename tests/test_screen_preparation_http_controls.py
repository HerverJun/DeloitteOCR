"""Native authenticated HTTP controls while coordinator CPU work is blocked.

These exercise the production ASGI app, SQLite and preparation futures. No
model, GPU worker or controller is started, so this is an engineering gate.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import httpx
from PIL import Image
import pytest

from ocr_workbench.imaging import add_image
from ocr_workbench.service import create_app
from test_screen_queue import prepare


@pytest.mark.parametrize('control', ['pause', 'cancel'])
def test_authenticated_status_control_and_reentry_during_preparation(tmp_path, control):
    bundle=tmp_path/'bundle'
    (bundle/'config').mkdir(parents=True)
    (bundle/'config/engines.json').write_text(json.dumps({
        'glm':{'name':'CPU fixture only','models':[],'capabilities':{'tables':True}}}),encoding='utf8')
    token='p'*32
    app=create_app(bundle,tmp_path/'workspace',token,start_queue=False,agent_enabled=False)
    store,manager=app.state.store,app.state.screen_batches
    manager._hold_wal()
    project=store.project('Long preparation HTTP')['id']
    other=store.project('Other scope')['id']
    source=tmp_path/'input.png'
    Image.new('RGB',(200,100),'white').save(source)
    version=add_image(store,project,source.name,source)['active_version']
    entered,release=threading.Event(),threading.Event()
    preparations=[]
    def blocked_prepare(*args,**kwargs):
        preparations.append(str(args[0]));entered.set()
        if not release.wait(10):raise TimeoutError('Controlled preparation was not released')
        return prepare(*args,**kwargs)
    manager.prepare_override=blocked_prepare

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url='http://testserver',headers={'Authorization':'Bearer '+token}) as client:
            endpoint=f'/api/projects/{project}/screen-batches'
            created=await client.post(endpoint,json={'version_ids':[version],'request_id':'http-preparation',
                'policy':{'primary_engine':'glm','layout_evidence':False,'max_upgrades':0}})
            assert created.status_code==200,created.text
            batch=created.json()['batch_id'];url=endpoint+'/'+batch
            with ThreadPoolExecutor(1) as pool:
                step=pool.submit(manager.step)
                try:
                    assert await asyncio.to_thread(entered.wait,2)
                    response=await asyncio.wait_for(client.get(url),2)
                    assert response.status_code==200,response.text
                    assert response.json()['status']=='running'
                    assert not step.done()
                    denied=await asyncio.wait_for(client.get(url,headers={'Authorization':'Bearer invalid'}),2)
                    assert denied.status_code==401
                    wrong=await asyncio.wait_for(client.get(f'/api/projects/{other}/screen-batches/{batch}'),2)
                    assert wrong.status_code in {400,404}
                    body={'action':control,'request_id':'during-preparation-'+control}
                    first=await asyncio.wait_for(client.post(url+'/action',json=body),2)
                    assert first.status_code==200,first.text
                    assert first.json()['status']==('paused' if control=='pause' else 'cancelled')
                    replay=await asyncio.wait_for(client.post(url+'/action',json=body),2)
                    lookup=await asyncio.wait_for(client.post(url+'/action-lookup',json=body),2)
                    assert replay.json()==first.json()==lookup.json()
                    assert not step.done()
                finally:
                    release.set()
                    await asyncio.to_thread(step.result,5)
            assert not store.rows('SELECT * FROM screen_tasks WHERE batch_id=?',(batch,))
            item=manager._items(batch)[0]
            if control=='cancel':
                assert item['status']=='cancelled'
            else:
                assert item['status']=='paused' and item['preparation']
                resumed=await client.post(url+'/action',json={'action':'resume','request_id':'new-resume-intent'})
                assert resumed.status_code==200,resumed.text
                manager.step();manager.step()
                assert len(preparations)==1
                tasks=store.rows('SELECT * FROM screen_tasks WHERE batch_id=?',(batch,))
                assert len(tasks)==1
                assert store.one('tasks',tasks[0]['task_id'])['status']=='queued'
    try:asyncio.run(exercise())
    finally:release.set();manager.stop()
