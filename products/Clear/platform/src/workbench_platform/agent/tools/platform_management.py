"""Use the existing batch, formal audit and todo business services."""
from typing import Literal
from pydantic import Field, create_model
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity, AuditProjectProfile
from packages.contracts import audit as audit_contract
from packages.contracts import automation as batch_contract
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import Conflict
from workbench_platform.application_services.references import asset_allowed, data_scope
from workbench_platform.data_api.service import DataError


def schema(name, original, excluded=(), extra=None):
    fields = {key: (field.annotation, field) for key, field in original.model_fields.items()
              if key not in {'schema_version', 'audit_version', 'workspace_id', 'request_id', 'request_key', 'actor', *excluded}}
    return create_model(name, __base__=AgentModel, **(fields | (extra or {})))


class Empty(AgentModel):
    pass


class Page(AgentModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class Tags(AgentModel):
    asset_id: Identity
    tags: list[str] = Field(max_length=16)


class TaskUpdate(AgentModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    goal: str | None = Field(default=None, max_length=16000)


class BatchRead(Page):
    batch_id: Identity


class BatchControl(AgentModel):
    batch_id: Identity
    operation: Literal['cancel', 'retry']


class FromSession(AgentModel):
    session_id: Identity
    name: str = Field(min_length=1, max_length=100)


class AuditPage(AgentModel):
    project_id: Identity | None = None
    kind: Literal['project', 'procedure', 'finding'] = 'project'
    limit: int = Field(default=50, ge=1, le=50)
    cursor: str | None = None
    snapshot_token: str | None = None


class AuditRead(AgentModel):
    project_id: Identity
    kind: Literal['project', 'procedure', 'finding', 'evidence'] = 'project'
    entity_id: Identity | None = None


ProfileWrite = schema('ProfileWrite', AuditProjectProfile)
Template = schema('AgentBatchTemplate', batch_contract.BatchTemplateCreate, {'template_id'})
Submit = schema('AgentBatchSubmit', batch_contract.BatchRequest)
Disposition = schema('AgentBatchDisposition', batch_contract.BatchDisposition, extra={'batch_id': (Identity, ...)})
ProjectCreate = schema('AgentFormalProjectCreate', audit_contract.AuditProjectCreate)
ProjectWrite = schema('AgentFormalProjectWrite', audit_contract.AuditProjectWrite, extra={'project_id': (Identity, ...)})
ProcedureCreate = schema('AgentFormalProcedureCreate', audit_contract.AuditProcedureCreate, extra={'project_id': (Identity, ...)})
ProcedureWrite = schema('AgentFormalProcedureWrite', audit_contract.AuditProcedureWrite, extra={'project_id': (Identity, ...), 'entity_id': (Identity, ...)})
FindingCreate = schema('AgentFormalFindingCreate', audit_contract.AuditFindingCreate, extra={'project_id': (Identity, ...)})
FindingWrite = schema('AgentFormalFindingWrite', audit_contract.AuditFindingWrite, extra={'project_id': (Identity, ...), 'entity_id': (Identity, ...)})
TodoQuery = schema('AgentTodoQuery', audit_contract.TodoQuery)
BatchExport = schema('AgentBatchExport', batch_contract.ReviewArtifactRequest, extra={'batch_id': (Identity, ...)})


class History(AuditRead):
    kind: Literal['project', 'procedure', 'finding'] = 'project'
    limit: int = Field(default=50, ge=1, le=50)
    cursor: str | None = None
    snapshot_token: str | None = None


class TodoLocate(TodoQuery):
    todo_id: Identity


def actor(ctx):
    return {'id': 'agent_'+ctx.session_id[:50], 'display_name': 'AI审计Agent', 'source': 'local'}


def result(value, summary):
    return AgentToolResult(status='success', summary=summary, data=value).model_dump()


def inputs(ctx, values):
    for item in values:
        if not asset_allowed(ctx, item['asset_id']):
            raise ValueError('批次资料超出任务范围')
        asset = ctx.services.asset_library[0].get(ctx.workspace_id, item['asset_id'])
        if (asset.revision, asset.blob_hash) != (item['asset_revision'], item['blob_hash']):
            raise ValueError('批次资料版本变化')
        with ctx.services.asset_library[0].stage_verified(ctx.workspace_id, asset.asset_id, revision=asset.revision, blob_hash=asset.blob_hash):
            pass


def batch(ctx, batch_id):
    value = ctx.services.batch_service.get(batch_id)
    if value['workspace_id'] != ctx.workspace_id:
        raise ValueError('批次不属于当前项目')
    inputs(ctx, value['inputs'])
    return value


def project(ctx, project_id):
    value = ctx.services.audit_service.get(project_id)
    if value['workspace_id'] != ctx.workspace_id:
        raise ValueError('正式项目不属于当前工作空间')
    # Project detail itself contains no result rows; individual linked batches
    # are checked when procedures/findings are read or modified.
    return value


def tags(args, ctx):
    if not asset_allowed(ctx, args.asset_id):
        raise ValueError('资料不属于当前范围')
    value = ctx.services.asset_library[0].set_tags(ctx.workspace_id, args.asset_id, args.tags)
    return result({'asset_id': args.asset_id, 'tags': value}, '已更新平台资料标签')


def task(args, ctx):
    value = ctx.services.agent.tasks.update(ctx.session_id, {'request_id': ctx.operation_id, **args.model_dump()})
    return result(value, '已按版本更新当前任务名称与目标')


def profile(args, ctx):
    return result(ctx.services.agent.tasks.save_profile(ctx.workspace_id, {'request_id': ctx.operation_id, **args.model_dump()}), '已按版本保存项目背景与审计口径')


def list_templates(args, ctx):
    values = ctx.services.batch_service.store.templates(ctx.workspace_id)
    return AgentToolResult(status='success', summary='已读取现有批次模板', data={'items': values[args.offset:args.offset+args.limit], 'total': len(values)},
        next_offset=args.offset+args.limit if args.offset+args.limit < len(values) else None).model_dump()


def create_template(args, ctx):
    service = ctx.services.batch_service
    template_id = 't'+ctx.operation_id[1:]
    try:
        prior = service.store.template(ctx.workspace_id, template_id, 1)
    except DataError as error:
        if error.status != 404:
            raise
    else:
        return result(prior, '已读取原模板创建回执')
    if args.scenario != 'data_compare':
        ctx.services.job_service._context(ctx.workspace_id, args.scenario, args.config.get('link_id'))
    return result(service.create_template({'schema_version': '1.0', 'workspace_id': ctx.workspace_id,
        'template_id': template_id, **args.model_dump()}), '已保存可供人工或Agent使用的批次模板')


def from_session(args, ctx):
    data_scope(ctx, ctx.services.data_workbench.get(args.session_id))
    config = ctx.services.batch_service.data.from_session(args.session_id)
    return create_template(Template(name=args.name, scenario='data_compare', config=config), ctx)


def command(args, ctx):
    value = {'schema_version': '1.0', 'workspace_id': ctx.workspace_id, 'request_key': ctx.operation_id, **args.model_dump()}
    inputs(ctx, value['inputs'])
    return value


def preflight(args, ctx):
    return result(ctx.services.batch_service.preflight(command(args, ctx)), '已调用现有批次预检查')


def submit(args, ctx):
    value = ctx.services.batch_service.submit(command(args, ctx))
    return AgentToolResult(status='pending', summary='已提交现有批次执行，等待实际结果', data={'batch_id': value['batch_id']},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, application='platform', object_id=value['batch_id'], revision=value['revision'], coverage={'kind': 'batch'})]).model_dump()


