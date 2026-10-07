"""Package Clear intelligent audit with shared F-drive inference assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from workbench_platform.supervisor.processes import require_f_path
from scripts.product_compatibility import DATA_COMPATIBILITY


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def copy_tree(source,target):
    source=require_f_path(source);target=require_f_path(target)
    for item in source.rglob('*'):
        if item.is_symlink() or item.is_junction():
            raise ValueError('Candidate inputs must not contain links: '+str(item))
    shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.pytest_cache','.git'))


def skills_manifest(root, layer):
    """Validate packaged Skill bytes and required tools on an isolated F root."""
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.agent.skills import SkillService
    temp = require_f_path(Path(os.environ['TEMP']), 'Packaging temporary directory')
    os.environ['WORKBENCH_AGENT_LAYER'] = str(require_f_path(layer))
    with tempfile.TemporaryDirectory(prefix='skill-package-', dir=temp) as directory:
        runtime = AgentRuntime(Path(directory), SimpleNamespace())
        host = SkillService(runtime.store, roots=[root], registry=runtime.registry)
        catalog = host.catalog('package-validation', limit=50)
        if catalog['rejected'] or not catalog['items'] or any(not item['compatible'] for item in catalog['items']):
            raise ValueError('Packaged Skill registration is invalid or incompatible')
        return {'format': 'clear-skills-manifest-v1', 'host': 'clear-workbench',
                'scripts_supported': False, 'skills': catalog['items']}


def package(source,program,layer,output,engineering=False,pdf_layer=None):
    source,program,layer,output=[require_f_path(p) for p in (source,program,layer,output)]
    pdf_relative='build/public-quality-20260916/tool/site-packages/pdfplumber'
    pdf_layer=require_f_path(pdf_layer if pdf_layer is not None else source.parent/'ocr'/pdf_relative)
    if not (pdf_layer/'__init__.py').is_file():
        raise ValueError('Verified PDF dependency missing; supply its read-only F-drive source with --pdf-layer')
    if output.exists():
        raise ValueError('Use one new candidate directory; existing candidates are never overwritten')
    if not (source/'apps/portal/dist/index.html').is_file():
        raise ValueError('Production portal build missing')
    for family, relative in [('ocr', 'frontend/dist'), ('classification', 'apps/web/dist')]:
        if not (source.parent / family / relative / 'index.html').is_file():
            raise ValueError('Production native build missing: ' + family)
    output.mkdir(parents=True)
    for name in ('src','packages','modules','config','scripts','apps/portal/dist','docs/agent'):
        copy_tree(source/name,output/'platform'/name)
    registered_skills = skills_manifest(output/'platform/config/skills', layer)
    (output/'skills.json').write_text(json.dumps(registered_skills,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for family,names in [('ocr',('src','config','frontend/dist')),
                         ('classification',('src','configs','apps/web/dist'))]:
        for name in names:
            copy_tree(source.parent/family/name,output/family/name)
    copy_tree(pdf_layer,output/'ocr'/pdf_relative)
    copy_tree(layer,output/'dependencies/agent')
    receipt=output/'dependencies/agent/layer-receipt.json'
    declaration=json.loads(receipt.read_text(encoding='utf-8'))
    declaration['candidate_destination']=str(output/'dependencies/agent')
    declaration['relocated_from_verified_layer']=str(layer)
    receipt.write_text(json.dumps(declaration,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for name in ('pyproject.toml',):
        shutil.copy2(source/name,output/'platform'/name)
    launcher='''import argparse,json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
sys.path[:0]=[str(root/'platform'),str(root/'platform/src')]
from workbench_platform.supervisor.processes import require_f_path,storage_environment
from scripts.package_agent_candidate import sha
manifest=json.loads((root/'candidate.json').read_text(encoding='utf-8'))
for name,expected in manifest['files'].items():
    member=require_f_path(root/name)
    if not member.is_relative_to(root) or sha(member)!=expected:
        raise ValueError('Candidate file changed: '+name)
for name,expected in manifest['shared_identity'].items():
    if sha(require_f_path(Path(name)))!=expected:
        raise ValueError('Shared runtime/declaration changed: '+name)
parser=argparse.ArgumentParser()
parser.add_argument('--data',type=Path,default=root/'workspace')
parser.add_argument('--port',type=int,default=8879)
parser.add_argument('--disable-agent',action='store_true')
args=parser.parse_args()
data=require_f_path(args.data)
from scripts.product_compatibility import check
check(data,manifest['data_compatibility'])
os.environ.update(storage_environment(data))
os.environ['WORKBENCH_AGENT_LAYER']=str(root/'dependencies/agent')
os.environ['WORKBENCH_AGENT_ENABLED']='0' if args.disable_agent else '1'
from scripts.agent_native_preview import main
sys.argv=['agent-candidate','--source',str(root/'platform'),'--program',manifest['shared_program'],'--data',str(data),'--port',str(args.port)]
main()
'''
    (output/'launch.py').write_text(launcher,encoding='utf-8')
    runtime=program/'runtimes/platform/python.exe'
    launch_cmd = '@echo off\n"'+str(runtime)+'" -I -B -u "%~dp0launch.py" %*\n'
    for name in ('启动智能审计.cmd', '启动平台Agent.cmd'):
        (output/name).write_text(launch_cmd,encoding='utf-8')
    (output/'README.md').write_text('''# Clear 智能审计

默认首页为大模型对话与审计任务工作台，四类 Clear 手动工具继续可用。
双击“启动智能审计.cmd”，访问 http://127.0.0.1:8879/ 。默认使用本目录 workspace，所有缓存/临时在 F 盘。
首页可保存目标、上传资料、接管任务；连接支持工具调用的大模型后可自动执行。主控设置使用独立 DPAPI 存储。
查看 platform/docs/agent/README.md 获取能力、手动接续、底稿及迁移说明。真实模型调用需用户填写有效配置；功能检查不冒充真实模型验证。
使用 --data 指定新的 F 盘工作区；--port 改端口；--disable-agent 关闭 Agent 保留普通工具。
关闭终端以停止服务。冷备前关闭平台和原生服务，使用 platform/scripts/backup_workspace.py 和分类 backup 模块。
原生模型/运行时来自 candidate.json 登记的共享 F 盘 program；该目录必须保留。未复制32GB模型，未改共享绑定。
不要用旧版本直接打开新数据库。关闭Agent是功能回退，备份恢复到新目录是数据恢复。
''',encoding='utf-8')
    if engineering:
        readme=output/'README.md'
        readme.write_text(readme.read_text(encoding='utf-8').replace('# Clear 智能审计',
            '# Clear 智能审计 — 工程验证候选\n\n本候选未冻结最终 OCR 路线与质量评估，不登记为 current。最终交付须重新按冻结源码打包。',1),encoding='utf-8')
    identity={}
    for name in ('runtimes/platform/python.exe','runtimes/platform/python312._pth',
                 'runtimes/data/python.exe','runtimes/forensics/python.exe',
                 'bundles/ocr/bundle.manifest.json','bundles/classification/bundle.manifest.json',
                 'bundles/ocr/runtimes/control/python.exe','bundles/classification/runtimes/control/python.exe'):
        identity[str(program/name)]=sha(program/name)
    files={p.relative_to(output).as_posix():sha(p) for p in sorted(output.rglob('*')) if p.is_file()}
    sources={}
    for family,path in [('platform',source),('ocr',source.parent/'ocr'),('classification',source.parent/'classification')]:
        head=subprocess.check_output(['git','-c','safe.directory='+path.as_posix(),'rev-parse','HEAD'],cwd=path,text=True).strip()
        sources[family]={'path':str(path),'base_head':head,'candidate_files_are_current_working_contents':True}
    manifest={'format':1,'status':'engineering_candidate' if engineering else 'intelligent_audit_product',
        'release_stage':'engineering' if engineering else 'final','product_version':'0.9.4',
        'controller_configuration_required':True,'shared_program':str(program),'sources':sources,
        'readiness_features':['screen-batch-v1','screen-directives-v1','per-image-excel-zip-v1'],
        'data_compatibility': DATA_COMPATIBILITY,
        'skills_manifest': 'skills.json', 'skills_count': len(registered_skills['skills']),
        'frontend_roots': {'portal': 'platform/apps/portal/dist', 'ocr': 'ocr/frontend/dist', 'classification': 'classification/apps/web/dist'},
        'shared_assets_read_only':True,'copies_models':False,'shared_identity':identity,'files':files}
    (output/'candidate.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'candidate':str(output),'files':len(files),'bytes':sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),'copies_models':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','program','layer','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--engineering',action='store_true',help='Clearly label an unfrozen engineering candidate; never registers current')
    parser.add_argument('--pdf-layer',type=Path,help='Read-only verified pdfplumber source on F: when isolated OCR source omits build dependencies')
    print(json.dumps(package(**vars(parser.parse_args())),ensure_ascii=False))
