"""Batch-level OCR tools: one reference and one native observer for 1000 images."""
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.application_services.native import availability, scoped_set
from workbench_platform.jobs.screen_batches import (ScreenSubmit, ScreenPolicy, ScreenAction, ScreenExport,
    ScreenCompare, ScreenResolution, ScreenIssueTarget)


class Run(AgentModel):
    link_id: Identity
    asset_set_id: Identity
    asset_set_revision: int = Field(ge=1)
    policy: ScreenPolicy = Field(default_factory=ScreenPolicy)


class Batch(AgentModel):
    batch_id: Identity


class Page(Batch):
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=10,ge=1,le=30)
    status: str | None = Field(default=None,pattern=r'^(completed|needs_review|failed|cancelled|pending)$')


class Inspect(Batch):
    input_version_id: Identity
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)


class Compare(Inspect):
    result_id: Identity
    expected_revision: int = Field(ge=0, strict=True)
    candidate_id: Identity


class Resolve(Batch):
    budget_seconds: int = Field(ge=10, le=864000, strict=True)
    targets: list[ScreenIssueTarget] = Field(min_length=1, max_length=50)


class Action(Batch):
    action: str = Field(pattern=r'^(pause|resume|cancel|retry_failed|upgrade)$')
    version_ids: list[Identity] | None = Field(default=None,min_length=1,max_length=50)


class Export(Batch):
    include_pending_review: bool = False
    numeric_columns: dict[str,dict[str,list[int]]] | None = None


def owned(ctx,batch_id):
    from .jobs import owned as job_owned
    _,ref=job_owned(ctx,batch_id)
    if ref['application']!='ocr' or ref.get('coverage',{}).get('kind')!='screen_batch':
        raise ValueError('不是当前会话的拍屏表格批次')
    row=ctx.services.screen_batches._row(ctx.workspace_id,batch_id)
    fixed=ctx.services.screen_batches.fixed(ctx.workspace_id,row)
    scoped_set(ctx,fixed.asset_set_id,fixed.revision)
    return ref


def compact(value):
    # No cell text or hundreds of filenames in ordinary status/settlement.
    fields=('batch_id','workspace_id','link_id','handoff_id','asset_set_id','asset_set_revision','status','stage','counts','policy','resolved_policy_sha256','capabilities','progress_done','progress_total','eta','delivery_snapshot_id','delivery_fresh','stale_item_count')
    output={key:value[key] for key in fields if key in value}
    output['exceptions']=[bounded_item(item) for item in value.get('items',[]) if item.get('status') in {'needs_review','failed'}][:5]
    counts=value.get('counts',{})
    output['exception_count']=counts.get('needs_review',0)+counts.get('failed',0)
    output['exceptions_preview_only']=output['exception_count']>len(output['exceptions'])
    return output


def bounded_item(item):
    output={key:item[key] for key in ('input_version_id','stage','status','result_id','result_revision') if key in item}
    if 'name' in item:output['name']=str(item['name'])[:160]
    output['issues']=[{'code':str(issue.get('code',''))[:64],
        'message':str(issue.get('message') or issue.get('reason') or '')[:120]}
        for issue in item.get('issues',[])[:3] if isinstance(issue,dict)]
    return output


def run(args,ctx,allow_new=True):
    scoped_set(ctx,args.asset_set_id,args.asset_set_revision)
    frozen = ctx.services.agent.preferences.freeze(ctx.workspace_id, ctx.session_id, ctx.run_id, 'screen_excel')
    values = args.model_dump(exclude_unset=True,exclude_none=True)
    policy = values.setdefault('policy', {})
    if 'require_human_review' not in policy and 'require_human_review' in frozen['values']:
        policy['require_human_review'] = frozen['values']['require_human_review']
    value=ctx.services.screen_batches.submit(ctx.workspace_id,ScreenSubmit(request_id=ctx.operation_id,
        **values),allow_new=allow_new)
    if value is None:
        return None
    return AgentToolResult(status='pending',summary='已启动整批图片转 Excel，后台按固定策略处理，结果仍需核对',
        data=compact(value),job_refs=[AgentRef(kind='job',workspace_id=ctx.workspace_id,object_id=value['batch_id'],
            native_id=value['batch_id'],revision=1,application='ocr',parent_id=value['handoff_id'],coverage={'kind':'screen_batch','asset_set_id':value['asset_set_id'],'asset_set_revision':value['asset_set_revision']})]).model_dump()


