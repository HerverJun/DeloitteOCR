"""Owned-job inspection, cancellation and deterministic failed-input attempts."""
from dataclasses import replace
import json
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentToolResult, Identity
from workbench_platform.application_services.native import read_job, scoped_set, scoped_handoff
from workbench_platform.application_services.identity import digest
from . import ocr, classification, forensics, data


class JobGet(AgentModel):
    job_id: Identity


class JobRetry(JobGet):
    engines: list[Identity] | None = Field(default=None, min_length=1, max_length=16)
    model_id: Identity | None = None


def owned(ctx, job_id):
    # Only jobs actually produced by an operation in this session qualify;
    # sharing a workspace is insufficient authority to cancel another task.
    rows = ctx.store.rows('SELECT o.* FROM operations o JOIN runs r ON r.id=o.run_id '
        'WHERE r.session_id=? AND o.result IS NOT NULL ORDER BY o.created', (ctx.session_id,))
    for row in rows:
        value = json.loads(row['result'])
        for ref in value.get('job_refs', []):
            if ref['object_id'] == job_id:
                if ref['application'] == 'multimodal' and row['tool'] not in {
                    'face_analyze', 'audio_transcribe', 'seal_extract', 'catnet_analyze', 'document_forensics_analyze', 'aigc_image_analyze',
                    'multimodal_prepare', 'multimodal_retry', 'jobs_retry_failed'}:
                    continue
                return row, ref
    raise ValueError('任务不是当前会话创建的任务')


def get(args, ctx):
    _, ref = owned(ctx, args.job_id)
    value = read_reference(ref, ctx)
    return AgentToolResult(status='success', summary='已读取当前会话原生任务的真实状态', data=value).model_dump()


def read_reference(ref, ctx):
    """Read one owned native identity; does not submit, resume or poll a model."""
    from workbench_platform.application_services.job_lifecycle import adapter
    return adapter(ref).read(ref, ctx)


def cancel_reference(ref, ctx):
    if ref['application'] == 'multimodal':
        owned(ctx, ref['object_id'])
    from workbench_platform.application_services.job_lifecycle import adapter
    return adapter(ref).cancel(ref, ctx)


def cancel(args, ctx):
    _, ref = owned(ctx, args.job_id)
    value = cancel_reference(ref, ctx)
    return AgentToolResult(status='unsupported' if value.get('status') == 'unsupported' else 'success',
        summary='已按原生能力请求取消；回执不代表进程已终止' if value.get('status') != 'unsupported' else value['message'],
        data=value).model_dump()


