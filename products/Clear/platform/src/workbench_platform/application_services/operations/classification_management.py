"""Scoped annotation, immutable datasets and native model training."""
from typing import Literal
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.application_services.identity import digest, Conflict
from workbench_platform.application_services.native import scoped_handoff, read_job, availability
from workbench_platform.jobs.native_classification import ClassificationConflict


class Bound(AgentModel):
    handoff_id: Identity


class Page(Bound):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class LabelWrite(AgentModel):
    asset_id: Identity
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    label_id: Identity | None
    group_id: str | None = Field(default=None, min_length=1, max_length=200)


class Annotate(Bound):
    items: list[LabelWrite] = Field(min_length=1, max_length=100)


class Publish(Bound):
    name: str = Field(min_length=1, max_length=100)
    seed: int = Field(default=42, ge=0, le=2**31-1)


class Train(Bound):
    dataset_id: Identity
    encoder: Literal['resnet18', 'dinov2', 'mobilenetv4', 'repvit'] = 'resnet18'
    head_method: Literal['adamw', 'logistic_cv'] = 'adamw'
    input_size: Literal[224, 336] = 224
    epochs: int = Field(default=100, ge=1, le=500)
    batch_size: int = Field(default=16, ge=1, le=64)
    seed: int = Field(default=42, ge=0, le=2**31-1)
    device: Literal['cpu', 'cuda'] = 'cpu'
    minimum_precision: float = Field(default=.95, ge=.5, le=1)


class TrainingRead(Bound):
    job_id: Identity


class ModelSelect(AgentModel):
    link_id: Identity
    model_id: Identity


class ModelActivate(ModelSelect):
    expected_active_model_id: Identity | None


class Disposition(AgentModel):
    reference: AgentRef
    item_ids: list[Identity] = Field(min_length=1, max_length=100)
    state: Literal['confirmed', 'deferred', 'reopened']
    label_id: Identity | None = None
    note: str = Field(default='', max_length=2000)


def bound(ctx, handoff_id):
    service = ctx.services.classification_handoff
    row = scoped_handoff(ctx, service, handoff_id)
    adapter = service._adapter(service._context(ctx.workspace_id, row['link_id']))
    native = adapter.import_status(row['request_id'])
    if native['state'] != 'completed':
        raise ValueError('资料尚未完整导入')
    fixed = ctx.services.asset_library[1].get(ctx.workspace_id, row['asset_set_id'], row['asset_set_revision'])
    mapping = {item['target_asset_id']: next(member for member in fixed.items if member.item_id == item['item_id']) for item in native['items']}
    return row, adapter, mapping


def read_labels(args, ctx):
    _, adapter, mapping = bound(ctx, args.handoff_id)
    items = []
    for native_id, member in list(mapping.items())[args.offset:args.offset+args.limit]:
        asset = adapter._request('GET', f'/api/v1/projects/{adapter.project_id}/assets/{native_id}')
        if asset['sha256'] != member.blob_hash:
            raise ValueError('分类原资料内容变化')
        items.append({'asset_id': member.asset_id, 'native_asset_id': native_id,
                      'label_id': asset['label_id'], 'group_id': asset['group_id'],
                      'expected_hash': digest({key: asset[key] for key in ('id', 'sha256', 'label_id', 'group_id')})})
    return AgentToolResult(status='success', summary='已读取固定范围的当前标注及条件写入版本',
        data={'items': items, 'labels': adapter.project()['labels'], 'active_model_id': adapter.project()['active_model_id'],
              'total': len(mapping), 'dataset_versions': list(adapter.dataset_versions())[:50]},
        next_offset=args.offset+args.limit if args.offset+args.limit < len(mapping) else None).model_dump()


def annotate(args, ctx):
    _, adapter, mapping = bound(ctx, args.handoff_id)
    reverse = {member.asset_id: native_id for native_id, member in mapping.items()}
    if any(item.asset_id not in reverse for item in args.items):
        raise ValueError('标注资料不属于固定范围')
    body = {'actor_id': ctx.session_id, 'items': [item.model_dump() | {'asset_id': reverse[item.asset_id]} for item in args.items]}
    try:
        receipt = adapter.agent_annotations(ctx.operation_id, body)
    except ClassificationConflict as error:
        raise Conflict('标注已变化') from error
    return AgentToolResult(status='success', summary='已保存 Agent 标注，作者与人工确认区分记录；旧训练版本不变', data=receipt).model_dump()


def annotate_lookup(args, ctx):
    _, adapter, _ = bound(ctx, args.handoff_id)
    value = adapter.agent_annotations(ctx.operation_id)
    if not value['committed']:
        return None
    return AgentToolResult(status='success', summary='已核实原Agent标注写入回执，未重复写入', data=value['receipt']).model_dump()


