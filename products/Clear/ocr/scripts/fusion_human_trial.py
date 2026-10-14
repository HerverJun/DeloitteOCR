"""Prepare and run the fixed 20-image, three-workflow HUMAN trial locally.

No automated action is recorded as a human completion. Public research images,
references, raw results and trial workspaces stay outside distribution archives.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'scripts')]
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.fusion import load_policy
from ocr_workbench.task_queue import FusionQueue
from evaluate_fusion import inputs, sources, metrics

SAMPLE_IDS = ['0161','0163','0165','0169','0170','0176','0179','0180',
              '0031','0082','0083','0084','0087','0090',
              '0001','0002','0005','0008','0009','0010']
MODES = ['old','conservative','aggressive']


def write(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')


def prepare(data, out):
    out.mkdir(parents=True,exist_ok=False)
    manifest,annotations=inputs(data)
    selected={s['id']:s for s in manifest['samples'] if s['id'] in SAMPLE_IDS}
    assert len(selected)==20 and all(s['split']!='holdout' for s in selected.values())
    store=Store(out/'workspace'); cpu=FusionQueue(store,ROOT)
    rounds=[]
    for n,id in enumerate(SAMPLE_IDS):
        sample=selected[id]
        for slot in range(3):
            mode=MODES[(slot+n)%3]
            project=store.project(f'试用 {len(rounds)+1:02d} · {id} · '+{'old':'普通校对','conservative':'保守融合','aggressive':'积极融合'}[mode])
            temporary=out/('import-'+id+'.png');shutil.copy2(sample['image'],temporary)
            photo=add_image(store,project['id'],id+'.png',temporary)
            ss=sources(sample,manifest)
            engine_sources={s['engine']:s for s in ss if sample['kind']!='table' or s['engine']!='ppocr'}
            store.enqueue(project['id'],[photo['active_version']],list(engine_sources))
            result_ids=[];results_by_engine={}
            while task:=store.claim():
                s=engine_sources[task['engine']]
                if s['status']=='succeeded':
                    raw=deepcopy(s['original']);raw['project_image_version']=photo['active_version']
                    raw['trial_original_path']=next(v['result_path'] for v in sample['sources'] if v['engine']==s['engine'])
                    store.complete(task['id'],raw)
                    rid=store.one('tasks',task['id'])['result_id'];result_ids.append(rid);results_by_engine[s['engine']]=rid
                else:
                    with store.transaction() as db: db.execute("UPDATE tasks SET status='failed',error=? WHERE id=?",(str(s.get('error')),task['id']))
            policy=load_policy(ROOT,sample['kind'],'conservative' if mode=='old' else mode)
            baseline=results_by_engine[policy['baseline']]
            with store.transaction() as db: db.execute('INSERT OR REPLACE INTO selections VALUES(?,?)',(photo['id'],baseline))
            target=baseline
            if mode!='old':
                task=store.enqueue_fusion(project['id'],result_ids,policy,'human-trial-'+str(len(rounds)),list(engine_sources))[0]
                assert cpu.step()
                target=store.one('tasks',task)['result_id'];assert target
            rounds.append({'round':len(rounds)+1,'sample_id':id,'mode':mode,'kind':sample['kind'],'split':sample['split'],
                           'project_id':project['id'],'image_id':photo['id'],'version_id':photo['active_version'],
                           'target_result':target,'baseline_result':baseline,'image_sha256':sample['image_sha256']})
    write(out/'trial.json',{'schema_version':1,'sample_ids':SAMPLE_IDS,'rounds':rounds,
         'sequence':'same sample repeated in Latin rotation old/conservative/aggressive; one-person learning carryover must be reported',
         'missing_coverage':['no multi-table full pages in this 200-crop corpus','no verified 18-digit identifier sample','no verified perspective-camera scene; 0169 is rotated'],
         'policy_sha256':hashlib.sha256((ROOT/'config/fusion-policy.json').read_bytes()).hexdigest(),
         'annotation_sha256':hashlib.sha256((data/'annotations.json').read_bytes()).hexdigest(),
         'status':'awaiting_human_operator','human_rounds_completed':0})
    write(out/'references.json',{id:annotations[id] for id in SAMPLE_IDS})
    with sqlite3.connect(out/'timing.sqlite3') as db:
        db.execute('CREATE TABLE timing(round INTEGER PRIMARY KEY,operator TEXT,started REAL,finished REAL,pause_seconds REAL,details TEXT,snapshot TEXT,metrics TEXT)')
    (out/'开始人工试用.cmd').write_text('@echo off\r\n"'+sys.executable+'" -B -X utf8 "'+str(Path(__file__).resolve())+'" serve --output "'+str(out.resolve())+'"\r\npause\r\n','utf-8')
    print(json.dumps({'prepared':len(rounds),'human_completed':0,'directory':str(out)},ensure_ascii=False),flush=True)


def trial_app(out):
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles
    from ocr_workbench.service import create_app
    token=secrets.token_urlsafe(32)
    app=create_app(ROOT,out/'workspace',token,review_only=True)
    spec=json.loads((out/'trial.json').read_text('utf-8'));references=json.loads((out/'references.json').read_text('utf-8'))
    store=app.state.store
    def database():
        db=sqlite3.connect(out/'timing.sqlite3');db.row_factory=sqlite3.Row;return db

    @app.get('/trial',response_class=HTMLResponse)
    def page(): return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>20 张人工流程试用</title>
<style>body{font:17px system-ui;max-width:850px;margin:40px auto;line-height:1.7;color:#243329;background:#f7f8f5}button,input,textarea{font:inherit;padding:9px;margin:5px}button{cursor:pointer}label{display:block}#error{color:#a51e15}pre{white-space:pre-wrap}small{color:#626a64}</style>
<h1>20 张人工流程试用</h1><p>60 轮：同一批图片分别使用普通校对、保守融合和积极融合。所有结果已准备，不需运行 OCR。</p>
<p>点击开始后在工作台核对原图、修正内容并明确确认结果。融合轮须采用融合草稿，可用快速校对和普通编辑。关闭工作台页后回到这里记录本轮。请勿以脚本操作代替人工。</p>
<label>操作者代号 <input id="operator" autocomplete="off"></label><pre id="status"></pre><button id="start">开始下一轮并打开工作台</button><button id="reopen">重新打开当前工作台</button>
<label>本轮中断/休息秒数 <input id="pause" type="number" min="0" value="0"></label>
<label>定位到错误位置次数 <input id="mislocate" type="number" min="0" value="0"></label>
<label>观察到的误写次数（含后来改正） <input id="miswrite" type="number" min="0" value="0"></label>
<label>整表复核次数 <input id="whole" type="number" min="0" value="0"></label>
<label>体验记录（定位、候选、熟悉样本等）<textarea id="notes" rows="3"></textarea></label>
<button id="finish">已在工作台确认，保存本轮人工记录</button><p id="error" role="alert"></p><small>时间来自开始/完成按钮；浏览器重启仍保留计时，请扣除实际休息。最终漏错按保存快照计分，存在标注歧义的项目须由第二读者复核。单人重复样本的熟悉效应必须计入解释。</small><script src="/trial.js"></script></html>'''

    @app.get('/trial.js',response_class=PlainTextResponse)
    def script():
        return PlainTextResponse('''const $=id=>document.getElementById(id);const token=new URLSearchParams(location.hash.slice(1)).get('token')||sessionStorage.getItem('ocr-token');sessionStorage.setItem('ocr-token',token);history.replaceState(null,'',location.pathname);
let state;async function call(url,body){const r=await fetch('/api/trial/'+url,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const v=await r.json();if(!r.ok)throw Error(v.message||v.detail);return v}
function openRound(round){localStorage.setItem('ocr-project',round.project_id);window.open('/?trial_round='+round.round+'#token='+encodeURIComponent(token),'fusion-trial-workbench')}
async function refresh(){state=await call('state');$('status').textContent=JSON.stringify({completed:state.completed,total:60,current:state.current?{round:state.current.round,sample:state.current.sample_id,mode:state.current.mode}:null},null,2);$('start').disabled=!!state.active||state.completed===60;$('finish').disabled=!state.active;$('reopen').disabled=!state.active}
async function act(fn){try{$('error').textContent='';await fn();await refresh()}catch(e){$('error').textContent=String(e)}}
$('start').onclick=()=>act(async()=>{const v=await call('start',{operator:$('operator').value.trim()});openRound(v)});$('reopen').onclick=()=>openRound(state.current);
$('finish').onclick=()=>act(async()=>{await call('finish',{round:state.current.round,pause_seconds:Number($('pause').value),mislocations:Number($('mislocate').value),miswrites:Number($('miswrite').value),whole_table_reviews:Number($('whole').value),notes:$('notes').value});$('pause').value=$('mislocate').value=$('miswrite').value=$('whole').value='0';$('notes').value=''});refresh().catch(e=>$('error').textContent=String(e));''',media_type='application/javascript')

    @app.get('/api/trial/state')
    def state():
        with database() as db: rows=[dict(r) for r in db.execute('SELECT * FROM timing ORDER BY round')]
        done={r['round'] for r in rows if r['finished']};active=next((r for r in rows if not r['finished']),None)
        current=next((r for r in spec['rounds'] if r['round'] not in done),None)
        return {'completed':len(done),'active':active,'current':current}

    @app.post('/api/trial/start')
    def start(body:dict):
        operator=str(body.get('operator','')).strip()
        if not operator or len(operator)>80: raise ValueError('请填写操作者代号（1–80字）')
        current=state()
        if not current['current']: raise ValueError('60轮已完成')
        if current['active']: return current['current']
        with database() as db: db.execute('INSERT INTO timing(round,operator,started) VALUES(?,?,?)',(current['current']['round'],operator,time.time()))
        return current['current']

    @app.post('/api/trial/finish')
    def finish(body:dict):
        current=state();round_=current['current']
        if not current['active'] or body.get('round')!=round_['round']: raise ValueError('当前轮次不匹配')
        snapshot=store.project_snapshot(round_['project_id']);photo=snapshot['images'][0]
        result=store.result(round_['target_result'])
        review=photo.get('review_state') or {}
        if photo['selected_result']!=result['id'] or photo.get('review_status')!='confirmed' or review.get('revision')!=result['revision']:
            raise ValueError('请先采用本轮指定结果，并在工作台明确确认当前图片')
        finished=time.time();pause=body.get('pause_seconds',0)
        if not isinstance(pause,(int,float)) or not 0<=pause<finished-current['active']['started']: raise ValueError('休息时间无效')
        for key in ['mislocations','miswrites','whole_table_reviews']:
            if type(body.get(key)) is not int or body[key]<0: raise ValueError('次数必须是非负整数')
        measured=metrics(references[round_['sample_id']]['revised'],result['edited'],round_['kind'])
        with database() as db:
            db.execute('UPDATE timing SET finished=?,pause_seconds=?,details=?,snapshot=?,metrics=? WHERE round=? AND finished IS NULL',
                       (finished,pause,json.dumps(body,ensure_ascii=False),json.dumps(result,ensure_ascii=False),json.dumps(measured),round_['round']))
            records=[dict(r) for r in db.execute('SELECT * FROM timing ORDER BY round')]
        write(out/'human-records.json',{'completed':len([r for r in records if r['finished']]),'records':records,
             'scope':'Operator-started/finished trial; raw wall-time minus operator-reported pause. Requires human final-error adjudication and learning-effect review.'})
        return {'saved':True,'completed':state()['completed']}

    app.mount('/',StaticFiles(directory=ROOT/'frontend/dist',html=True))
    return app,token


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['prepare','serve'])
    parser.add_argument('--data',type=Path,default=ROOT/'audit/fusion-20260912/data')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8881)
    args=parser.parse_args()
    if args.stage=='prepare': return prepare(args.data,args.output.resolve())
    import uvicorn
    app,token=trial_app(args.output.resolve())
    url=f'http://127.0.0.1:{args.port}/trial#token={token}'
    (args.output/'trial-url.txt').write_text(url,'utf-8')
    print(url,flush=True)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,access_log=False,timeout_graceful_shutdown=8))
    app.state.shutdown=lambda: setattr(server,'should_exit',True)
    server.run()


if __name__=='__main__': main()