def retry(args, ctx, allow_new=True):
    old, ref = owned(ctx, args.job_id)
    with ctx.store.transaction() as db:
        db.execute('UPDATE operations SET parent_id=? WHERE id=?', (old['id'], ctx.operation_id))
    family = ref['application']
    if family == 'multimodal':
        from .multimodal import scoped_job, Retry, retry as retry_multimodal
        item = scoped_job(ctx, args.job_id, owned=True)
        return retry_multimodal(Retry(job_id=args.job_id, expected_revision=item['revision'],
            expected_generation=item['generation']), ctx, lookup=not allow_new)
    special = ref.get('coverage', {}).get('kind')
    if special in {'train', 'visual_review', 'fusion', 'batch'}:
        next_tool = {'train': 'classification_train', 'visual_review': 'ocr_visual_review',
                     'fusion': 'ocr_fuse_results', 'batch': 'batch_control'}[special]
        return AgentToolResult(status='unsupported', summary='此任务不是图片推理失败子集；读取原错误后用对应业务工具创建新操作',
            data={'job_id': args.job_id, 'kind': special, 'next_tool': next_tool}).model_dump()
    if ref.get('coverage', {}).get('kind') == 'pdf_pages':
        from . import pdf
        receipt = pdf.status(ref, ctx)
        failed = [stage['page_number'] for stage in receipt['stages'] if stage['status'] == 'failed']
        if not failed or any(stage['status'] in {'queued', 'running', 'waiting_gpu'} for stage in receipt['stages']):
            raise ValueError('没有最终失败页可重试')
        service, row, _, _ = pdf.bound(ctx, ref['parent_id'], ref['native_id'])
        inspected = pdf.inspect(pdf.InspectDocument(link_id=row['link_id'], asset_set_id=row['asset_set_id'],
                                 asset_set_revision=row['asset_set_revision']), ctx)
        _, _, _, documents = pdf.bound(ctx, inspected['data']['handoff_id'])
        if len(documents) != 1:
            raise ValueError('失败PDF输入映射不明确')
        parameters = receipt['parameters']
        handler = pdf.process if allow_new else pdf.lookup
        return handler(pdf.ProcessPages(handoff_id=inspected['data']['handoff_id'],
            document_id=next(iter(documents)), page_numbers=failed, mode=parameters['mode'],
            engine=args.engines[0] if args.engines else parameters['engine'], force=True,
            parent_operation_id=old['id']), ctx)
    if family not in {'ocr', 'classification'}:
        return AgentToolResult(status='unsupported', summary='此任务族没有失败子集重试入口；原任务和成果保留').model_dump()
    job = read_job(ctx, args.job_id)
    if job['status'] not in {'partial', 'failed'} or not job['counts']['failed']:
        raise ValueError('任务没有可核实的失败输入')
    service = ctx.services.ocr_handoff if family == 'ocr' else ctx.services.classification_handoff
    row = scoped_handoff(ctx, service, ref['parent_id'])
    original = scoped_set(ctx, row['asset_set_id'], row['asset_set_revision'])
    adapter = service._adapter(service._context(ctx.workspace_id, row['link_id']))
    if family == 'ocr':
        batch = adapter.batch(job['native_job_ids'])
        raw_tasks = {item['id']: item for item in adapter.snapshot()['tasks']}
        failed = {(task.image_id, raw_tasks[task.native_task_id].get('input_version_id') or task.version_id)
                  for task in batch.tasks if task.native_status == 'failed'}
        native = adapter.import_status(row['request_id'])
        ids = {item['item_id'] for item in native['items']
               if (item.get('target_asset_id'), item.get('target_version_id')) in failed}
    else:
        snapshot = service.result_snapshot(ctx.workspace_id, ref['parent_id'])
        failed = {item['asset_id'] for item in snapshot['items'] if item['state'] == 'failed'}
        ids = {item.item_id for item in original.items if item.asset_id in failed}
    members = [item for item in original.items if item.item_id in ids]
    if not members:
        raise ValueError('无法将失败项映射到固定资料')
    subset = ctx.services.asset_library[1].create(ctx.workspace_id, members,
        parents={item.item_id: (original.asset_set_id, original.revision, item.item_id) for item in members},
        asset_set_id='s' + digest([ctx.operation_id, 'failed'])[:40])
    ancestor = old
    seen = set()
    lineage = [old]
    while ancestor['tool'] == 'jobs_retry_failed':
        if ancestor['id'] in seen or not ancestor['parent_id']:
            raise ValueError('重试父操作无法核实')
        seen.add(ancestor['id'])
        parent = ctx.store.rows('SELECT o.* FROM operations o JOIN runs r ON r.id=o.run_id '
            'WHERE o.id=? AND r.session_id=?', (ancestor['parent_id'], ctx.session_id))
        if not parent:
            raise ValueError('重试父操作不属于当前会话')
        ancestor = parent[0]
        lineage.append(ancestor)
    parameters = json.loads(ancestor['arguments'])
    parameters.update(asset_set_id=subset.asset_set_id, asset_set_revision=subset.revision,
                      link_id=row['link_id'], parent_operation_id=old['id'])
    # A retry is a new identified attempt. Native retry_failed endpoints without
    # a request lookup are deliberately avoided; successful inputs are excluded.
    if family == 'ocr':
        parameters['engines'] = args.engines or latest_choice(lineage, 'engines') or parameters.get('engines')
        parameters.pop('job_id', None)
        value = (ocr.run if allow_new else ocr.lookup)(ocr.OcrRun.model_validate(parameters), ctx)
    else:
        parameters['model_id'] = args.model_id or latest_choice(lineage, 'model_id') or parameters.get('model_id')
        parameters.pop('job_id', None)
        value = (classification.infer if allow_new else classification.lookup)(classification.ClassificationInfer.model_validate(parameters), ctx)
    if value is None:
        return None
    with ctx.store.transaction() as db:
        db.execute('UPDATE operations SET parent_id=? WHERE id=?', (old['id'], ctx.operation_id))
    value['data'].update(failed_input_set=subset.model_dump(mode='json'), parent_operation_id=old['id'],
                         excluded_successful_inputs=len(original.items) - len(members))
    return value


def lookup_retry(args, ctx):
    return retry(args, ctx, allow_new=False)


def latest_choice(lineage, key):
    for operation in lineage:
        result = json.loads(operation['result']) if operation['result'] else {}
        value = (result.get('data') or {}).get(key) or json.loads(operation['arguments']).get(key)
        if value:
            return value
    return None