def submit_lookup(args, ctx):
    values = ctx.services.batch_service.store.list(ctx.workspace_id, request_key=ctx.operation_id)
    if not values:
        return None
    value = batch(ctx, values[0]['batch_id'])
    return AgentToolResult(status='pending', summary='已核实原批次提交身份，继续等待实际结果', data={'batch_id': value['batch_id']},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, application='platform', object_id=value['batch_id'], revision=value['revision'], coverage={'kind': 'batch'})]).model_dump()


def read_batch(args, ctx):
    value = batch(ctx, args.batch_id)
    page = ctx.services.batch_service.issue_page(args.batch_id, args.offset, args.limit)
    bundles = []
    for artifact in value.get('artifacts', []):
        if artifact.get('bundle_id'):
            bundles.append(AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=artifact['bundle_id'], revision=1))
    return AgentToolResult(status='success', summary='已读取真实批次状态、事项与成果', data={'batch': value, 'issues': page}, artifact_refs=bundles,
                           next_offset=page.get('next_offset')).model_dump()


def list_batches(args, ctx):
    values = ctx.services.batch_service.store.list(ctx.workspace_id)
    accessible = []
    for value in values:
        try:
            inputs(ctx, value['inputs'])
        except ValueError:
            continue
        accessible.append({key: value[key] for key in ('batch_id', 'revision', 'status', 'template_id', 'template_version', 'created_at')})
    return AgentToolResult(status='success', summary='已读取当前资料范围可访问的既有批次目录', data={'items': accessible[args.offset:args.offset+args.limit], 'total': len(accessible)},
        next_offset=args.offset+args.limit if args.offset+args.limit < len(accessible) else None).model_dump()