def publish(args, ctx):
    _, adapter, mapping = bound(ctx, args.handoff_id)
    value = adapter.publish_dataset(args.name, args.seed, list(mapping), ctx.operation_id)
    return AgentToolResult(status='success', summary='已发布仅覆盖固定资料的训练版本', data=value).model_dump()


def publish_lookup(args, ctx):
    _, adapter, _ = bound(ctx, args.handoff_id)
    value = adapter._request('GET', f'/api/v1/projects/{adapter.project_id}/dataset-requests/{ctx.operation_id}')
    if not value['submitted']:
        return None
    saved = next(item for item in adapter.dataset_versions() if item['id'] == value['dataset_id'])
    return AgentToolResult(status='success', summary='已核实固定训练版本原发布回执', data=saved).model_dump()


def submit(args, ctx, allow_new=True):
    row, adapter, mapping = bound(ctx, args.handoff_id)
    dataset = adapter.dataset(args.dataset_id)
    if not dataset['items'] or any(item['id'] not in mapping or item['sha256'] != mapping[item['id']].blob_hash for item in dataset['items']):
        raise ValueError('训练版本含范围外资料或内容变化')
    native_id = adapter.lookup_training(ctx.operation_id)
    if native_id is None:
        if not allow_new:
            return None
        native_id = adapter.train(args.dataset_id, key=ctx.operation_id,
            **args.model_dump(exclude={'handoff_id', 'dataset_id'}))
    job = ctx.services.job_service.register(ctx.workspace_id, row['link_id'], 'classification', [native_id])
    return AgentToolResult(status='pending', summary='已提交原生分类训练，等待真实模型产出', data={'job': job, 'dataset_id': args.dataset_id},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, application='classification', object_id=job['platform_job_id'],
                          revision=job['generation'], parent_id=args.handoff_id, coverage={'kind': 'train'})]).model_dump()


def lookup(args, ctx):
    return submit(args, ctx, False)


def read_training(args, ctx):
    row, adapter, _ = bound(ctx, args.handoff_id)
    job = read_job(ctx, args.job_id)
    if job['link_id'] != row['link_id'] or adapter.job(job['native_job_ids'][0]).kind != 'train':
        raise ValueError('训练任务不属于此关联')
    raw = adapter._request('GET', '/api/v1/jobs/'+job['native_job_ids'][0])
    if raw['params']['dataset_id'] not in {item['id'] for item in adapter.dataset_versions()}:
        raise ValueError('训练版本不可核实')
    # Recheck the fixed input against the immutable native training dataset.
    _, _, mapping = bound(ctx, args.handoff_id)
    dataset = adapter.dataset(raw['params']['dataset_id'])
    if any(item['id'] not in mapping or item['sha256'] != mapping[item['id']].blob_hash for item in dataset['items']):
        raise ValueError('训练任务含范围外资料')
    models = [{key: model[key] for key in ('id', 'name', 'metadata', 'metrics', 'job_id')} for model in adapter.models() if model['job_id'] in job['native_job_ids']]
    status = 'error' if job['status'] in {'failed', 'cancelled'} else 'success'
    if job['status'] == 'succeeded' and not models:
        return AgentToolResult(status='error', summary='训练任务结束但实际模型尚不可核实，不能交付或继续推理', data={'job': job, 'models': []},
            error={'code': 'model_unavailable', 'message': '请读取原生训练日志和模型目录'}).model_dump()
    return AgentToolResult(status=status, summary='已读取训练状态与实际模型，可将模型ID交给分类推理', data={'job': job, 'models': models, 'error': raw.get('error')},
        error={'code': job['status'], 'message': '训练未完成，请读取原生错误后调整参数重新提交'} if status == 'error' else None).model_dump()


def settle(ref, ctx):
    job = read_job(ctx, ref['object_id'])
    if job['status'] not in {'succeeded', 'partial', 'failed', 'cancelled'}:
        return None
    return read_training(TrainingRead(handoff_id=ref['parent_id'], job_id=ref['object_id']), ctx)


def activate(args, ctx):
    adapter = ctx.services.job_service._adapter(ctx.services.job_service._context(ctx.workspace_id, 'classification', args.link_id))
    if adapter.context.role == 'viewer' or args.model_id not in {model['id'] for model in adapter.models()}:
        raise ValueError('模型或关联无效')
    try:
        value = adapter._request('POST', f'/api/v1/projects/{adapter.project_id}/agent-models/{ctx.operation_id}',
            {'model_id': args.model_id, 'expected_active_model_id': args.expected_active_model_id, 'actor_id': ctx.session_id}, conditional=True)
    except ClassificationConflict as error:
        raise Conflict('活动模型已变化') from error
    return AgentToolResult(status='success', summary='已选择原生项目活动模型', data=value).model_dump()


