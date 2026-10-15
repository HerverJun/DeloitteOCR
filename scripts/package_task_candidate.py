"""Freeze authored sources and web assets; declare F-drive shared dependencies."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

EXCLUDE={'.git','node_modules','__pycache__','.pytest_cache','dist','build','cache','temp','dependencies','runtimes','models','weights','output','.venv'}
SUFFIXES={'.pyc','.pyo','.tsbuildinfo','.sqlite','.sqlite3','.db','.bin','.pth','.pt','.onnx','.safetensors','.log','.zip','.whl','.exe','.dll'}

def physical(value):
    requested=Path(value).absolute();actual=requested.resolve()
    if requested.drive.upper()!='F:' or actual.drive.upper()!='F:':raise ValueError('Candidate storage must remain on F:')
    return actual

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def authored(source,target):
    git=['git','-c','safe.directory='+source.as_posix(),'-C',str(source)]
    metadata=subprocess.check_output([*git,'rev-parse','--absolute-git-dir'],text=True).strip();physical(metadata)
    names=subprocess.check_output([*git,'ls-files','-z','--cached','--others','--exclude-standard']).decode('utf-8').split('\0')
    for name in sorted(set(names)-{''}):
        relative=Path(name)
        if any(part.lower() in EXCLUDE for part in relative.parts) or relative.suffix.lower() in SUFFIXES:continue
        if relative.name.startswith('.env') and relative.name!='.env.example':continue
        source_file=source/relative
        if not source_file.is_file():continue
        if not physical(source_file).is_relative_to(source):raise ValueError('Source file escapes repository')
        destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source_file,destination)
    return {'source':str(source),'head':subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip()}

def package(task_root,source_root,output):
    task_root,source_root,output=map(physical,(task_root,source_root,output))
    if output.exists():raise ValueError('Candidate destination already exists; choose a new directory')
    if shutil.disk_usage(task_root).free<1024**3:raise ValueError('Candidate requires 1 GiB free on F:')
    output.mkdir(parents=True);sources=[]
    for family,web in [('platform','apps/portal/dist'),('ocr','frontend/dist'),('classification','apps/web/dist')]:
        source=physical(source_root/family);target=output/'sources'/family
        sources.append({'family':family,**authored(source,target)})
        if not (source/web/'index.html').is_file():raise ValueError('Missing native production frontend: '+family)
        shutil.copytree(source/web,target/web)
    shutil.copytree(task_root/'dependencies/wheelhouse',output/'wheelhouse')
    licenses=output/'licenses';licenses.mkdir()
    for info in (task_root/'dependencies/python').glob('*.dist-info'):
        for file in info.rglob('*'):
            if file.is_file() and ('license' in file.name.lower() or file.name in {'METADATA','NOTICE'} or 'licenses' in file.parts):
                destination=licenses/'python'/info.name/file.relative_to(info);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,destination)
    pg=task_root/'dependencies/postgresql/pgsql'
    shutil.copy2(pg/'server_license.txt',licenses/'postgresql-server-license.txt')
    program=physical('F:/OCR/output/clear-automation-dev-20261002/program')
    agent=physical('F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    shared=[{'kind':'verified-base-program','path':str(program),'declaration_sha256':sha(program/'dependency-manifest.json')},
            {'kind':'agent-layer','path':str(agent)},
            {'kind':'task-python-layer','path':str(task_root/'dependencies/python')},
            {'kind':'postgresql-binaries','path':str(pg),'postgres_sha256':sha(pg/'bin/postgres.exe')},
            {'kind':'postgresql-official-zip','path':str(task_root/'dependencies/postgresql-17.11-5.zip'),
             'url':'https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64-binaries.zip',
             'sha256':sha(task_root/'dependencies/postgresql-17.11-5.zip')}]
    (output/'dependencies.json').write_text(json.dumps({'format':'clear-task-shared-dependencies-v1','standalone':False,'shared':shared},ensure_ascii=False,indent=2),'utf-8')
    # Runtime configuration and TLS keys stay in the private restored workspace;
    # neither credentials nor business files are part of this source candidate.
    launch=r'''param([Parameter(Mandatory=$true)][string]$Data,[Parameter(Mandatory=$true)][string]$PrivateConfig,[Parameter(Mandatory=$true)][string]$Certificate,[Parameter(Mandatory=$true)][string]$PrivateKey,[string]$PublicOrigin='https://localhost:8901',[int]$Port=8901,[int]$WorkerPort=8902)
$ErrorActionPreference='Stop'
$taskStorageRoot='__TASK_ROOT__'
$candidateRoot=$PSScriptRoot
foreach($value in @($candidateRoot,$Data,$PrivateConfig,$Certificate,$PrivateKey)) { if(-not [IO.Path]::GetFullPath($value).StartsWith('F:\')) {throw 'All paths must remain on F:'} }
$cache=Join-Path $Data 'cache';$temporary=Join-Path $Data 'temp'
New-Item -ItemType Directory -Force -Path $cache,$temporary,(Join-Path $cache 'localappdata'),(Join-Path $cache 'appdata') | Out-Null
$env:TEMP=$temporary;$env:TMP=$temporary;$env:TMPDIR=$temporary
$env:LOCALAPPDATA=Join-Path $cache 'localappdata';$env:APPDATA=Join-Path $cache 'appdata'
$env:PIP_CACHE_DIR=Join-Path $cache 'pip';$env:npm_config_cache=Join-Path $cache 'npm'
$env:HF_HOME=Join-Path $cache 'huggingface';$env:TORCH_HOME=Join-Path $cache 'torch'
$env:PYTHONDONTWRITEBYTECODE='1';$env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8'
$env:CLEAR_TASK_ROOT=$taskStorageRoot;$env:CLEAR_TASK_LAYER=Join-Path $taskStorageRoot 'dependencies/python'
$source=Join-Path $candidateRoot 'sources/platform'
Push-Location -LiteralPath $source
try { & '__PYTHON__' -B scripts/task_python.py scripts/serve_task_stack.py --program '__PROGRAM__' --data $Data --private-config $PrivateConfig --dependency-layer $env:CLEAR_TASK_LAYER --postgres-bin '__PG_BIN__' --port $Port --worker-port $WorkerPort --public-origin $PublicOrigin --certificate $Certificate --private-key $PrivateKey --agent-layer '__AGENT__'; exit $LASTEXITCODE } finally { Pop-Location }
'''
    for name,value in {'TASK_ROOT':task_root,'PYTHON':program/'runtimes/platform/python.exe','PROGRAM':program,'PG_BIN':pg/'bin','AGENT':agent}.items():launch=launch.replace('__'+name+'__',str(value).replace("'","''"))
    (output/'启动候选.ps1').write_text(launch,'utf-8-sig')
    (output/'README.md').write_text('# 多人任务中心候选\n\n本目录冻结三仓自有源码、三个前端 dist、Python wheelhouse 与许可。共享依赖见 dependencies.json；此目录不是独立全模型离线包。\n\n部署及恢复说明：sources/platform/docs/task-center-operations.md。启动时明确传入新的 F 盘恢复工作区、受限 runtime 配置和 TLS 证书，示例端口 8901/8902。测试证书仅供本机；正式入口未切换。\n','utf-8')
    records=[{'path':file.relative_to(output).as_posix(),'size_bytes':file.stat().st_size,'sha256':sha(file)} for file in sorted(output.rglob('*')) if file.is_file()]
    manifest={'format':'clear-task-candidate-v1','created_at':datetime.now(timezone.utc).isoformat(),'sources':sources,'files':records,'standalone':False}
    (output/'candidate-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    for record in records:
        if sha(output/record['path'])!=record['sha256']:raise ValueError('Candidate copy verification failed')
    return {'candidate':str(output),'files':len(records),'verified':True,'standalone':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--task-root',required=True);parser.add_argument('--source-root',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    print(json.dumps(package(args.task_root,args.source_root,args.output)))