def batch_history(args, ctx):
    batch(ctx, args.batch_id)
    return result(ctx.services.batch_service.disposition_history(args.batch_id, args.offset, args.limit), '已读取真实批次处置历史')


def export_batch(args, ctx):
    batch(ctx, args.batch_id)
    value = ctx.services.batch_service.review_export(args.batch_id, {'request_id': ctx.operation_id, 'actor': actor(ctx), **args.model_dump(exclude={'batch_id'})})
    bundle_id = value['delivery'].get('bundle_id')
    if not bundle_id:
        raise ValueError('原生批次成果回执缺少身份')
    return AgentToolResult(status='success', summary='已调用现有批次成果导出，保留真实事项状态与AI操作人', data=value,
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle_id, revision=1)]).model_dump()


def export_lookup(args, ctx):
    from contextlib import closing
    with closing(ctx.services.batch_service.store.db()) as db:
        row = db.execute('SELECT batch_id FROM batch_review_requests WHERE request_id=?', (ctx.operation_id,)).fetchone()
    if not row:
        return None
    if row['batch_id'] != args.batch_id:
        raise Conflict('批次导出操作身份变化')
    return export_batch(args, ctx)


def settle(ref, ctx):
    value = batch(ctx, ref['object_id'])
    if value['status'] in {'queued', 'running', 'waiting_resource', 'cancelling'}:
        return None
    result = read_batch(BatchRead(batch_id=ref['object_id']), ctx)
    if value['status'] in {'failed', 'cancelled', 'waiting_input', 'needs_verification'}:
        result.update(status='needs_verification' if value['status'] == 'needs_verification' else 'error',
            error={'code': value['status'], 'message': '批次未完整完成，请读取实际事项后处理'})
    return result


def control(args, ctx):
    batch(ctx, args.batch_id)
    return result(ctx.services.batch_service.control(args.batch_id, {'operation': args.operation}), '已按批次当前能力提交控制动作，后续读取实际状态')


def disposition(args, ctx):
    batch(ctx, args.batch_id)
    try:
        value = ctx.services.batch_service.dispositions(args.batch_id, {'request_id': ctx.operation_id, 'actor': actor(ctx), **args.model_dump(exclude={'batch_id'})})
    except DataError as error:
        if error.status == 409:
            raise Conflict(str(error)) from error
        raise
    return result(value, '已按所见批次版本保存事项处置，操作人明确为AI审计Agent')


def disposition_lookup(args, ctx):
    batch(ctx, args.batch_id)
    payload = batch_contract.BatchDisposition.model_validate({'request_id': ctx.operation_id, 'actor': actor(ctx), **args.model_dump(exclude={'batch_id'})}).model_dump(mode='json')
    operation = ctx.services.batch_service.store.request_operation(args.batch_id, payload)
    if operation is None or operation['state'] != 'committed':
        return None
    return result(operation['receipt']['batch'], '已核实原批次事项处置回执，未重复修改')


def list_audit(args, ctx):
    service = ctx.services.audit_service
    if args.kind == 'project':
        value = service.projects(ctx.workspace_id, args.limit, args.cursor, args.snapshot_token)
    else:
        if not args.project_id:
            raise ValueError('程序和发现目录需要项目ID')
        project(ctx, args.project_id)
        value = service.entities(args.project_id, args.kind, args.limit, args.cursor, args.snapshot_token)
        for item in value['items']:
            check_entity(ctx, item, args.kind)
    return result(value, '已分页读取正式项目、程序或发现目录')


def check_entity(ctx, value, kind):
    for ref in value.get('batches', []):
        batch(ctx, ref['batch_id'])
    for ref in value.get('evidence_refs', []):
        if ref.get('batch_id'):
            batch(ctx, ref['batch_id'])
        elif ref.get('inspection_id'):
            inspection = ctx.services.forensic_service.get(ctx.workspace_id, ref['inspection_id'])
            if not asset_allowed(ctx, inspection['asset_id']):
                raise ValueError('发现证据不属于当前资料范围')
        elif ref.get('scenario') == 'direct_tool':
            ctx.services.audit_service.evidence(value['project_id'], ref['evidence_id'], context=ctx)


