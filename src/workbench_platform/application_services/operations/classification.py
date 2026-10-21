"""Native inference and prediction-only immutable subsets; no annotation writes."""
from dataclasses import replace
import uuid
from typing import Literal
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.application_services.native import availability, prepare, scoped_set, scoped_handoff, attempt_set, read_job
from workbench_platform.application_services.identity import digest


class ClassificationPrepare(AgentModel):
    link_id: Identity | None = None
    categories: list[str] | None = Field(default=None, min_length=2, max_length=100)


class ClassificationInfer(AgentModel):
    link_id: Identity
    asset_set_id: Identity
    asset_set_revision: int = Field(ge=1)
    model_id: Identity
    device: Literal['cpu', 'cuda'] = 'cpu'
    parent_operation_id: Identity | None = None


class ClassificationRead(AgentModel):
    handoff_id: Identity
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class ClassificationExport(AgentModel):
    handoff_id: Identity


class SelectPredictions(ClassificationExport):
    label_ids: list[Identity] = Field(min_length=1, max_length=100)
    minimum_score: float = Field(default=0.0, ge=0, le=1)
    prediction_statuses: list[str] = Field(default_factory=list, max_length=20)


def prepare_tool(args, context):
    value = prepare(context, 'classification', args.link_id, args.categories)
    return AgentToolResult(status='success', summary='已核实分类项目、模型与标签目录', data=value).model_dump()


def pending(value, context):
    job, handoff = value['job'], value['handoff']
    return AgentToolResult(status='pending', summary='已提交真实分类推理，保留人工标签',
        data={'handoff_id': handoff['handoff_id'], 'model_id': handoff['processing']['model_id']},
        job_refs=[AgentRef(kind='job', workspace_id=context.workspace_id, object_id=job['platform_job_id'],
            revision=job['generation'], application='classification', parent_id=handoff['handoff_id'])]).model_dump()


def submission(args, context, allow_new, *, include_not_submitted=False):
    original = scoped_set(context, args.asset_set_id, args.asset_set_revision)
    attempt = attempt_set(context, original)
    handoff = context.services.classification_handoff
    context.current()
    imported = handoff.begin(context.workspace_id, args.link_id, attempt.asset_set_id, attempt.revision, {})
    # Empty labels intentionally preserve the native annotation workflow.
    context.current()
    value = handoff.submit(context.workspace_id, imported['handoff_id'], args.model_id, args.device, allow_new=allow_new)
    if value.get('not_submitted'):
        if include_not_submitted and value.get('authoritative_negative') is True:
            from workbench_platform.application_services.errors import NativeNotSubmitted
            return NativeNotSubmitted(context.operation_id, 'classification_infer', value['native_request_id'],
                value['native_project_id'], value['input_fingerprint'])
        return None
    return pending(value, context)


def infer(args, context):
    return submission(args, context, True)


def lookup(args, context, *, include_not_submitted=False):
    if include_not_submitted and availability(context.services, 'classification')['state'] == 'preparable':
        prepare(replace(context, operation_id='l' + uuid.uuid4().hex), 'classification', args.link_id)
        context.current()
    return submission(args, context, False, include_not_submitted=include_not_submitted)


def snapshot(context, handoff_id):
    from workbench_platform.application_services.native_mapping import binding, classification_snapshot
    imported = binding(context, handoff_id)
    if imported:
        return classification_snapshot(context, imported)
    service = context.services.classification_handoff
    scoped_handoff(context, service, handoff_id)
    return service.result_snapshot(context.workspace_id, handoff_id)


def reference(value, context):
    return AgentRef(kind='result', workspace_id=context.workspace_id, object_id=value['native_job_id'],
        native_id=value['native_job_id'], revision=digest(value['items']), application='classification',
        parent_id=value['handoff_id'], sha256=digest(value['items']),
        coverage=value['platform_job']['counts'] | ({'origin': 'native_binding'} if value.get('native_binding') else {}))