def lookup(args,ctx):
    return run(args,ctx,False)


def read(args,ctx):
    owned(ctx,args.batch_id)
    value=ctx.services.screen_batches.status(ctx.workspace_id,args.batch_id,offset=args.offset,limit=args.limit,status=args.status)
    output=compact(value)
    output['items']=[bounded_item(item) for item in value.get('items',[])]
    total=value.get('filtered_total',value.get('total',value['counts']['total']))
    return AgentToolResult(status='success',summary='已读取批次覆盖与集中异常，完整结果保存在服务中',data=output,
        next_offset=args.offset+args.limit if args.offset+args.limit<total else None).model_dump()


def preview(value, depth=0):
    """Bound evidence at the model boundary; originals stay in the service."""
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, dict):
        if depth >= 5:
            return {'preview_only': True}
        return {key: preview(item, depth+1) for key, item in list(value.items())[:24]}
    if isinstance(value, list):
        return [preview(item, depth+1) for item in value[:12]] if depth < 5 else []
    return value


def diagnostic_page(diagnostics, offset, limit):
    items = diagnostics.get('items', [])
    return {key: diagnostics[key] for key in ('version', 'summary', 'checks', 'budget', 'binding',
            'human_review', 'values_modified', 'reference_input_used') if key in diagnostics} | {
        'items': [preview(item) for item in items[offset:offset+limit]],
        'total': len(items), 'offset': offset, 'limit': limit, 'evidence_preview_only': True,
        'full_evidence_in_service': True}


def inspect_item(args, ctx):
    owned(ctx, args.batch_id)
    value = ctx.services.screen_batches.detail(ctx.workspace_id, args.batch_id, args.input_version_id)
    diagnostics = value.get('diagnostics', {})
    page = diagnostic_page(diagnostics, args.offset, args.limit)
    result = value.get('result') or {}
    return AgentToolResult(status='success', summary='已读取具体疑点与原图证据；截断原文须到复核入口查看',
        data={'batch_id': args.batch_id, 'input_version_id': args.input_version_id, 'result_id': result.get('id'),
            'result_revision': result.get('revision'), 'original_url': value.get('original_url'),
            'human_confirmed': value.get('human_confirmed', False),
            'candidates': [{'id': item['id'], 'engine': item.get('engine')} for item in value.get('candidate_results', [])[:4]],
            'diagnostics': page}, next_offset=args.offset+args.limit if args.offset+args.limit < page['total'] else None).model_dump()


def compare(args, ctx):
    owned(ctx, args.batch_id)
    value = ctx.services.screen_batches.compare(ctx.workspace_id, args.batch_id, args.input_version_id,
        ScreenCompare(result_id=args.result_id, expected_revision=args.expected_revision, candidate_id=args.candidate_id))
    page = diagnostic_page(value.get('diagnostics', {}), args.offset, args.limit)
    return AgentToolResult(status='success', summary='已比较实际候选；未改值、未采用、未确认',
        data={key: value[key] for key in ('current_id', 'alternative_id', 'recommended_candidate_id',
            'selected_candidate_id', 'selection_applied', 'values_modified', 'human_review',
            'input_version_id', 'result_id', 'result_revision') if key in value} | {'diagnostics': page},
        next_offset=args.offset+args.limit if args.offset+args.limit < page['total'] else None).model_dump()