def read_audit(args, ctx):
    project(ctx, args.project_id)
    if args.kind == 'evidence':
        if args.entity_id is None:
            raise ValueError('读取正式证据须指定所见证据ID')
        value = ctx.services.audit_service.evidence(args.project_id, args.entity_id, context=ctx)
        if value.get('batch_id'):
            batch(ctx, value['batch_id'])
        elif value.get('scenario') == 'image_forensics':
            if any(not asset_allowed(ctx, source['asset_id']) for source in value['sources']):
                raise ValueError('正式取证证据不属于当前资料范围')
        from .audit import evidence_data
        return result(evidence_data(value), '已读取正式档案固定证据、来源与过期情况')
    value = ctx.services.audit_service.get(args.project_id, args.kind, args.entity_id)
    check_entity(ctx, value, args.kind)
    return result(value, '已读取正式审计对象的所见版本')


def create_project(args, ctx):
    return result(ctx.services.audit_service.create_project(ctx.workspace_id, {'schema_version': '1.0', 'audit_version': '1.0',
        'request_id': ctx.operation_id, 'actor': actor(ctx), **args.model_dump()}), '已创建正式审计项目草稿，未生成编制复核署名')


def write_audit(args, ctx, kind):
    project(ctx, args.project_id)
    if getattr(args, 'entity_id', None):
        check_entity(ctx, ctx.services.audit_service.get(args.project_id, kind, args.entity_id), kind)
    body = args.model_dump(exclude={'project_id', 'entity_id'})
    for ref in body.get('batches', []):
        batch(ctx, ref['batch_id'])
    for ref in body.get('evidence', []):
        if ref.get('batch_id'):
            batch(ctx, ref['batch_id'])
        elif ref.get('inspection_id'):
            inspection = ctx.services.forensic_service.get(ctx.workspace_id, ref['inspection_id'])
            if not asset_allowed(ctx, inspection['asset_id']):
                raise ValueError('发现证据不属于范围')
    try:
        value = ctx.services.audit_service.write(args.project_id, kind, {'schema_version': '1.0', 'audit_version': '1.0',
            'request_id': ctx.operation_id, 'actor': actor(ctx), **body}, getattr(args, 'entity_id', None), context=ctx)
    except DataError as error:
        if error.status == 409:
            raise Conflict(str(error)) from error
        raise
    return result(value, '已按版本保存正式审计草稿/程序/发现，记录AI操作人，人工署名独立')


def todos(args, ctx):
    from workbench_platform.audit.todos import WorkspaceTodoService
    value = WorkspaceTodoService(ctx.services.audit_service).list(ctx.workspace_id, audit_contract.TodoQuery.model_validate(args.model_dump()))
    # Do not expose findings/issue content from an out-of-scope source.
    for item in value['items']:
        context = item.get('context', {})
        if context.get('batch_id'):
            batch(ctx, context['batch_id'])
        if context.get('project_id'):
            project(ctx, context['project_id'])
            if context.get('finding_id'):
                check_entity(ctx, ctx.services.audit_service.get(context['project_id'], 'finding', context['finding_id']), 'finding')
    return result(value, '已读取现有待办目录；事项处置用batch_dispose，发现修订用audit_formal_update_finding')


def locate_todo(args, ctx):
    from workbench_platform.audit.todos import WorkspaceTodoService
    value = WorkspaceTodoService(ctx.services.audit_service).locate(ctx.workspace_id, args.todo_id,
        audit_contract.TodoQuery.model_validate(args.model_dump(exclude={'todo_id'})))
    context = value['item']['context']
    if context.get('batch_id'):
        batch(ctx, context['batch_id'])
    if context.get('finding_id'):
        project(ctx, context['project_id'])
        check_entity(ctx, ctx.services.audit_service.get(context['project_id'], 'finding', context['finding_id']), 'finding')
    return result(value, '已按真实待办身份定位事项及页面，可继续处理')


