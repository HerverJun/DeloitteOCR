"""Bounded cell reread candidates. They never become page results or exports."""
from pydantic import Field

from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import digest
from workbench_platform.application_services.native import availability
from workbench_platform.jobs.screen_local_candidates import (
    ScreenLocalControl, ScreenLocalSubmit, ScreenLocalTarget,
)


class Run(AgentModel):
    batch_id: Identity
    budget_seconds: int = Field(ge=10, le=900, strict=True)
    target: ScreenLocalTarget


class Job(AgentModel):
    job_id: Identity


class Page(Job):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6, ge=1, le=12)


class Control(Job):
    action: str = Field(pattern=r'^(pause|resume|cancel|retry_failed)$')


def compact(value):
    counts = value.get('counts', {})
    pending = sum(counts.get(state, 0) for state in ('queued', 'running', 'paused', 'interrupted'))
    status = value.get('execution_status', 'unknown')
    if status == 'pending':
        status = 'paused' if pending and pending == counts.get('paused', 0) + counts.get('interrupted', 0) else 'running'
    elif status == 'succeeded':
        status = 'needs_review'
    return {key: value[key] for key in ('version', 'request_id', 'batch_id', 'root_batch_id', 'target',
        'reason', 'capability', 'requested_budget_seconds', 'reserved_seconds', 'values_modified',
        'selection_applied', 'human_review', 'automatic_apply', 'control') if key in value} | {
        'status': status, 'submission_status': value.get('status'), 'execution_status': value.get('execution_status'),
        'stage': 'local_candidates', 'counts': {'total': value.get('total', 0), 'completed': 0,
            'needs_review': counts.get('succeeded', 0), 'failed': counts.get('failed', 0),
            'cancelled': counts.get('cancelled', 0), 'pending': pending},
        'progress_total': value.get('total', 0), 'progress_done': value.get('total', 0)-pending,
        'owned_task_count': len(value.get('owned_task_ids', [])),
        'capabilities': ['pause', 'resume', 'cancel', 'retry_failed'] if value.get('owned_task_ids') else []}


def reference(value, parent, ctx):
    return AgentRef(kind='job', workspace_id=ctx.workspace_id,
        object_id='lc'+digest([ctx.workspace_id, value['batch_id'], value['request_id']])[:40],
        native_id=value['request_id'], parent_id=parent['parent_id'], application='ocr', revision=1,
        coverage={'kind': 'screen_local_candidates', 'batch_id': value['batch_id'],
                  'asset_set_id': parent['coverage']['asset_set_id'],
                  'asset_set_revision': parent['coverage']['asset_set_revision']})


def run(args, ctx, allow_new=True):
    from .screen_batches import owned
    parent = owned(ctx, args.batch_id)
    value = ctx.services.screen_batches.local_candidates(ctx.workspace_id, args.batch_id,
        ScreenLocalSubmit(request_id=ctx.operation_id, budget_seconds=args.budget_seconds, target=args.target),
        allow_new=allow_new)
    if value is None:
        return None
    data = compact(value)
    refs = [reference(value, parent, ctx)] if value['task_ids'] else []
    status = ('pending' if data['counts']['pending'] else 'success' if data['counts']['needs_review']
              else 'unsupported' if not refs else 'error')
    return AgentToolResult(status=status,
        summary='已登记局部文字候选，后台完成后查看原值与候选' if status == 'pending'
        else '局部候选已终态，请读取具体候选并核对原图' if status == 'success'
        else '局部候选未新增可用识别，请按实际原因保留待核',
        data=data, job_refs=refs, warnings=['未改原值、未采用候选、未完成人工确认']).model_dump()


def lookup(args, ctx):
    return run(args, ctx, False)


def owned(ctx, job_id):
    from .jobs import owned as job_owned
    from .screen_batches import owned as batch_owned
    _, ref = job_owned(ctx, job_id)
    if ref.get('application') != 'ocr' or ref.get('coverage', {}).get('kind') != 'screen_local_candidates':
        raise ValueError('不是当前会话的局部候选任务')
    parent = batch_owned(ctx, ref['coverage']['batch_id'])
    if (parent['parent_id'] != ref['parent_id']
            or any(parent['coverage'][key] != ref['coverage'].get(key)
                   for key in ('asset_set_id', 'asset_set_revision'))):
        raise ValueError('局部候选的原批次或资料范围已变化')
    return ref


