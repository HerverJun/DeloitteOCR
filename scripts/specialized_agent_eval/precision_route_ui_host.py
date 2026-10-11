"""Isolated real HTTP/UI admission of new precision requests; idle queue only.

The 240 synthetic image versions exercise input scope and durable original
budgets. They are engineering inputs, not recognition or review-relief evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time

from common import fpath, frozen_json, read_json
from controlled_recovery import require_environment
from freeze_sources import freeze


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--worktrees',required=True)
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--port',type=int,default=8791)
    parser.add_argument('--resume-from',help='Closed prior engineering host; preserve exact input/batch identities')
    args=parser.parse_args()
    require_environment()
    output=fpath(args.output)
    if output.exists():raise ValueError('Use a new immutable workflow identity')
    output.mkdir(parents=True)
    freeze(args.worktrees,output/'source')
    prior=fpath(args.resume_from) if args.resume_from else None
    if prior:
        from cold_backup_restore_native import inventory
        stop=read_json(prior/'stopped.json')
        if not stop['servers_stopped'] or not stop['task_queue_stopped']:
            raise ValueError('Resume source must be completely stopped')
        before=inventory(prior/'runtime')
        shutil.copytree(prior/'runtime',output/'runtime')
        if inventory(output/'runtime')!=before or inventory(prior/'runtime')!=before:
            raise ValueError('Closed engineering source changed during byte copy')
        for name in ('precision-submission.json','ordinary-submission.json'):
            shutil.copyfile(prior/name,output/name)
        frozen_json(output/'resume-lineage.json',{'source':str(prior),'runtime_files_sha256':before,
            'batch_ids_preserved':True,'original_budget_and_deadline_preserved':True})
    native=output/'source/ocr'
    sys.path[:0]=[str(native/'src')]
    import uvicorn
    from PIL import Image
    from ocr_workbench.service import create_app
    from ocr_workbench.imaging import add_image

    token=secrets.token_hex(32)
    app=create_app(fpath(args.bundle),output/'runtime/ocr',token,
        start_queue=False,agent_enabled=False,web_root=native/'frontend/dist')
    if prior:
        scope=read_json(prior/'scope.json')
        project,versions=scope['project_id'],scope['version_ids']
        input_hash=scope['input_sha256']
    else:
        project=app.state.store.project('精度入口工程验证240图')['id']
        source=output/'synthetic-input.png'
        Image.new('RGB',(100,70),'white').save(source)
        input_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        versions=[]
        for index in range(240):
            temporary=output/'import-input.png'
            shutil.copyfile(source,temporary)
            versions.append(add_image(app.state.store,project,f'工程输入-{index:03d}.png',temporary)['active_version'])
    # Full production App correctly disables submission for an unhealthy queue.
    # Keep the actual task queue healthy and idle, without starting the batch
    # coordinator that would turn admitted screen_items into model tasks.
    def reject_adapter(*args,**kwargs):
        raise RuntimeError('Engineering admission host forbids model invocation')
    app.state.queue.factory=reject_adapter
    app.state.queue.start()
    frozen_json(output/'scope.json',{'format':'precision-route-ui-scope-v1',
        'project_id':project,'version_ids':versions,'input_sha256':input_hash,
        'images':240,'synthetic_engineering_inputs':True,'model_calls':0,'agent_calls':0,
        'idle_task_queue_started':True,'batch_coordinator_started':False,
        'recognition_or_review_relief_qualification':False})
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,log_level='warning'))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+60
        while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.1)
        if not server.started:raise RuntimeError('Isolated native host did not start')
        frozen_json(output/'ready.json',{'url':f'http://127.0.0.1:{args.port}',
            'token':token,'project_id':project,'idle_task_queue_started':True,'batch_coordinator_started':False})
        print(json.dumps({'ready':True,'output':str(output),'batch_coordinator_started':False}),flush=True)
        while not (output/'stop.request').exists() and thread.is_alive():time.sleep(.2)
    finally:
        server.should_exit=True
        thread.join(timeout=15)
        app.state.queue.stop()
        tasks=app.state.store.rows('SELECT id,status FROM tasks')
        frozen_json(output/'stopped.json',{'servers_stopped':not thread.is_alive(),
            'task_queue_stopped':not app.state.queue.status()['alive'],
            'batch_coordinator_started':False,'native_tasks':tasks,'model_calls':0,'agent_calls':0,
            'database':str(app.state.store.root/'workbench.sqlite3')})
        if tasks:raise RuntimeError('Admission-only host created unexpected tasks')


if __name__=='__main__':main()