def audit_history(args, ctx):
    project(ctx, args.project_id)
    if args.kind != 'project':
        check_entity(ctx, ctx.services.audit_service.get(args.project_id, args.kind, args.entity_id), args.kind)
    # Each history row can contain evidence. Check its saved sources too.
    value = ctx.services.audit_service.history(args.project_id, args.limit, args.cursor, args.snapshot_token,
        args.kind, args.entity_id or args.project_id)
    for item in value['items']:
        for snapshot in (item.get('previous'), item.get('current')):
            if isinstance(snapshot, dict):
                check_entity(ctx, snapshot, args.kind)
    return result(value, '已读取正式审计对象操作历史和保存版本')


def register(registry):
    definitions = [
        ('assets_set_tags', Tags, tags, '更新当前范围资料标签，不删除原件。', 'versioned'),
        ('audit_update_task', TaskUpdate, task, '按所见版本更新当前任务名称与目标，不自行扩大资料范围。', 'versioned'),
        ('audit_update_project', ProfileWrite, profile, '按所见版本更新项目背景、期间与检查规则。', 'versioned'),
        ('batch_list_templates', Page, list_templates, '分页读取项目现有批次模板。', 'read'),
        ('batch_list', Page, list_batches, '分页列出当前资料范围内的既有批次，可接续手动或Agent执行。', 'read'),
        ('batch_create_template', Template, create_template, '保存现有业务批次模板，可继续手动使用；原生场景需当前关联link_id。', 'derive'),
        ('batch_template_from_session', FromSession, from_session, '复用真实数据会话中的配置保存批次模板，不手写替代脚本。', 'derive'),
        ('batch_preflight', Submit, preflight, '调用现有批次输入和配置预检查。', 'read'),
        ('batch_run', Submit, submit, '按模板与固定输入执行原生批次并等待真实结果。', 'derive'),
        ('batch_read', BatchRead, read_batch, '分页读取当前范围批次的状态、事项与成果。', 'read'),
        ('batch_control', BatchControl, control, '请求取消或继续现有批次，需读取实际状态确认；不自动重复控制。', 'control'),
        ('batch_dispose', Disposition, disposition, '按版本处置事项；明确AI操作人，采用需显式原生版本。', 'versioned'),
        ('batch_read_history', BatchRead, batch_history, '分页读取真实批次事项处置历史。', 'read'),
        ('batch_export', BatchExport, export_batch, '调用现有批次成果导出；未完成事项时采用intermediate，final沿用原生约束。', 'derive'),
        ('audit_formal_list', AuditPage, list_audit, '分页读取正式项目、程序和发现目录。', 'read'),
        ('audit_formal_read', AuditRead, read_audit, '读取正式项目、程序、发现或固定证据；evidence须提供entity_id。', 'read'),
        ('audit_formal_create_project', ProjectCreate, create_project, '创建正式审计项目草稿，保留手动编制复核能力。', 'derive'),
        ('audit_formal_update_project', ProjectWrite, lambda args, ctx: write_audit(args, ctx, 'project'), '按版本更新正式项目草稿。', 'versioned'),
        ('audit_formal_create_procedure', ProcedureCreate, lambda args, ctx: write_audit(args, ctx, 'procedure'), '创建审计程序并关联真实批次。', 'derive'),
        ('audit_formal_update_procedure', ProcedureWrite, lambda args, ctx: write_audit(args, ctx, 'procedure'), '按版本更新审计程序。', 'versioned'),
        ('audit_formal_create_finding', FindingCreate, lambda args, ctx: write_audit(args, ctx, 'finding'), '保存正式发现，或以source_draft固定版本转档；可关联批次、取证或直接证据。', 'derive'),
        ('audit_formal_update_finding', FindingWrite, lambda args, ctx: write_audit(args, ctx, 'finding'), '按版本修改正式发现、解释、结论和状态，可处理发现待办。', 'versioned'),
        ('audit_list_todos', TodoQuery, todos, '按项目/场景/状态分页查询现有待办，再调用实际事项或发现工具处理。', 'read'),
        ('audit_locate_todo', TodoLocate, locate_todo, '按待办身份直接定位实际事项、页面和筛选状态，再使用事项处置或发现修订工具。', 'read'),
        ('audit_formal_read_history', History, audit_history, '分页读取正式项目/程序/发现的真实操作历史及保存版本。', 'read'),
    ]
    for name, model, handler, description, effect in definitions:
        registry.register(ToolSpec(name, description, model, handler, effect=effect, idempotent=name != 'batch_control',
            lookup_operation={'batch_run': submit_lookup, 'batch_export': export_lookup, 'batch_dispose': disposition_lookup}.get(name)))