def resolve(args, ctx, allow_new=True):
    owned(ctx, args.batch_id)
    value = ctx.services.screen_batches.resolve(ctx.workspace_id, args.batch_id,
        ScreenResolution(request_id=ctx.operation_id, budget_seconds=args.budget_seconds, targets=args.targets),
        allow_new=allow_new)
    if value is None:
        return None
    attempt = value.get('attempt')
    fields = ('request_id', 'action', 'parent_batch_id', 'root_batch_id', 'batch_id', 'status', 'reason',
              'needs_review', 'values_modified', 'selection_applied', 'human_review', 'budget',
              'capability', 'existing_attempt', 'existing_candidates', 'unprocessed_version_ids', 'job_registered')
    data = {key: value[key] for key in fields if key in value}
    if value.get('job_registered') and attempt:
        data['attempt'] = compact(attempt)
        return AgentToolResult(status='pending', summary='已启动一次有预算的精度候选；原结果保留，完成后比较并复核',
            data=data, job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, object_id=attempt['batch_id'],
                native_id=attempt['batch_id'], revision=1, application='ocr', parent_id=attempt['handoff_id'],
                coverage={'kind': 'screen_batch', 'asset_set_id': attempt['asset_set_id'],
                          'asset_set_revision': attempt['asset_set_revision']})]).model_dump()
    if value.get('status') == 'intent':
        return AgentToolResult(status='needs_verification', summary='已有处理意图，但尚未查到提交回执；沿原身份核实',
            data=data).model_dump()
    return AgentToolResult(status='success' if value.get('status') == 'reused' else 'unsupported',
        summary='已查到现有尝试，未重复运行' if value.get('status') == 'reused' else '本次未新增识别，请按实际原因比较现有候选或保留待核',
        data=data, warnings=['未自动采用候选或完成人工确认']).model_dump()


def resolve_lookup(args, ctx):
    return resolve(args, ctx, False)


def action(args,ctx,allow_new=True):
    owned(ctx,args.batch_id)
    value=ctx.services.screen_batches.action(ctx.workspace_id,args.batch_id,ScreenAction(request_id=ctx.operation_id,
        action=args.action,version_ids=args.version_ids), allow_new=allow_new)
    if value is None:
        return None
    if args.action=='upgrade':
        if value.get('new_attempt_started') is False:
            resolution = value.get('resolution', {})
            return AgentToolResult(status='needs_verification' if resolution.get('status')=='intent' else 'unsupported',
                summary='未新增精度任务，请比较已有候选或按实际原因保留待核',
                data={**compact(value), 'resolution': preview(resolution)}).model_dump()
        return AgentToolResult(status='pending',summary='已为所选图片启动提高精度的新尝试，原结果和来源保留',data=compact(value),
            job_refs=[AgentRef(kind='job',workspace_id=ctx.workspace_id,object_id=value['batch_id'],native_id=value['batch_id'],revision=1,
                application='ocr',parent_id=value['handoff_id'],coverage={'kind':'screen_batch','asset_set_id':value['asset_set_id'],'asset_set_revision':value['asset_set_revision']})]).model_dump()
    return AgentToolResult(status='success',summary='已按原批次身份提交控制，真实状态以服务回执为准',data=compact(value)).model_dump()


def action_lookup(args,ctx):
    return action(args,ctx,False)


def export(args,ctx):
    owned(ctx,args.batch_id)
    frozen = ctx.services.agent.preferences.freeze(ctx.workspace_id, ctx.session_id, ctx.run_id, 'screen_excel')
    include_review = args.include_pending_review if 'include_pending_review' in args.model_fields_set else frozen['values'].get('include_pending_review', False)
    bundle=ctx.services.screen_batches.export(ctx.workspace_id,args.batch_id,ScreenExport(
        include_pending_review=include_review,numeric_columns=args.numeric_columns))
    return AgentToolResult(status='success',summary='已生成每图一个 Excel 的 ZIP，失败及待复核项列入清单',
        data={'batch_id':args.batch_id,'bundle':{key:bundle[key] for key in ('bundle_id','title','files')}},
        artifact_refs=[AgentRef(kind='artifact',workspace_id=ctx.workspace_id,object_id=bundle['bundle_id'],revision=1,application='ocr',parent_id=args.batch_id)]).model_dump()