def read(args, context):
    value = snapshot(context, args.handoff_id)
    items = value['items']
    return AgentToolResult(status='success', summary='已读取预测结果；人工标注与预测分别展示',
        data={key: value[key] for key in ('handoff_id', 'model_id', 'project_id', 'input_set_id', 'input_set_revision')} |
             {'items': items[args.offset:args.offset + args.limit], 'total': len(items)},
        result_refs=[reference(value, context)], coverage=value['platform_job']['counts'],
        next_offset=args.offset + args.limit if args.offset + args.limit < len(items) else None).model_dump()


def select(args, context):
    value = snapshot(context, args.handoff_id)
    from workbench_platform.application_services.native_mapping import binding, adapter_for
    imported = binding(context, args.handoff_id)
    if imported:
        adapter = adapter_for(context, 'classification', imported['link_id'])
    else:
        row = context.services.classification_handoff._row(context.workspace_id, args.handoff_id)
        adapter = context.services.classification_handoff._adapter(
            context.services.classification_handoff._context(context.workspace_id, row['link_id']))
    labels = {label['label_id'] for label in adapter.project()['labels']}
    if not set(args.label_ids) <= labels:
        raise ValueError('所选类别不属于结果项目')
    original = scoped_set(context, value['input_set_id'], value['input_set_revision'])
    selected = {item['asset_id'] for item in value['items'] if item['state'] == 'succeeded'
        and item['prediction_label_id'] in args.label_ids
        and item['prediction_score'] is not None and item['prediction_score'] >= args.minimum_score
        and (not args.prediction_statuses or item['prediction_status'] in args.prediction_statuses)}
    members = [member for member in original.items if member.asset_id in selected]
    if not members:
        return AgentToolResult(status='success', summary='预测筛选未命中资料，未创建空任务',
            data={'count': 0, 'condition': args.model_dump(), 'model_id': value['model_id']},
            result_refs=[reference(value, context)]).model_dump()
    parents = {member.item_id: (original.asset_set_id, original.revision, member.item_id) for member in members}
    subset = context.services.asset_library[1].create(context.workspace_id, members, parents=parents,
        asset_set_id='s' + context.operation_id[1:])
    return AgentToolResult(status='success', summary='已按预测创建不可变资料子集，可继续OCR；人工标签保持原状',
        data={'asset_set': subset.model_dump(mode='json'), 'condition': args.model_dump(),
              'model_id': value['model_id'], 'source_result_hash': digest(value['items'])},
        result_refs=[AgentRef(kind='asset_set', workspace_id=context.workspace_id, object_id=subset.asset_set_id,
            revision=subset.revision, sha256=subset.manifest_hash, sources=[reference(value, context)])]).model_dump()


def export(args, context):
    snapshot(context, args.handoff_id)
    bundle = context.services.artifact_center.publish_classification_result(context.workspace_id, args.handoff_id)
    return AgentToolResult(status='success', summary='已归集真实分类结果和来源', data={'bundle': bundle},
        artifact_refs=[AgentRef(kind='artifact', workspace_id=context.workspace_id, object_id=bundle['bundle_id'],
            revision=1, application='classification', parent_id=args.handoff_id)]).model_dump()


def settle(ref, context):
    context.current()
    job = read_job(context, ref['object_id'])
    context.current()
    if job['status'] == 'paused':
        # Native recovery requeues interrupted inference; paused is deliberate
        # control (or historical unknown intent), never an observer's resume.
        return None
    if job['status'] not in {'succeeded', 'partial', 'failed', 'cancelled'}:
        return None
    context.current()
    value = read(ClassificationRead(handoff_id=ref['parent_id']), context)
    value['job_refs'] = [ref]
    if job['status'] in {'failed', 'cancelled'}:
        value.update(status='error', error={'code': job['status'], 'message': '分类任务未成功，请读取错误与可用模型', 'retryable': False})
    elif job['status'] == 'partial':
        value['warnings'].append('分类输入部分失败，子集仅来自实际成功预测')
    return value
