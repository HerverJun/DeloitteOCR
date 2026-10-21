"""Local Agent APIs. Clients cannot choose checkpoint IDs or native hosts."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from packages.contracts.agent import AgentSessionCreate, AgentMessage, AgentReply, AgentCancel, ControllerDraft, AgentReferenceRequest, AgentRef, AgentTaskWrite, AgentTaskControl, AuditProjectProfile, AgentManualReturn, AgentNativeAttach
from packages.contracts.audit import DirectEvidenceRequest, AuditDraftWrite, AuditDraftExport
from workbench_platform.assets.routes import json_body, library
from workbench_platform.workspaces.routes import _loopback_host, _same_loopback_origin
from .connection import probe
from .providers import ProviderFault
from .registry import invoke
from workbench_platform.application_services.context import SessionContext as ToolContext
from .runtime import AgentRuntime
from .store import Conflict

router = APIRouter(prefix='/api/platform/v1', tags=['platform-agent'])


async def configure_agent(app):
    app.state.agent = None
    app.state.agent_unavailable_reason = 'Agent 尚未启用或未配置独立依赖层'
    root = os.environ.get('WORKBENCH_DATA_DIR')
    if not root:
        return
    runtime = None
    try:
        runtime = AgentRuntime(Path(root) / 'agent', app.state)
        app.state.agent = runtime
        await runtime.start()
        app.state.agent_unavailable_reason = ''
    except (ImportError, FileNotFoundError, ValueError):
        if runtime:
            await runtime.close()
        app.state.agent = None


def runtime(request, write=False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, '请从本机平台页面操作')
    value = getattr(request.app.state, 'agent', None)
    if value is None:
        raise HTTPException(503, getattr(request.app.state, 'agent_unavailable_reason', 'Agent 不可用'))
    return value


async def parsed(request, schema):
    try:
        return schema.model_validate(await json_body(request, 65536))
    except ValidationError:
        raise HTTPException(400, '参数无效，请检查字段和范围') from None


def session_scope(agent, request, session_id, write=False):
    try:
        value = agent.store.session(session_id)
    except ValueError:
        raise HTTPException(404, '会话不存在') from None
    library(request, value['workspace_id'], write=write)
    return value


def run_scope(agent, request, run_id, write=False):
    try:
        run = agent.store.run(run_id)
    except ValueError:
        raise HTTPException(404, '运行不存在') from None
    session_scope(agent, request, run['session_id'], write)
    return run


@router.get('/agent/health')
def health(request: Request):
    if not _loopback_host(request):
        raise HTTPException(403, '请从本机平台页面操作')
    value = getattr(request.app.state, 'agent', None)
    return {'enabled': value is not None and value.agent_enabled, 'direct_enabled': value is not None,
            'engine': 'deepseek-harness' if value is not None and value.agent_enabled else None,
            'engine_version': '0.2.1-alpha.4' if value is not None and value.agent_enabled else None,
            'reason': value.agent_unavailable_reason if value is not None else
                      getattr(request.app.state, 'agent_unavailable_reason', '')}


@router.get('/agent/connection')
def connection(request: Request):
    return runtime(request).connection.view()


@router.get('/agent/integrations')
def integrations(request: Request):
    from .integrations import view
    return view(runtime(request))


@router.put('/agent/integrations')
async def save_integrations(request: Request):
    from .integrations import Integrations, save
    agent = runtime(request, write=True)
    draft = await parsed(request, Integrations)
    try:
        return save(agent, draft)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.put('/agent/connection')
async def save_connection(request: Request):
    value = runtime(request, True)
    settings = await parsed(request, ControllerDraft)
    try:
        return await value.connection.save(settings.model_dump())
    except Conflict:
        raise HTTPException(409, '连接已在另一窗口改变，请重新测试') from None
    except ProviderFault as error:
        raise HTTPException(400, str(error)) from None
    except ValueError:
        raise HTTPException(400, '连接参数或凭据无效，请检查地址、模型和 API Key') from None


@router.delete('/agent/connection')
def clear_connection(request: Request):
    return runtime(request, True).connection.clear()


@router.post('/agent/connection/probe')
async def probe_connection(request: Request):
    value = runtime(request, True)
    settings = await parsed(request, ControllerDraft)
    try:
        config, secret, _ = value.connection.draft(settings.model_dump())
        local_check = value.connection.preflight(config)
        capabilities = await probe(config, secret)
        return {**capabilities, 'production_preflight': local_check}
    except ProviderFault as error:
        raise HTTPException(400, str(error)) from None
    except ValueError:
        raise HTTPException(400, '连接参数或凭据无效') from None


@router.get('/workspaces/{workspace_id}/agent/capabilities')
def capabilities(workspace_id: str, request: Request):
    value = runtime(request)
    library(request, workspace_id)
    return {'tools': [value.registry.describe(name, value.services) for name in value.registry.tools],
            'capabilities': value.registry.capability_catalog(value.services),
            'controller': value.connection.view()}


@router.post('/agent/connection/models')
async def models(request: Request):
    value = runtime(request, True)
    payload = await json_body(request, 65536)
    try:
        return await value.connection.models(payload)
    except ProviderFault as error:
        raise HTTPException(400, str(error)) from None
    except ValueError:
        raise HTTPException(400, '模型列表或连接参数无效；可以手填模型ID') from None


@router.post('/workspaces/{workspace_id}/agent/sessions', status_code=201)
async def create_session(workspace_id: str, request: Request):
    value = runtime(request, True)
    registry, _, _ = library(request, workspace_id, write=True)
    payload = await parsed(request, AgentSessionCreate)
    try:
        for asset_id in payload.selected_assets:
            registry.get(workspace_id, asset_id)
        from workbench_platform.application_services.references import verify
        context = ToolContext(workspace_id, '', '', 0, 0, '', tuple(payload.selected_assets), value.services, value.store)
        references = [ref.model_dump() for ref in payload.initial_refs]
        for ref in references:
            verify(ref, context)
        from .tools.multimodal import validate_reference_ids
        validate_reference_ids(value.services, workspace_id, payload.authorized_reference_ids)
        return value.store.create_session(workspace_id, payload.name, payload.selected_assets, payload.request_id,
                                          initial_refs=references, authorized_reference_ids=payload.authorized_reference_ids,
                                          delivery_policy=payload.delivery_policy)
    except Conflict:
        raise HTTPException(409, '创建请求与原会话不同') from None
    except ValueError:
        raise HTTPException(400, '选中资料不属于当前项目') from None


@router.post('/workspaces/{workspace_id}/agent/references')
async def resolve_reference(workspace_id: str, request: Request):
    value = runtime(request, True)
    library(request, workspace_id, write=True)
    payload = await parsed(request, AgentReferenceRequest)
    context = ToolContext(workspace_id, '', '', 0, 0, '', (), value.services, value.store)
    from workbench_platform.application_services.references import verify, data_reference
    try:
        if payload.kind == 'artifact':
            ref = AgentRef(kind='artifact', workspace_id=workspace_id, object_id=payload.object_id, revision=1)
            checked = await invoke(verify, ref.model_dump(), context)
            ref.sha256 = checked['content_sha256']
        else:
            if not payload.parent_id:
                raise ValueError('数据引用需要作业来源')
            checked = await invoke(data_reference, context, payload.parent_id, payload.kind, payload.object_id)
            ref = AgentRef(kind=payload.kind, workspace_id=workspace_id, object_id=payload.object_id,
                revision=payload.object_id, parent_id=payload.parent_id, application='data', sha256=checked['content_sha256'])
        return {'reference': ref.model_dump()}
    except (ValueError, KeyError):
        raise HTTPException(400, '成果、来源或版本不可核实') from None


@router.get('/workspaces/{workspace_id}/agent/sessions')
def sessions(workspace_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=50)):
    value = runtime(request)
    library(request, workspace_id)
    rows = value.store.rows('SELECT id,name,created FROM sessions WHERE workspace_id=? ORDER BY created DESC LIMIT ? OFFSET ?',
                            (workspace_id, limit, offset))
    return {'items': rows, 'next_offset': offset + limit if len(rows) == limit else None}


@router.get('/agent/sessions/{session_id}')
def session(session_id: str, request: Request):
    value = runtime(request)
    result = session_scope(value, request, session_id)
    from .task_recovery import can_resume_run
    runs = value.store.rows('SELECT * FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 20', (session_id,))
    return {**result, 'runs': [{**run, 'can_resume': can_resume_run(value, run)} for run in runs]}


@router.post('/agent/sessions/{session_id}/messages', status_code=202)
async def message(session_id: str, request: Request):
    value = runtime(request, True)
    session_scope(value, request, session_id, True)
    payload = await parsed(request, AgentMessage)
    try:
        result = await value.submit(session_id, payload.content, payload.request_id)
        value.tasks.record_goal(session_id, payload.content, payload.request_id)
        return result
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.post('/agent/sessions/{session_id}/direct/image-text', status_code=202)
async def direct_image_text(session_id: str, request: Request):
    from workbench_platform.application_services.image_text import ImageTextRequest, submit
    value = runtime(request, True)
    session_scope(value, request, session_id, True)
    payload = await parsed(request, ImageTextRequest)
    try:
        return await submit(value, session_id, payload)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


async def direct_request(session_id, request, module_name, schema_name):
    import importlib
    module = importlib.import_module('workbench_platform.application_services.' + module_name)
    value = runtime(request, True)
    session_scope(value, request, session_id, True)
    payload = await parsed(request, getattr(module, schema_name))
    try:
        return await module.submit(value, session_id, payload)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.post('/agent/sessions/{session_id}/direct/pdf-text', status_code=202)
async def direct_pdf_text(session_id: str, request: Request):
    return await direct_request(session_id, request, 'pdf_text', 'PdfTextRequest')


@router.post('/agent/sessions/{session_id}/direct/data-action', status_code=202)
async def direct_data_action(session_id: str, request: Request):
    return await direct_request(session_id, request, 'data_action', 'DataActionRequest')


@router.post('/agent/sessions/{session_id}/direct/classification-predict', status_code=202)
async def direct_classification_predict(session_id: str, request: Request):
    return await direct_request(session_id, request, 'classification_action', 'PredictRequest')


@router.get('/agent/sessions/{session_id}/jobs')
async def session_jobs(session_id: str, request: Request):
    from .store import now
    value = runtime(request)
    session = session_scope(value, request, session_id)
    records = value.store.rows('SELECT o.result,r.id AS run_id,r.generation FROM operations o '
        'JOIN runs r ON r.id=o.run_id WHERE r.session_id=? AND o.result IS NOT NULL ORDER BY o.created', (session_id,))
    jobs = {}
    for record in records:
        ctx = ToolContext(session['workspace_id'], session_id, record['run_id'], record['generation'], 0, '',
                          tuple(session['selected_assets']), value.services, value.store)
        for ref in json.loads(record['result']).get('job_refs', []):
            # Read-only snapshots of terminal runs use the same ownership check
            # without reopening the run or authorizing a new operation.
            from .progress import snapshot_for
            progress = await invoke(snapshot_for, ref, ctx)
            jobs[(ref['workspace_id'], ref['application'], ref.get('parent_id'), ref['object_id'])] = {**progress, 'job_ref': ref,
                'run_id': record['run_id'], 'generation': record['generation'], 'sampled_at': now()}
    return {'jobs': list(jobs.values()), 'sampled_at': now()}


@router.get('/agent/sessions/{session_id}/results/text')
async def session_text(session_id: str, request: Request, reference: str = Query(max_length=16000),
                       offset: int = Query(0, ge=0), limit: int = Query(8000, ge=1, le=32000)):
    from workbench_platform.application_services.context import snapshot_context
    from workbench_platform.application_services.references import verify
    from workbench_platform.artifacts.text_outputs import text_page, verified_text
    value = runtime(request)
    session = session_scope(value, request, session_id)
    try:
        ref = AgentRef.model_validate(json.loads(reference)).model_dump(mode='json')
        if ref['application'] != 'ocr' or ref['kind'] != 'result':
            raise ValueError('不是可读文字结果')
        rows = value.store.rows('SELECT o.result,r.* FROM operations o JOIN runs r ON r.id=o.run_id '
                               'WHERE r.session_id=? AND o.result IS NOT NULL ORDER BY o.created DESC', (session_id,))
        owner = next((row for row in rows if ref in json.loads(row['result']).get('result_refs', [])), None)
        if owner is None:
            raise ValueError('结果不属于本会话')
        ctx = snapshot_context(ToolContext(session['workspace_id'], session_id, owner['id'], owner['generation'], 0, '',
                        tuple(session['selected_assets']), value.services, value.store))
        result = await invoke(verify, ref, ctx)
        return {**text_page(verified_text(result), offset=offset, limit=limit), 'reference': ref}
    except (ValueError, KeyError, TypeError):
        raise HTTPException(404, '固定文字结果暂不可读取') from None


@router.get('/agent/sessions/{session_id}/events')
def events(session_id: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    value = runtime(request)
    session_scope(value, request, session_id)
    return value.store.events(session_id, after, limit)


@router.get('/agent/sessions/{session_id}/stream')
async def stream(session_id: str, request: Request, after: int = Query(0, ge=0)):
    value = runtime(request)
    session_scope(value, request, session_id)
    header = request.headers.get('last-event-id', '')
    if header:
        if not header.isdecimal():
            raise HTTPException(400, '事件游标无效')
        after = max(after, int(header))
    async def chunks():
        cursor = after
        wakeup = value.store.watch(session_id)
        next_keepalive = 0
        try:
            while not value.closing and not await request.is_disconnected():
                # Clear before reading the durable cursor. A commit before or
                # during this query is either returned by SQL or leaves our
                # private wakeup set; other subscribers cannot clear it.
                wakeup.clear()
                page = await asyncio.to_thread(value.store.events, session_id, cursor, 50)
                for event in page['items']:
                    yield 'id: ' + str(event['seq']) + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                cursor = page['next_cursor']
                if not page['items']:
                    current_time = asyncio.get_running_loop().time()
                    if current_time >= next_keepalive:
                        yield ': keepalive\n\n'
                        next_keepalive = current_time + 15
                    try:
                        # Also reconcile SQL-only historical writers within
                        # 0.5 s; notification improves latency, not correctness.
                        await asyncio.wait_for(wakeup.wait(), 0.5)
                    except TimeoutError:
                        pass
        finally:
            value.store.unwatch(wakeup)
    return StreamingResponse(chunks(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@router.get('/agent/runs/{run_id}')
def run(run_id: str, request: Request):
    value = runtime(request)
    from .task_recovery import can_resume_run
    item = run_scope(value, request, run_id)
    return {**item, 'can_resume': can_resume_run(value, item)}


@router.post('/agent/runs/{run_id}/cancel')
async def cancel(run_id: str, request: Request):
    value = runtime(request, True)
    run_scope(value, request, run_id, True)
    payload = await parsed(request, AgentCancel)
    # cancel fences the run and signals its asyncio-owned Harness process.
    # Moving it to a worker thread loses the running event loop after commit.
    return value.cancel(run_id, payload.cancel_owned_jobs)


@router.post('/agent/runs/{run_id}/resume')
async def resume(run_id: str, request: Request):
    value = runtime(request, True)
    item = run_scope(value, request, run_id, True)
    if await json_body(request) != {}:
        raise HTTPException(400, '恢复参数无效')
    try:
        return await value.reconcile(run_id)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(409, '原生回执暂不可核实，未重新提交') from None


@router.post('/agent/runs/{run_id}/pause')
async def pause(run_id: str, request: Request):
    value = runtime(request, True)
    run_scope(value, request, run_id, True)
    if await json_body(request) != {}:
        raise HTTPException(400, '暂停参数无效')
    try:
        return await value.pause(run_id)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None


@router.post('/agent/sessions/{session_id}/compact', status_code=202)
async def compact(session_id: str, request: Request):
    value = runtime(request, True)
    session_scope(value, request, session_id, True)
    if await json_body(request) != {}:
        raise HTTPException(400, '整理参数无效')
    try:
        return await value.compact(session_id)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ProviderFault as error:
        raise HTTPException(400, str(error)) from None


@router.post('/agent/decisions/{decision_id}/reply')
async def reply(decision_id: str, request: Request):
    value = runtime(request, True)
    payload = await parsed(request, AgentReply)
    decision = value.store.rows('SELECT run_id FROM decisions WHERE id=?', (decision_id,))
    if not decision:
        raise HTTPException(404, '问题不存在')
    run_scope(value, request, decision[0]['run_id'], True)
    try:
        return await value.reply(decision_id, payload.content, payload.revision, payload.request_id)
    except Conflict:
        raise HTTPException(409, '问题已变化、已答复或运行已停止') from None


@router.get('/workspaces/{workspace_id}/audit-tasks')
def audit_tasks(workspace_id: str, request: Request, keyword: str = Query('', max_length=100)):
    value = runtime(request)
    library(request, workspace_id)
    return value.tasks.list(workspace_id, keyword)


@router.get('/agent/sessions/{session_id}/task')
def audit_task(session_id: str, request: Request):
    value = runtime(request)
    session_scope(value, request, session_id)
    return value.tasks.get(session_id)


@router.put('/agent/sessions/{session_id}/task')
async def update_task(session_id: str, request: Request):
    value = runtime(request, True)
    session = session_scope(value, request, session_id, True)
    payload = await parsed(request, AgentTaskWrite)
    try:
        pending = value.tasks.get(session_id).get('pending_scope') or {}
        selected = payload.selected_assets if payload.selected_assets is not None else pending.get('selected_assets', session['selected_assets'])
        if payload.authorized_reference_ids is not None:
            from .tools.multimodal import validate_reference_ids
            validate_reference_ids(value.services, session['workspace_id'], payload.authorized_reference_ids)
        for identifier in selected:
            value.services.asset_library[0].get(session['workspace_id'], identifier)
        if payload.initial_refs is not None:
            from workbench_platform.application_services.references import verify
            ctx = ToolContext(session['workspace_id'], session_id, '', 0, 0, '', tuple(selected), value.services, value.store)
            for reference in payload.initial_refs:
                await invoke(verify, reference.model_dump(), ctx)
        return value.tasks.update(session_id, payload.model_dump(exclude_none=True))
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(400, '资料范围或结果引用无效') from None


@router.post('/agent/sessions/{session_id}/task/control')
async def task_control(session_id: str, request: Request):
    value = runtime(request, True)
    session_scope(value, request, session_id, True)
    payload = await parsed(request, AgentTaskControl)
    try:
        return await invoke(value.tasks.control, session_id, payload.model_dump())
    except Conflict as error:
        raise HTTPException(409, str(error)) from None


@router.get('/workspaces/{workspace_id}/audit-profile')
def audit_profile(workspace_id: str, request: Request):
    value = runtime(request)
    library(request, workspace_id)
    return value.tasks.profile(workspace_id)


@router.put('/workspaces/{workspace_id}/audit-profile')
async def update_profile(workspace_id: str, request: Request):
    value = runtime(request, True)
    library(request, workspace_id, write=True)
    payload = await parsed(request, AuditProjectProfile)
    try:
        return value.tasks.save_profile(workspace_id, payload.model_dump())
    except Conflict as error:
        raise HTTPException(409, str(error)) from None


def product_context(value, session):
    from workbench_platform.application_services.context import SessionContext
    return SessionContext(session['workspace_id'], session['id'], '', 0, 0, '', tuple(session['selected_assets']), value.services, value.store)


@router.get('/agent/sessions/{session_id}/manual-results')
async def manual_results(session_id: str, request: Request, offset: int = Query(0, ge=0),
                         limit: int = Query(50, ge=1, le=50), keyword: str = Query('', max_length=100),
                         related_only: bool = False):
    value = runtime(request)
    session = session_scope(value, request, session_id)
    from workbench_platform.application_services.manual import candidates
    return await invoke(candidates, product_context(value, session), offset, limit, keyword, related_only)


@router.get('/agent/sessions/{session_id}/native-results')
async def native_results(session_id: str, request: Request, application: str | None = None, link_id: str | None = None,
                         offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=50)):
    value = runtime(request)
    session = session_scope(value, request, session_id)
    links = [{'application': link.app_id, 'link_id': link.link_id, 'project_id': link.module_project_id}
             for link in value.services.workspace_store.list_links(session['workspace_id']) if link.app_id in {'ocr', 'classification'}]
    if not link_id:
        return {'links': links, 'items': [], 'next_offset': None}
    from .tools.native_catalog import Catalog, catalog
    try:
        answer = await invoke(catalog, Catalog(application=application, link_id=link_id, offset=offset, limit=limit), product_context(value, session))
    except ValueError:
        raise HTTPException(400, '原生目录或关联项目暂不可核实') from None
    return {'links': links, **answer['data'], 'next_offset': answer['next_offset']}


@router.post('/agent/sessions/{session_id}/native-results/attach')
async def attach_native_result(session_id: str, request: Request):
    from dataclasses import replace
    from .tools.native_catalog import Attach, attach_result
    value = runtime(request, True)
    session = session_scope(value, request, session_id, True)
    if not value.tasks.get(session_id)['manual']:
        raise HTTPException(409, '请先手动接管任务，再接回原生工具结果')
    payload = await parsed(request, AgentNativeAttach)
    ctx = replace(product_context(value, session), operation_id=payload.request_id)
    try:
        return await invoke(attach_result, Attach(**payload.model_dump(exclude={'request_id'})), ctx)
    except ValueError:
        raise HTTPException(400, '原生结果或原件来源暂不可核实') from None


@router.get('/workspaces/{workspace_id}/native-results/{binding_id}')
async def read_native_binding(workspace_id: str, binding_id: str, request: Request, session_id: str | None = None,
                              offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
    from .tools.native_catalog import Read, read_result
    from workbench_platform.application_services.native_mapping import binding
    value = runtime(request)
    library(request, workspace_id)
    if session_id:
        session = session_scope(value, request, session_id)
        if session['workspace_id'] != workspace_id:
            raise HTTPException(404, '原生结果不属于当前任务')
        ctx = product_context(value, session)
    else:
        ctx = ToolContext(workspace_id, '', '', 0, 0, '', (), value.services, value.store)
    try:
        imported = binding(ctx, binding_id)
        if imported is None:
            raise ValueError('回传身份不存在')
        result = await invoke(read_result, Read(binding_id=binding_id, offset=offset, limit=limit), ctx)
        return {**result, 'application': imported['application'], 'link_id': imported['link_id']}
    except ValueError:
        raise HTTPException(400, '结果或当前资料范围不可核实') from None


@router.post('/agent/sessions/{session_id}/manual-return')
async def manual_return(session_id: str, request: Request):
    value = runtime(request, True)
    session = session_scope(value, request, session_id, True)
    payload = await parsed(request, AgentManualReturn)
    from workbench_platform.application_services.references import verify
    try:
        context = product_context(value, session)
        for identifier in payload.include_asset_ids:
            value.services.asset_library[0].get(session['workspace_id'], identifier)
        if session['selected_assets']:
            from dataclasses import replace
            selected = tuple(dict.fromkeys([*session['selected_assets'], *payload.include_asset_ids]))
            if len(selected) > 100:
                raise ValueError('资料范围过大')
            context = replace(context, selected_assets=selected)
        for reference in payload.references:
            await invoke(verify, reference.model_dump(), context)
        return value.tasks.return_results(session_id, payload.model_dump())
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(400, '带回结果的来源、范围或版本不可核实') from None


@router.get('/workspaces/{workspace_id}/audit-overview')
async def audit_overview(workspace_id: str, request: Request):
    value = runtime(request)
    registry, _, _ = library(request, workspace_id)
    ctx = ToolContext(workspace_id, '', '', 0, 0, '', (), value.services, value.store)
    _, total = registry.list(workspace_id, limit=1)
    formal = value.services.audit_service.projects(workspace_id, 50, None, None)
    return {'profile': value.tasks.profile(workspace_id), 'tasks': value.tasks.list(workspace_id), 'asset_count': total,
            'findings': await invoke(value.services.audit_service.drafts.list, ctx),
            'artifacts': await invoke(value.services.artifact_center.list, workspace_id, 0, 50),
            'formal_projects': {'items': formal['items'], 'total': formal['total']}}


@router.get('/agent/sessions/{session_id}/findings')
async def findings(session_id: str, request: Request):
    value = runtime(request)
    session = session_scope(value, request, session_id)
    return await invoke(value.services.audit_service.drafts.list, product_context(value, session), session_id)


@router.get('/agent/sessions/{session_id}/findings/{draft_id}/revisions/{revision}')
async def fixed_finding(session_id: str, draft_id: str, revision: int, request: Request,
                        sha256: str = Query(..., min_length=64, max_length=64, pattern=r'^[a-f0-9]{64}$')):
    value = runtime(request)
    session = session_scope(value, request, session_id)
    try:
        reference = AgentRef(kind='result', workspace_id=session['workspace_id'], object_id=draft_id,
            revision=revision, sha256=sha256, parent_id=session_id, application='platform', coverage={'kind': 'audit_draft'})
        from workbench_platform.application_services.references import verify
        return await invoke(verify, reference.model_dump(), product_context(value, session))
    except ValueError:
        raise HTTPException(409, '固定草稿版本、内容哈希或当前资料范围不可核实') from None


@router.post('/agent/sessions/{session_id}/evidence')
async def bind_direct_evidence(session_id: str, request: Request):
    value = runtime(request, True)
    session = session_scope(value, request, session_id, True)
    payload = await parsed(request, DirectEvidenceRequest)
    try:
        return await invoke(value.services.audit_service.drafts.bind, payload, product_context(value, session))
    except ValueError:
        raise HTTPException(400, '证据范围、版本或分页不可核实') from None


@router.get('/agent/sessions/{session_id}/evidence/{evidence_id}')
async def direct_evidence(session_id: str, evidence_id: str, request: Request):
    value = runtime(request)
    session = session_scope(value, request, session_id)
    try:
        return await invoke(value.services.audit_service.drafts.evidence_status, evidence_id, product_context(value, session))
    except ValueError:
        raise HTTPException(409, '固定证据仍保留，当前结果版本或范围已变化') from None


@router.put('/agent/sessions/{session_id}/findings')
async def edit_finding(session_id: str, request: Request):
    value = runtime(request, True)
    session = session_scope(value, request, session_id, True)
    payload = await parsed(request, AuditDraftWrite)
    try:
        return await invoke(value.services.audit_service.drafts.write, payload, product_context(value, session),
                            {'kind': 'human', 'label': '本机人工编辑', 'formal_signature': False})
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(400, '发现事实需要关联本项目实际证据') from None


@router.post('/agent/sessions/{session_id}/workpaper')
async def export_draft(session_id: str, request: Request):
    value = runtime(request, True)
    session = session_scope(value, request, session_id, True)
    payload = await parsed(request, AuditDraftExport)
    try:
        return await invoke(value.services.audit_service.drafts.export, payload, product_context(value, session),
                            {'kind': 'human', 'label': '本机生成草稿', 'formal_signature': False})
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(400, '草稿或证据版本不可用，请刷新后再生成') from None