def activate_lookup(args, ctx):
    adapter = ctx.services.job_service._adapter(ctx.services.job_service._context(ctx.workspace_id, 'classification', args.link_id))
    value = adapter._request('GET', f'/api/v1/projects/{adapter.project_id}/agent-models/{ctx.operation_id}')
    if not value['committed']:
        return None
    return AgentToolResult(status='success', summary='已核实原活动模型选择回执，未覆盖后续选择', data=value['receipt']).model_dump()


def export_model(args, ctx):
    import http.client
    from workbench_platform.artifacts.native_exports import publish as publish_export
    from workbench_platform.application_services.identity import canonical
    adapter = ctx.services.job_service._adapter(ctx.services.job_service._context(ctx.workspace_id, 'classification', args.link_id))
    model = next((model for model in adapter.models() if model['id'] == args.model_id), None)
    if not model:
        raise ValueError('模型不属于关联项目')
    connection = http.client.HTTPConnection('127.0.0.1', adapter.port, timeout=10)
    try:
        connection.request('GET', f'/api/v1/models/{args.model_id}/download', headers={'X-Platform-Request': '1'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('原生模型包校验未通过')
        payload = response.read(512*1024*1024+1)
        if not payload or len(payload) > 512*1024*1024:
            raise ValueError('模型包超出导出范围')
    finally:
        connection.close()
    metadata = {key: model[key] for key in ('id', 'name', 'project_id', 'job_id', 'metadata', 'metrics')}
    bundle = publish_export(ctx, '分类模型 · '+model['name'], {'model.zip': (payload, 'application/zip'),
        'model.json': (canonical(metadata).encode('utf-8'), 'application/json')}, [], [], args.model_id)
    return AgentToolResult(status='success', summary='已归集原生校验后的实际模型包与元数据', data={'bundle': bundle},
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle['bundle_id'], revision=1, application='classification')]).model_dump()


def export_model_lookup(args, ctx):
    from workbench_platform.artifacts.native_exports import lookup_result
    return lookup_result(ctx)


def dispose(args, ctx):
    from workbench_platform.application_services.references import verify
    from workbench_platform.application_services.native_mapping import binding, adapter_for
    from .classification import reference, snapshot
    if args.reference.kind != 'result' or args.reference.application != 'classification':
        raise ValueError('需要明确分类结果引用')
    checked = verify(args.reference.model_dump(), ctx)
    imported = binding(ctx, args.reference.parent_id)
    if imported:
        adapter = adapter_for(ctx, 'classification', imported['link_id'])
    else:
        _, adapter, _ = bound(ctx, args.reference.parent_id)
    valid_ids = {item['native_item_id'] for item in checked['items']}
    if not set(args.item_ids) <= valid_ids or len(set(args.item_ids)) != len(args.item_ids):
        raise ValueError('处置事项不属于所见范围')
    items = []
    total = adapter.job(args.reference.object_id).total
    for offset in range(0, total, 200):
        items.extend(item for item in adapter.results(args.reference.object_id, limit=min(200, total-offset), offset=offset) if item.native_item_id in args.item_ids)
    seen = {item['native_item_id']: item['disposition_revision'] for item in checked['items']}
    if any(item.disposition_revision != seen[item.native_item_id] for item in items):
        raise Conflict('分类事项版本变化')
    try:
        from workbench_platform.application_services.native_operations import origin
        origin(ctx, 'classification', adapter.context.link_id, adapter.context.instance_id)
        receipt = adapter.dispose(args.reference.object_id, items, args.state, args.label_id, args.note,
            operation_id=ctx.operation_id, actor={'id': 'agent_'+ctx.session_id[:50], 'display_name': 'AI审计Agent', 'source': 'local'}, intent='manual')
    except ClassificationConflict as error:
        raise Conflict('分类处置版本变化') from error
    ref = reference(snapshot(ctx, args.reference.parent_id), ctx)
    return AgentToolResult(status='success', summary='已按所见分类版本保存处置，记录AI操作人和原生历史', data=receipt, result_refs=[ref]).model_dump()


def dispose_lookup(args, ctx):
    from workbench_platform.application_services.native_mapping import binding, adapter_for, verify_sources
    from workbench_platform.application_services.native_operations import origin
    imported = binding(ctx, args.reference.parent_id)
    if imported:
        verify_sources(ctx, imported)
        adapter = adapter_for(ctx, 'classification', imported['link_id'])
    else:
        _, adapter, _ = bound(ctx, args.reference.parent_id)
    instance = origin(ctx, 'classification', adapter.context.link_id, adapter.context.instance_id)
    receipt = adapter.operation(ctx.operation_id, expected_instance_id=instance)
    if receipt is None:
        return None
    return AgentToolResult(status='success', summary='已核实原分类处置回执；继续处理前请读取最新结果版本', data=receipt).model_dump()