def status_reference(ref,ctx):
    owned(ctx,ref['object_id'])
    return compact(ctx.services.screen_batches.status(ctx.workspace_id,ref['object_id'],limit=10,status='needs_review'))


def cancel_reference(ref,ctx):
    owned(ctx,ref['object_id'])
    from workbench_platform.agent.store import digest
    return ctx.services.screen_batches.action(ctx.workspace_id,ref['object_id'],ScreenAction(
        action='cancel',request_id='r'+digest([ctx.session_id,ctx.generation,ref['object_id'],'cancel'])[:32]))


def settle(ref,ctx):
    value=status_reference(ref,ctx)
    if value['counts']['pending'] or value['status'] in {'paused','unknown','queued','running'}:
        return None
    count=value['counts']
    success=count['completed']+count['needs_review']
    return AgentToolResult(status='success' if success else 'error',summary=
        f'整批处理已结束：完成 {count["completed"]}，待复核 {count["needs_review"]}，失败 {count["failed"]}，取消 {count["cancelled"]}',
        data=value,coverage={key:number for key,number in count.items()},
        warnings=['待复核结果未经人工确认'] if count['needs_review'] else [],
        error={'code':'batch_failed','message':'请读取异常或仅重试失败图片'} if not success else None).model_dump()


def register(registry):
    from .screen_local_candidates import register as register_local_candidates
    register_local_candidates(registry)
    for name,schema,handler,description,effect in [
        ('ocr_screen_batch_run',Run,run,'一次处理固定图片集（最多1000张）：自动表区、有限质量升级、最终结果。返回一个批次引用，等待由后台完成。每图一个XLSX。policy仅填用户明确覆盖的选项；省略时使用原生已冻结默认，不从API兼容值推测策略。','derive'),
        ('ocr_screen_batch_status',Page,read,'分页查看整批进度和异常摘要。按needs_review或failed集中查看，避免逐图调用。','read'),
        ('ocr_screen_batch_inspect',Inspect,inspect_item,'分页读取明确图片的具体问题ID、原图定位与证据、实际结果revision及已有候选；不传整图全部格值。无可靠格位时标到图或表。','read'),
        ('ocr_screen_batch_compare',Compare,compare,'比较本图已有真实候选，绑定当前result_id与revision；只报告不同与建议，不改值、不采用、不代签人审。','read'),
        ('ocr_screen_batch_resolve',Resolve,resolve,'对明确问题启动一次已有GLM精度候选。须先inspect，绑定图片/结果revision/真实问题IDs和总预算；继承来源链实际成本与在途预留，重复像素策略不重跑，保留人审。','derive'),
        ('ocr_screen_batch_control',Action,action,'暂停、继续、取消整批或只重试失败项。异常精度优先inspect后resolve；兼容upgrade也遵守同一来源链预算和防重复规则，保留原结果。','derive'),
        ('ocr_screen_batch_export',Export,export,'导出整批最终选定结果：每图一个XLSX，同图多表分工作表，ZIP及对应清单；可显式包含待复核项。默认原值文本；numeric_columns仅用于已人工accepted的图片，按图片版本→表序号→已确认列序号指定，不得由模型自动代签确认。','derive')]:
        registry.register(ToolSpec(name,description,schema,handler,effect=effect,idempotent=True,
            availability=lambda services: availability(services,'ocr'),
            lookup_operation=lookup if name=='ocr_screen_batch_run' else resolve_lookup if name=='ocr_screen_batch_resolve' else action_lookup if name=='ocr_screen_batch_control' else None,
            input_kinds=('asset_set','job'),output_kinds=('job','artifact'),resource_keys=lambda args,ctx:('ocr:'+getattr(args,'link_id',ctx.workspace_id),)))
