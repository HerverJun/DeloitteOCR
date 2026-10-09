"""Seal stopped native model evidence, then independently collect and score it.

The seal and receipt collection never read truth. Scoring is a separate command
after terminal byte sealing. Source databases are only copied as bytes; all
SQLite reads use a separate F-drive byte copy including WAL/SHM.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, copy_bytes, directory_inventory, file_inventory, plain_path, require_environment


def owned_processes(run):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',run.name):
        raise ValueError('Unsafe unique run identity')
    command=("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*"+run.name+"*' -and "
             "($_.CommandLine -like '*evaluate_screen_agent_native.py*' -or "
             "$_.CommandLine -like '*ocr_workbench.engine_host*') } | "
             "ForEach-Object { [pscustomobject]@{ pid=$_.ProcessId; parent_pid=$_.ParentProcessId; "
             "creation_utc=$_.CreationDate.ToUniversalTime().ToString('o') } } | ConvertTo-Json -Compress")
    result=subprocess.run(['powershell.exe','-NoProfile','-Command',command],cwd=run.parent,
        capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('Owned process presence query failed')
    value=json.loads(result.stdout) if result.stdout.strip() else []
    return value if isinstance(value,list) else [value]


def check_sources(run):
    prepared=read_json(run/'prepared.json')
    source=fpath(prepared['native_source']).parent
    trial=read_json(source/'trial-source-binding.json')
    for family,entry in trial['sources'].items():
        for name,sha in entry['files'].items():
            if digest(fpath(entry['path'])/name)!=sha:
                raise ValueError('Frozen '+family+' source changed: '+name)
    if (fpath(trial['actual_output'])!=run or prepared['screen_policy']!=trial['policy']
            or prepared['input_count']!=trial['scope_inputs']
            or digest(prepared['inputs'])!=trial['inputs']['sha256']):
        raise ValueError('Actual trial differs from explicit CPU preparation')
    platform=read_json(run/'platform-source-binding.json')
    if any(trial['sources']['platform']['files'].get(name)!=sha for name,sha in platform.items()):
        raise ValueError('Actual platform source differs')
    return {'trial':bound(source/'trial-source-binding.json'),'prepared':bound(run/'prepared.json'),
            'scope_inputs':prepared['input_count'],'engine':prepared['screen_policy']['primary_engine'],
            'final_agent_qualification':False,'same_source_performance_comparison':False}


def seal(run,output,exit_code):
    if output.exists():
        raise ValueError('Use a new seal output')
    if exit_code!=0:
        raise ValueError('Requires actually observed normal harness exit; preserve nonzero separately')
    summary=read_json(run/'agent-native-summary.json')
    events=read_json(run/'stage-timestamps.json')['events']
    if (summary.get('truth_read') is not False or summary.get('frozen_results_loaded') is not False
            or not any(e['stage']=='cleanup' and e['event']=='finished' for e in events)):
        raise ValueError('Truth-isolated finished native cleanup not observed')
    remaining=owned_processes(run)
    if remaining:
        raise ValueError('Owned runtime processes remain; no terminal seal possible')
    sources=check_sources(run)
    files,dirs=file_inventory(run),directory_inventory(run)
    copy_bytes(run,output/'runtime',files,dirs)
    if file_inventory(run)!=files or directory_inventory(run)!=dirs:
        raise ValueError('Original actual evidence changed during byte sealing')
    helpers={}
    for source in [Path(__file__),*(Path(__file__).parent/name for name in
        ('common.py','controlled_recovery.py','native_bridge.py','native_diagnostics.py','quality.py','readback.py')),
        Path('F:/OCR/scripts/screen_native_report.py')]:
        target=output/'helper-source'/source.name
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        if digest(source)!=digest(target):raise ValueError('Offline helper byte copy changed')
        helpers[source.name]=bound(target)
    report={'format':'m1-model-trial-stopped-seal-v1','utc':datetime.now(timezone.utc).isoformat(),
            'status':'sealed_before_truth','actual_harness_exit_code':exit_code,'raw_run':str(run),
            'sealed_runtime':str(output/'runtime'),'runtime_files':files,'runtime_directories':dirs,
            'sources':sources,'helpers':helpers,'owned_processes_remaining':remaining,
            'truth_read':False,'holdout_read':False,'source_sqlite_opened':False,
            'model_or_service_started':False,'formal_current_changed':False}
    frozen_json(output/'result.json',report)
    print({'status':report['status'],'files':len(files),'scope':sources['scope_inputs']})


def copied_database(run,seal_root,output,relative,name):
    source=seal_root/'runtime'/relative;target=output/'sqlite-copies'/name
    target.parent.mkdir(parents=True,exist_ok=True)
    bindings={}
    for member in (source,source.with_name(source.name+'-wal'),source.with_name(source.name+'-shm')):
        if member.exists():
            destination=target.with_name(target.name+member.name[len(source.name):])
            shutil.copyfile(member,destination)
            if digest(destination)!=digest(member):raise ValueError('SQLite byte copy differs')
            bindings[member.name]=bound(member)
    return target,bindings


def collect(run,seal_root,output,inputs):
    if output.exists():raise ValueError('Use a new independent output')
    sealed=read_json(seal_root/'result.json')
    if (sealed['status']!='sealed_before_truth' or sealed['truth_read'] is not False
            or fpath(sealed['raw_run'])!=run or owned_processes(run)):
        raise ValueError('Actual stopped truth-isolated seal unavailable')
    if file_inventory(run)!=sealed['runtime_files'] or directory_inventory(run)!=sealed['runtime_directories']:
        raise ValueError('Actual evidence changed after its terminal seal')
    check_sources(run);output.mkdir(parents=True)
    original_native=run/'native/ocr/workbench.sqlite3'
    native,native_sources=copied_database(run,seal_root,output,'native/ocr/workbench.sqlite3','native.sqlite3')
    agent,agent_sources=copied_database(run,seal_root,output,'agent/operations.sqlite3','agent.sqlite3')
    original_connect=sqlite3.connect;native_uri=original_native.as_uri();redirected=[]
    def isolated_connect(database,*args,**kwargs):
        value=str(database)
        if value==native_uri or value.startswith(native_uri+'?'):
            redirected.append({'original':str(original_native),'actual_sqlite_read':str(native)})
            return original_connect(native.as_uri()+value[len(native_uri):],*args,**kwargs)
        return original_connect(database,*args,**kwargs)
    import native_bridge
    sqlite3.connect=isolated_connect
    try:index,observations=native_bridge.collect(run,inputs)
    finally:sqlite3.connect=original_connect
    if not redirected:raise ValueError('Independent SQLite redirect not exercised')
    frozen_json(output/'observations.native-index.json',index)
    observations['native_index_sha256']=digest(output/'observations.native-index.json')
    observations['sqlite_original_opened']=False
    frozen_json(output/'observations.json',observations)
    from readback import check_bundle
    summary=read_json(run/'agent-native-summary.json');checks=[]
    for batch in summary['native_batches']:
        export=batch.get('export_receipt')
        if not export:
            checks.append({'batch_id':batch['batch_id'],'status':'no_actual_export','workbooks':0,
                           'current_delivery_verified':False});continue
        batch_id=batch['batch_id'];zip_path=seal_root/'runtime'/(batch_id+'.zip')
        snapshot=seal_root/'runtime'/('readback-snapshot-'+batch_id+'.json')
        check=check_bundle(zip_path,read_json(snapshot));path=output/(batch_id+'-xlsx-readback.json')
        frozen_json(path,check)
        checks.append({'batch_id':batch_id,'bundle_id':export['bundle_id'],'zip':bound(zip_path),
                       'snapshot':bound(snapshot),'report':bound(path),
                       **{k:check[k] for k in ('status','inputs','workbooks','no_book','fidelity_status','freshness_status','current_delivery_verified')}})
    with closing(sqlite3.connect(agent.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        operations=[dict(row) for row in db.execute('SELECT tool,state,result FROM operations ORDER BY created')]
    published=[json.loads(row['result']) for row in operations if row['tool']=='delivery_publish' and row['result']]
    bundles={item['bundle_id'] for result in published for item in result.get('data',{}).get('delivery',[])}
    delivered={row['bundle_id'] for row in checks if row.get('current_delivery_verified')}
    unchanged=file_inventory(run)==sealed['runtime_files'] and directory_inventory(run)==sealed['runtime_directories']
    if not unchanged:raise ValueError('Original runtime changed during independent readback')
    report={'format':'m1-model-trial-independent-readback-v1','utc':datetime.now(timezone.utc).isoformat(),
            'seal':bound(seal_root/'result.json'),'observations':bound(output/'observations.json'),
            'native_index':bound(output/'observations.native-index.json'),'scope_inputs':len(observations['records']),
            'sqlite_byte_sources':{'native':native_sources,'agent':agent_sources},'redirected_reads':redirected,
            'xlsx_checks':checks,'actual_agent_published_bundle_ids':sorted(bundles),
            'current_readback_bundle_ids':sorted(delivered),
            'agent_current_bundle_matches_independent_download':bool(bundles) and bundles==delivered,
            'post_agent_harness_export_is_not_agent_delivery':True,'actual_agent_run_error':summary.get('run_error'),
            'original_runtime_byte_set_unchanged':unchanged,'source_sqlite_opened':False,
            'truth_read':False,'holdout_read':False,'model_or_service_started':False,
            'final_agent_qualification':False,'same_source_performance_comparison':False}
    frozen_json(output/'independent-readback.json',report)
    print({'inputs':len(observations['records']),'workbooks':sum(x['workbooks'] for x in checks),
           'agent_current_bundle_matches':report['agent_current_bundle_matches_independent_download']})


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    s=sub.add_parser('seal');s.add_argument('--run',required=True);s.add_argument('--output',required=True)
    s.add_argument('--observed-harness-exit-code',type=int,required=True)
    c=sub.add_parser('collect')
    for name in ('run','seal','output','inputs'):c.add_argument('--'+name,required=True)
    q=sub.add_parser('score')
    for name in ('seal','truth','observations','output'):q.add_argument('--'+name,required=True)
    args=parser.parse_args();require_environment()
    if args.command=='seal':seal(plain_path(args.run),plain_path(args.output),args.observed_harness_exit_code)
    elif args.command=='collect':collect(plain_path(args.run),plain_path(args.seal),plain_path(args.output),plain_path(args.inputs))
    else:
        sealed=read_json(fpath(args.seal)/'result.json')
        if sealed['status']!='sealed_before_truth' or sealed['truth_read'] is not False or owned_processes(fpath(sealed['raw_run'])):
            raise ValueError('Only stopped sealed source may be scored')
        from quality import score_files
        score=score_files(args.truth,args.observations);frozen_json(args.output,score)
        print(score['summary'])


if __name__=='__main__':main()
