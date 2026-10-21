"""Allowlisted mapping of platform execution APIs to durable commands."""
import re
from urllib.parse import quote
from .store import LedgerError

P='/api/platform/v1'
_ID=r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}'

def task_label(tool,action):
    tools={'documents':'文档解析','ocr':'OCR','visual_review':'视觉审校','classification':'分类','data':'数据处理','forensics':'图像取证','face':'人脸比对','audio':'音频分析','seal':'印章分析','catnet':'深度取证','agent':'智能审计','batch':'批次','assets':'资料','artifact':'成果'}
    actions={'import':'导入','export':'导出','recognize':'识别','analyze':'分析','train':'训练','predict':'预测','evaluate':'评估','message':'执行','execute':'执行'}
    return tools.get(tool,tool)+' · '+actions.get(action,action)

def match(method,path,payload):
    if method not in {'POST','GET'}: return None
    patterns=[
        ('POST',rf'{P}/workspaces/({_ID})/documents/parse','documents','analyze'),
        ('POST',rf'{P}/data/sessions/({_ID})/actions','data','action'),
        ('POST',rf'{P}/data/sessions/({_ID})/asset-uploads','assets','import'),
        ('POST',rf'{P}/workspaces/({_ID})/assets/audio-uploads/({_ID})/complete','assets','import'),
        ('POST',rf'{P}/workspaces/({_ID})/multimodal/jobs','manual','analyze'),
        ('POST',rf'{P}/workspaces/({_ID})/forensics','forensics','analyze'),
        ('POST',rf'{P}/batches','batch','execute'),
        ('POST',rf'{P}/agent/sessions/({_ID})/messages','agent','message'),
        ('POST',rf'{P}/workspaces/({_ID})/ocr/handoffs/({_ID})/tasks','ocr','recognize'),
        ('POST',rf'{P}/workspaces/({_ID})/classification/handoffs/({_ID})/jobs','classification','predict'),
        ('POST',rf'{P}/workspaces/({_ID})/artifacts/(ocr|classification|data)-exports','artifact','export'),
        ('POST',rf'{P}/workspaces/({_ID})/forensics/({_ID})/collect','forensics','export'),
        ('POST',rf'{P}/workspaces/({_ID})/multimodal/jobs/({_ID})/collect','artifact','export'),
        ('GET',rf'{P}/workspaces/({_ID})/forensics/({_ID})/download','forensics','export'),
        ('GET',rf'{P}/workspaces/({_ID})/multimodal/jobs/({_ID})/download','manual','export'),
    ]
    for verb,pattern,tool,action in patterns:
        found=re.fullmatch(pattern,path)
        if verb==method and found:
            if tool=='manual': tool=payload.get('kind','audio')
            if action=='action': action=payload.get('action','process')
            return {'tool_id':tool,'action':action,'method':method,'path':path,'payload':payload,'groups':list(found.groups()),**({'resource_class':'io'} if action=='export' else {})}
    return None

def command_for(value):
    tool=value['tool_id']; action=value['action']; options=value.get('options',{})
    workspace=quote(value['workspace_id'],safe='')
    payload=dict(options.get('payload',options))
    if tool in {'face','audio','seal','catnet'}:
        path=f'{P}/workspaces/{workspace}/multimodal/jobs'
        payload={'schema_version':'1.0','kind':tool,'request_id':value['idempotency_key'],'inputs':value['inputs'],'options':options}
    elif tool=='documents':
        path=f'{P}/workspaces/{workspace}/documents/parse'; payload.setdefault('request_id',value['idempotency_key']); payload.setdefault('schema_version','1.0')
        if value['inputs']: payload.update({name:value['inputs'][0][name] for name in ('asset_id','asset_revision','blob_hash')})
    elif tool=='forensics':
        path=f'{P}/workspaces/{workspace}/forensics'; payload.setdefault('request_id',value['idempotency_key']); payload.setdefault('schema_version','1.0')
        if value['inputs']: payload.update({name:value['inputs'][0][name] for name in ('asset_id','asset_revision','blob_hash')})
    elif tool=='data':
        session=options.get('session_id','')
        if not re.fullmatch(_ID,session): raise LedgerError('Data 任务需要有效 session_id')
        path=f'{P}/data/sessions/{session}/actions'
        payload={'action':action,'payload':options.get('payload',{}),'version':options.get('version')}
    elif tool=='batch': path=f'{P}/batches'
    elif tool=='agent':
        session=options.get('session_id','')
        if not re.fullmatch(_ID,session): raise LedgerError('Agent 任务需要有效 session_id')
        path=f'{P}/agent/sessions/{session}/messages'
        payload=options.get('payload',{})
        payload.setdefault('schema_version','1.0');payload.setdefault('request_id',value['idempotency_key'])
    elif tool in {'ocr','classification'}:
        handoff=options.get('handoff_id','')
        if not re.fullmatch(_ID,handoff): raise LedgerError('原生任务需要有效 handoff_id')
        suffix='tasks' if tool=='ocr' else 'jobs'
        path=f'{P}/workspaces/{workspace}/{tool}/handoffs/{handoff}/{suffix}'
    else: raise LedgerError('此工具请通过对应工作台发起执行')
    command=match('POST',path,payload)
    if command is None: raise LedgerError('任务操作尚未登记')
    if command['action']!=action:raise LedgerError('此操作请从对应工具工作台发起，或使用该工具支持的任务操作')
    return command

def binding(command,body):
    """Return source identity and polling path from the actual durable receipt."""
    tool=command['tool_id']; groups=command.get('groups',[])
    if tool=='documents':
        identifier=body.get('document_id')
        return ('documents.documents',identifier,f'{P}/workspaces/{groups[0]}/documents/{identifier}') if identifier else None
    if tool=='data':
        identifier=body.get('id') or body.get('job_id')
        return ('data.jobs',identifier,f'{P}/data/sessions/{groups[0]}/jobs/{identifier}') if identifier else None
    if tool in {'face','audio','seal','catnet'}:
        identifier=body.get('job_id')
        return ('manual.manual_analysis',identifier,f'{P}/workspaces/{groups[0]}/multimodal/jobs/{identifier}') if identifier else None
    if tool=='forensics' and command['action']!='export':
        identifier=body.get('inspection_id')
        return ('forensics.inspections',identifier,f'{P}/workspaces/{groups[0]}/forensics/{identifier}') if identifier else None
    if tool=='batch':
        identifier=body.get('batch_id')
        return ('batches.batches',identifier,f'{P}/batches/{identifier}') if identifier else None
    if tool=='agent':
        identifier=body.get('run_id') or body.get('id')
        return ('agent.runs',identifier,f'{P}/agent/runs/{identifier}') if identifier else None
    if tool in {'ocr','classification'}:
        ref=body.get('new_job') or body.get('job') or body.get('job_ref') or body
        identifier=ref.get('platform_job_id')
        return ('projection',identifier,f'{P}/workspaces/{groups[0]}/jobs/{identifier}') if identifier else None
    return None