def read(args, ctx):
    ref = owned(ctx, args.job_id)
    value = ctx.services.screen_batches.local_candidates_status(ctx.workspace_id, ref['coverage']['batch_id'],
        ref['native_id'], offset=args.offset, limit=args.limit)
    candidates = []
    for item in value['items']:
        binding = item['binding']
        candidates.append({key: item[key] for key in ('task_id', 'status', 'candidate_state', 'current_binding',
            'text_preview_only', 'content_status', 'values_modified', 'selection_applied', 'human_review') if key in item} | {
            'text': item['text'][:512] if item.get('text') else item.get('text'),
            'phase': str(item.get('phase') or '')[:160],
            'error': str(item.get('error') or '')[:512] or None,
            'binding': {key: binding.get(key) for key in ('input_version_id', 'result_id', 'result_revision', 'source_sha256')},
            'location': binding.get('cell', {}).get('location'),
            'artifacts': [{'file': name, 'sha256': artifact.get('sha256')}
                          for name, artifact in (item.get('artifact') or {}).items()][:10]})
    return AgentToolResult(status='success', summary='已读取局部文字候选与冻结原图格位；历史候选不可覆盖当前结果',
        data={**compact(value), 'items': candidates, 'full_evidence_in_service': True},
        next_offset=value['next_offset']).model_dump()


def control(args, ctx, allow_new=True):
    ref = owned(ctx, args.job_id)
    value = ctx.services.screen_batches.local_candidates_control(ctx.workspace_id, ref['coverage']['batch_id'],
        ref['native_id'], ScreenLocalControl(request_id=ctx.operation_id, action=args.action), allow_new=allow_new)
    if value is None:
        return None
    return AgentToolResult(status='success', summary='已沿原局部候选任务提交控制；未改变整图结果或人工复核'
                           if value.get('owned_task_ids') else '本请求仅复用已有候选；任务须通过原请求控制',
                           data=compact(value)).model_dump()


def control_lookup(args, ctx):
    return control(args, ctx, False)


def status_reference(ref, ctx):
    recorded = owned(ctx, ref['object_id'])
    if any(ref.get(key) != recorded.get(key) for key in ('parent_id', 'native_id', 'coverage')):
        raise ValueError('局部候选引用与原操作身份不一致')
    return compact(ctx.services.screen_batches.local_candidates_status(ctx.workspace_id,
        recorded['coverage']['batch_id'], recorded['native_id'], limit=12))


def cancel_reference(ref, ctx):
    ref = owned(ctx, ref['object_id'])
    return compact(ctx.services.screen_batches.local_candidates_control(ctx.workspace_id,
        ref['coverage']['batch_id'], ref['native_id'], ScreenLocalControl(action='cancel',
            request_id='lcc'+digest([ctx.session_id, ctx.generation, ref['object_id'], 'cancel'])[:32])))


def settle(ref, ctx):
    data = status_reference(ref, ctx)
    if data['counts']['pending']:
        return None
    success = data['counts']['needs_review'] > 0
    return AgentToolResult(status='success' if success else 'error',
        summary='局部文字候选处理结束，读取具体候选后仍须核对原图' if success else '局部候选未生成可用结果，原值保留',
        data=data, coverage=data['counts'], warnings=['候选未经人工确认；未改原值或采用'],
        error=None if success else {'code': 'local_candidate_failed', 'message': '读取原错误与剩余预算后决定下一步'}).model_dump()


def register(registry):
    for name, schema, handler, description, effect, recover in [
        ('ocr_screen_local_candidates', Run, run,
         '仅对inspect已返回的可靠单格疑点生成GLM文字候选；一图、1–12疑点、10–900秒。绑定原图/result/revision，坐标与模型由服务决定。后台等待，沿原预算；不改值、不采用、不确认。', 'derive', lookup),
        ('ocr_screen_local_candidates_read', Page, read,
         '分页读取本会话局部候选任务的原图格位、文字和版本状态；stale为历史建议，不能覆盖当前结果。完整原始证据保存在复核入口。', 'read', None),
        ('ocr_screen_local_candidates_control', Control, control,
         '沿本会话原局部候选job暂停、继续、取消或仅重试失败；原操作身份防重复，继承剩余预算，不重跑整批。', 'derive', control_lookup)]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=lambda services: availability(services, 'ocr'), lookup_operation=recover,
            input_kinds=('job',), output_kinds=('job',),
            resource_keys=lambda args, ctx: ('ocr:'+ctx.workspace_id,)))
