"""Scoped business adapters to Clear's native multimodal workers, never shell tools."""
from contextlib import closing
from functools import partial
import json
from typing import Literal

from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from packages.contracts.multimodal import ManualInput, ManualOptions
from workbench_platform.application_services.identity import canonical, digest, Conflict
from workbench_platform.application_services.references import asset_allowed

KINDS = ('face', 'audio', 'seal', 'catnet')


class Analyze(AgentModel):
    inputs: list[ManualInput] = Field(min_length=1, max_length=2)
    options: ManualOptions = Field(default_factory=ManualOptions)


class Prepare(Analyze):
    kind: Literal['face', 'audio', 'seal', 'catnet']
    inputs: list[ManualInput] = Field(min_length=1, max_length=1)


class Result(AgentModel):
    job_id: Identity
    snapshot_id: Identity | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=50)


class Collect(Result):
    require_review: bool = False


class Retry(AgentModel):
    job_id: Identity
    expected_revision: int = Field(ge=1)
    expected_generation: int = Field(ge=1)
    device: Literal['cpu', 'cuda'] | None = None


class Empty(AgentModel):
    pass


class UserAction(AgentModel):
    action: Literal['review', 'reference_add', 'reference_delete']
    job_id: Identity | None = None
    snapshot_id: Identity | None = None
    reference_id: Identity | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    input_index: int = Field(default=0, ge=0, le=1)
    face_index: int | None = Field(default=None, ge=0, le=1000)
    label: str | None = Field(default=None, min_length=1, max_length=128)


class ApplyAction(AgentModel):
    decision_id: Identity


def service(ctx):
    value = getattr(ctx.services, 'multimodal_service', None)
    if value is None:
        raise ValueError('多模态业务服务未配置')
    return value


def validate_reference_ids(services, workspace, identifiers):
    if not identifiers:
        return
    native = getattr(services, 'multimodal_service', None)
    if native is None:
        raise ValueError('参考库未配置')
    available = {item['reference_id'] for item in native.references(workspace)['items']}
    if len(set(identifiers)) != len(identifiers) or not set(identifiers) <= available:
        raise ValueError('授权参考不存在或不属于当前项目')


def authorized(ctx):
    return set(ctx.services.agent.tasks.get(ctx.session_id).get('authorized_reference_ids', []))


def availability(services, kind):
    native = getattr(services, 'multimodal_service', None)
    if native is None:
        return {'state': 'unavailable', 'reason': '多模态业务服务未配置'}
    item = next((item for item in native.capabilities()['items'] if item['kind'] == kind), {})
    ready = item.get('cpu_ready') or item.get('gpu_ready')
    return {'state': 'ready' if ready else 'unavailable' if item.get('missing') else 'preparable',
        'reason': '' if ready else '缺少资源：' + '、'.join(item['missing']) if item.get('missing') else '已有资源，尚缺有效的本机推理验证回执',
        'dependencies': item.get('missing', []), 'cpu_ready': bool(item.get('cpu_ready')),
        'gpu_ready': bool(item.get('gpu_ready')), 'hard_cancel': True}


def scoped_job(ctx, job_id, *, owned=False):
    if owned:
        from .jobs import owned as ownership
        _, ref = ownership(ctx, job_id)
        if ref['application'] != 'multimodal':
            raise ValueError('任务类型不一致')
    item = service(ctx).get(ctx.workspace_id, job_id)
    if any(not asset_allowed(ctx, source['asset_id']) for source in item['inputs']):
        raise ValueError('分析来源超出任务资料范围')
    return item


def job_ref(ctx, item):
    return AgentRef(kind='job', application='multimodal', workspace_id=ctx.workspace_id,
        object_id=item['job_id'], parent_id=item['job_id'], revision=item['generation'],
        coverage={'kind': item['kind'], 'generation': item['generation']})


def submitted(ctx, item):
    return AgentToolResult(status='pending', summary='已提交平台原生分析，任务层等待权威回执后继续',
        data={'job_id': item['job_id'], 'generation': item['generation'], 'status': item['status'],
              'purpose': item.get('purpose', 'analysis')},
        job_refs=[job_ref(ctx, item)]).model_dump()


def prepare(args, ctx):
    if any(not asset_allowed(ctx, item.asset_id) for item in args.inputs):
        raise ValueError('准备诊断资料超出当前任务范围')
    item = service(ctx).prepare(ctx.workspace_id, args.kind, {'request_id': ctx.operation_id,
        'inputs': [item.model_dump() for item in args.inputs], 'options': args.options.model_dump()})
    return submitted(ctx, item)


def lookup_prepare(args, ctx):
    receipt = service(ctx).lookup_operation(ctx.workspace_id, 'prepare', ctx.operation_id)
    return submitted(ctx, scoped_job(ctx, receipt['resource_id'])) if receipt['found'] else None


def analyze(kind, args, ctx):
    if any(not asset_allowed(ctx, item.asset_id) for item in args.inputs):
        raise ValueError('分析资料超出当前任务范围')
    options = args.options.model_dump()
    if kind != 'face':
        options['action'] = 'analyze'
    references = options.get('reference_ids') or []
    if kind == 'face':
        if not set(references) <= authorized(ctx):
            raise ValueError('人脸检索参考未获本任务授权')
        if options['action'] == 'search' and not references:
            from workbench_platform.application_services.decisions import ask_user, AskUser
            return ask_user(AskUser(question='人脸检索需要明确参考范围，请在平台工具面板选择本任务授权的参考人脸。'), ctx)
    options['reference_ids'] = references if kind == 'face' else []
    item = service(ctx).create(ctx.workspace_id, {'request_id': ctx.operation_id, 'kind': kind,
        'inputs': [item.model_dump() for item in args.inputs], 'options': options})
    return submitted(ctx, item)


def lookup_analysis(kind, args, ctx):
    value = service(ctx).lookup_operation(ctx.workspace_id, 'create', ctx.operation_id)
    if not value['found']:
        return None
    item = scoped_job(ctx, value['resource_id'])
    if item['kind'] != kind:
        raise ValueError('原提交类型不一致')
    return submitted(ctx, item)


def checked_snapshot(ctx, args):
    scoped_job(ctx, args.job_id)
    snapshot_id = args.snapshot_id
    if snapshot_id is None and ctx.operation_id:
        from workbench_platform.resources.reader_features import require_features
        require_features(ctx.store.root.parent, 'agent-multimodal-v1')
        with ctx.store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS multimodal_operation_snapshots(operation_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,job_id TEXT NOT NULL,snapshot_id TEXT NOT NULL)')
            pinned = db.execute('SELECT * FROM multimodal_operation_snapshots WHERE operation_id=?', (ctx.operation_id,)).fetchone()
        if pinned:
            if (pinned['workspace_id'], pinned['job_id']) != (ctx.workspace_id, args.job_id):
                raise ValueError('原操作已固定另一份结果')
            snapshot_id = pinned['snapshot_id']
    snapshot = service(ctx).snapshot(ctx.workspace_id, args.job_id, snapshot_id)
    if args.snapshot_id is None and ctx.operation_id and snapshot_id is None:
        with ctx.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO multimodal_operation_snapshots VALUES(?,?,?,?)',
                (ctx.operation_id, ctx.workspace_id, args.job_id, snapshot['snapshot_id']))
            pinned = db.execute('SELECT * FROM multimodal_operation_snapshots WHERE operation_id=?', (ctx.operation_id,)).fetchone()
            if (pinned['workspace_id'], pinned['job_id'], pinned['snapshot_id']) != (ctx.workspace_id, args.job_id, snapshot['snapshot_id']):
                raise Conflict('原操作固定结果已变化，请恢复原操作回执')
    if any(not asset_allowed(ctx, source['asset_id']) for source in snapshot['sources']):
        raise ValueError('快照资料超出任务范围')
    reference_ids = {item['reference_id'] for item in snapshot.get('reference_scope', [])}
    if reference_ids and (not ctx.session_id or not reference_ids <= authorized(ctx)):
        # A completed citation retains the authorization at its actual submit
        # boundary, even after the task's reference picker changes.
        from .jobs import owned
        try:
            operation, _ = owned(ctx, args.job_id)
            original = json.loads(operation['arguments']).get('options', {}).get('reference_ids', [])
        except ValueError:
            original = []
        if not reference_ids <= set(original):
            raise ValueError('快照包含本任务未授权的参考人脸')
    for reference in snapshot.get('reference_scope', []):
        source = reference.get('source') or {}
        if not all(key in source for key in ('asset_id', 'asset_revision', 'blob_hash')):
            raise ValueError('冻结参考缺少原件身份')
        registry = ctx.services.asset_library[0]
        asset = registry.get(ctx.workspace_id, source['asset_id'])
        if (asset.revision, asset.blob_hash) != (source['asset_revision'], source['blob_hash']):
            raise ValueError('冻结参考原件版本或内容变化')
        with registry.stage_verified(ctx.workspace_id, source['asset_id'], revision=source['asset_revision'],
                                     blob_hash=source['blob_hash']):
            pass
    return snapshot


def result_ref(ctx, snapshot):
    return AgentRef(kind='result', application='multimodal', workspace_id=ctx.workspace_id,
        object_id=snapshot['snapshot_id'], parent_id=snapshot['job_id'], native_id=snapshot['job_id'],
        revision=snapshot['revision'], sha256=snapshot['snapshot_hash'],
        coverage={'kind': snapshot['kind'], 'generation': snapshot['generation'],
                  'reviewed': bool(snapshot['review_revision']), 'draft': not bool(snapshot['review_revision'])},
        sources=[AgentRef(kind='asset', workspace_id=ctx.workspace_id, object_id=item['asset_id'],
            revision=item['asset_revision'], sha256=item['blob_hash']) for item in snapshot['sources']])


def read(args, ctx):
    item = scoped_job(ctx, args.job_id)
    if not args.snapshot_id and item['status'] in {'queued', 'running', 'cancelling'}:
        return submitted(ctx, item)
    if not args.snapshot_id and item['status'] != 'succeeded':
        status = 'needs_verification' if item['status'] == 'needs_verification' else 'error'
        return AgentToolResult(status=status, summary=item.get('error') or '原生分析未完成',
            data={'job_id': item['job_id'], 'native_status': item['status']}, job_refs=[job_ref(ctx, item)],
            error={'code': 'multimodal_' + item['status'], 'message': (item.get('error') or '原生分析未完成')[:500]}).model_dump()
    snapshot = checked_snapshot(ctx, args)
    if snapshot['analysis'].get('purpose') == 'preparation':
        return AgentToolResult(status='success', summary='本机准备诊断已完成；继续提交实际业务分析以形成交付结果',
            data={'job_id': item['job_id'], 'purpose': 'preparation', 'prepared_kind': snapshot['kind'],
                  'generation': snapshot['generation'], 'review_state': 'not_applicable'},
            job_refs=[job_ref(ctx, snapshot['analysis'])], warnings=['工程诊断回执不作为业务分析或交付成果']).model_dump()
    result = snapshot['analysis'].get('result') or {}
    field = {'audio': 'segments', 'seal': 'seals', 'face': 'images'}.get(snapshot['kind'])
    values = result.get(field, []) if field else []
    if snapshot['kind'] == 'face':
        values = [{'input_index': index, **face} for index, image in enumerate(values) for face in image.get('faces', [])]
    data = {key: value for key, value in snapshot.items() if key != 'analysis'}
    data.update(result={key: value for key, value in result.items() if key != field},
                review=snapshot['analysis'].get('review'))
    if field:
        data.update(items=values[args.offset:args.offset + args.limit], item_kind=field, total=len(values), offset=args.offset)
    return AgentToolResult(status='success', summary='已读取不可变分析结果及来源；' + ('有用户复核回执' if snapshot['review_revision'] else '未人工复核，可用于草稿分析'),
        data=data, result_refs=[result_ref(ctx, snapshot)], job_refs=[job_ref(ctx, snapshot['analysis'])],
        next_offset=args.offset + args.limit if args.offset + args.limit < len(values) else None,
        warnings=[] if snapshot['review_revision'] else ['未人工复核；算法执行成功不代表业务结论已成立'],
        coverage={'kind': snapshot['kind'], 'reviewed': bool(snapshot['review_revision']), 'items': len(values)}).model_dump()


def export(args, ctx):
    snapshot = checked_snapshot(ctx, args)
    _, files = service(ctx).snapshot_export(ctx.workspace_id, args.job_id, snapshot['snapshot_id'])
    base = f'/api/platform/v1/workspaces/{ctx.workspace_id}/multimodal/jobs/{args.job_id}/snapshots/{snapshot["snapshot_id"]}'
    return AgentToolResult(status='success', summary='已生成固定版本导出，保留复核状态和原件来源',
        result_refs=[result_ref(ctx, snapshot)], data={'snapshot_id': snapshot['snapshot_id'],
        'files': [{'name': name, 'sha256': service(ctx).file_hash(path), 'size_bytes': path.stat().st_size,
                   'download_url': base + '/files/' + name} for name, path in files.items()],
        'download_url': base + '/download', 'review_state': snapshot['review_state']}).model_dump()


def collect(args, ctx):
    snapshot = checked_snapshot(ctx, args)
    if args.require_review and (snapshot['analysis'].get('review') or {}).get('assessment') != 'confirmed':
        return request_action(UserAction(action='review', job_id=args.job_id, snapshot_id=snapshot['snapshot_id']), ctx)
    bundle = service(ctx).collect_snapshot(ctx.workspace_id, args.job_id, snapshot['snapshot_id'])
    return AgentToolResult(status='success', summary='已归集固定版本成果；' + ('含复核回执' if snapshot['review_revision'] else '标记为未复核草稿'),
        data={'bundle': bundle, 'review_state': snapshot['review_state']},
        result_refs=[result_ref(ctx, snapshot)],
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle['bundle_id'], revision=1,
            application='multimodal', parent_id=args.job_id, coverage={'kind': snapshot['kind'],
            'snapshot_id': snapshot['snapshot_id'], 'reviewed': bool(snapshot['review_revision']), 'draft': not bool(snapshot['review_revision'])})]).model_dump()


def to_data(args, ctx):
    snapshot = checked_snapshot(ctx, args)
    value = service(ctx).draft_to_data(ctx.workspace_id, args.job_id,
        {'request_id': ctx.operation_id, 'snapshot_id': snapshot['snapshot_id'], 'expected_revision': snapshot['revision']}, ctx=ctx)
    if isinstance(value, dict) and value.get('status') in {'success', 'pending'}:
        return value
    return transfer_pending(value, snapshot, ctx, draft=True)


def to_data_reviewed(args, ctx):
    snapshot = checked_snapshot(ctx, args)
    if (snapshot['analysis'].get('review') or {}).get('assessment') != 'confirmed':
        return request_action(UserAction(action='review', job_id=args.job_id, snapshot_id=snapshot['snapshot_id']), ctx)
    value = service(ctx).to_data(ctx.workspace_id, args.job_id, ctx=ctx, snapshot_id=snapshot['snapshot_id'])
    return transfer_pending(value, snapshot, ctx, draft=False)


def transfer_pending(value, snapshot, ctx, *, draft):
    job, asset = value['job'], value['asset']
    rows = ctx.store.rows('SELECT operation_id FROM derived_inputs WHERE workspace_id=? AND asset_id=?',
                          (ctx.workspace_id, asset['asset_id']))
    if not rows:
        raise ValueError('转换输入缺少本任务来源账本')
    service = ctx.services.data_workbench
    original = service.operation(rows[0]['operation_id'])
    if not original or original['id'] != job['id'] or original['session_id'] != value['session_id']:
        raise ValueError('转换数据提交回执身份不一致')
    ref = AgentRef(kind='job', workspace_id=ctx.workspace_id, application='data', object_id=job['id'],
        parent_id=value['session_id'], native_id=job['id'], revision=service.get(value['session_id'])['version'],
        coverage={'kind': 'multimodal_transfer', 'request_id': rows[0]['operation_id'],
            'analysis_job_id': snapshot['job_id'], 'snapshot_id': snapshot['snapshot_id'],
            'derived_asset_id': asset['asset_id'], 'draft': draft})
    return AgentToolResult(status='pending', summary='已提交确定性字段转换，任务层等待原生数据导入完成',
        data={**value, 'data_session_id': value['session_id'], 'draft': draft}, job_refs=[ref],
        result_refs=[result_ref(ctx, snapshot), AgentRef(kind='asset', workspace_id=ctx.workspace_id,
            object_id=asset['asset_id'], revision=asset['asset_revision'], sha256=asset['blob_hash'])],
        warnings=['草稿转换不构成人工核准'] if draft else []).model_dump()


def settle_transfer(ref, ctx):
    service = ctx.services.data_workbench
    metadata = ref['coverage']
    snapshot = checked_snapshot(ctx, Result(job_id=metadata['analysis_job_id'], snapshot_id=metadata['snapshot_id']))
    session = service.get(ref['parent_id'])
    if session['workspace_id'] != ctx.workspace_id:
        raise ValueError('转换数据作业项目不一致')
    job = service.job(ref['parent_id'], ref['object_id'])
    if job['status'] == 'interrupted':
        original = service.operation(metadata['request_id'])
        if (not original or original['id'] != ref['object_id']
                or original['session_id'] != ref['parent_id']):
            raise ValueError('原数据转换操作无法核实')
        job = service.resume_operation(metadata['request_id'])
    if job['status'] in {'queued', 'running', 'cancelling'}:
        return None
    from .data import settle
    value = settle(ref, ctx)
    if value is None or value['status'] != 'success':
        return value
    asset = ctx.services.asset_library[0].get(ctx.workspace_id, metadata['derived_asset_id'])
    value['data'].update(draft=metadata['draft'], review_state='unreviewed' if metadata['draft'] else 'reviewed',
                         snapshot_id=snapshot['snapshot_id'], asset_id=asset.asset_id)
    value['result_refs'] = [result_ref(ctx, snapshot).model_dump(), AgentRef(kind='asset', workspace_id=ctx.workspace_id,
        object_id=asset.asset_id, revision=asset.revision, sha256=asset.blob_hash).model_dump()]
    return value


def retry(args, ctx, *, lookup=False):
    item = scoped_job(ctx, args.job_id, owned=True)
    action = 'retry_cpu' if args.device == 'cpu' else 'retry'
    if args.device == 'cuda' and item['options']['device'] != 'cuda':
        raise ValueError('现有重试入口不能扩大执行设备')
    if lookup:
        receipt = service(ctx).lookup_operation(ctx.workspace_id, 'control:' + action, ctx.operation_id)
        return submitted(ctx, receipt['receipt']) if receipt['found'] else None
    value = service(ctx).control(ctx.workspace_id, args.job_id, action,
        {'request_id': ctx.operation_id, 'expected_revision': args.expected_revision,
         'expected_generation': args.expected_generation})
    return submitted(ctx, value)


def list_references(args, ctx):
    ids = authorized(ctx)
    items = [item for item in service(ctx).references(ctx.workspace_id)['items'] if item['reference_id'] in ids]
    return AgentToolResult(status='success', summary='仅展示本任务明确授权的参考人脸，不含特征向量',
        data={'items': items, 'authorized_reference_ids': sorted(ids)}).model_dump()


def action_table(ctx):
    from workbench_platform.resources.reader_features import require_features
    require_features(ctx.store.root.parent, 'agent-multimodal-v1')
    with ctx.store.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS multimodal_user_actions(decision_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,body TEXT NOT NULL)')


def request_action(args, ctx):
    """Only this fixed, version-bound decision can authorize a human action."""
    body = args.model_dump()
    if args.action in {'review', 'reference_add'}:
        if not args.job_id:
            raise ValueError('操作缺少原任务')
        snapshot = checked_snapshot(ctx, Result(job_id=args.job_id, snapshot_id=args.snapshot_id))
        if args.expected_revision is not None and args.expected_revision != snapshot['revision']:
            raise ValueError('请求核准的版本已变化')
        body.update(snapshot_id=snapshot['snapshot_id'], expected_revision=snapshot['revision'],
                    generation=snapshot['generation'], snapshot_hash=snapshot['snapshot_hash'])
        if args.action == 'reference_add' and (snapshot['kind'] != 'face' or args.face_index is None or not args.label):
            raise ValueError('参考登记需要指定已检测人脸和名称')
    else:
        if not args.reference_id or args.reference_id not in authorized(ctx) or args.expected_revision is None:
            raise ValueError('删除参考需明确授权的参考与版本')
        selected_reference = next((item for item in service(ctx).references(ctx.workspace_id)['items']
            if item['reference_id'] == args.reference_id), None)
        if selected_reference is None or selected_reference.get('revision', 1) != args.expected_revision:
            raise ValueError('参考人脸或版本已变化')
    from workbench_platform.application_services.decisions import ask_user, AskUser
    question = (f'请核准链接中的固定算法结果（版本 {snapshot["revision"]}）后继续正式成果。' if args.action == 'review' else
                f'请确认将链接原图第 {args.input_index + 1} 张、检测人脸序号 {args.face_index} 以“{args.label}”加入项目参考库。' if args.action == 'reference_add' else
                f'请确认删除参考人脸“{selected_reference.get("label", args.reference_id)}”（{args.reference_id}，版本 {args.expected_revision}）。')
    result = ask_user(AskUser(question=question, options=['确认此版本', '不确认']), ctx)
    action_table(ctx)
    with ctx.store.transaction() as db:
        existing = db.execute('SELECT body FROM multimodal_user_actions WHERE decision_id=?', (ctx.operation_id,)).fetchone()
        if existing and existing['body'] != canonical(body):
            raise Conflict('确认问题已绑定另一个结果版本')
        db.execute('INSERT OR IGNORE INTO multimodal_user_actions VALUES(?,?,?)', (ctx.operation_id, ctx.session_id, canonical(body)))
    result['data'].update(action=body, review_state='waiting_user')
    if args.action in {'review', 'reference_add'}:
        result['result_refs'] = [result_ref(ctx, snapshot).model_dump()]
    return result


def apply_action(args, ctx):
    action_table(ctx)
    rows = ctx.store.rows('SELECT a.body,d.reply,d.request_id FROM multimodal_user_actions a JOIN decisions d ON d.id=a.decision_id '
        'JOIN runs r ON r.id=d.run_id WHERE a.decision_id=? AND a.session_id=? AND r.session_id=?',
        (args.decision_id, ctx.session_id, ctx.session_id))
    if not rows or not rows[0]['request_id'] or rows[0]['reply'] != '确认此版本':
        raise ValueError('没有绑定此版本的用户核准回执，模型不能代签')
    body = json.loads(rows[0]['body'])
    native = service(ctx)
    operation_type = 'review' if body['action'] == 'review' else body['action']
    old = native.lookup_operation(ctx.workspace_id, operation_type, ctx.operation_id)
    if old['found']:
        return (read(Result(job_id=body['job_id'], snapshot_id=native.snapshot_at_revision(ctx.workspace_id,
            body['job_id'], old['receipt']['revision'])['snapshot_id']), ctx) if body['action'] == 'review' else
                AgentToolResult(status='success', summary='已恢复原用户指令的操作回执', data=old['receipt']).model_dump())
    if body['action'] in {'review', 'reference_add'}:
        item = scoped_job(ctx, body['job_id'])
        snapshot = native.snapshot(ctx.workspace_id, body['job_id'], body['snapshot_id'])
        if (snapshot['snapshot_hash'] != body['snapshot_hash'] or item['generation'] != body['generation']
                or item['revision'] != body['expected_revision']):
            raise ValueError('核准后结果版本已变化，需要重新核准')
    if body['action'] == 'review':
        previous_review = snapshot['analysis'].get('review') or {}
        value = native.review(ctx.workspace_id, body['job_id'], {'request_id': ctx.operation_id,
            'expected_revision': body['expected_revision'], 'actor': '用户回执:' + rows[0]['request_id'],
            'note': '用户核准固定快照 ' + body['snapshot_id'], 'assessment': 'confirmed',
            'edits': previous_review.get('edits', []), 'regions': previous_review.get('regions', [])})
        frozen = native.snapshot_at_revision(ctx.workspace_id, value['job_id'], value['revision'])
        return read(Result(job_id=value['job_id'], snapshot_id=frozen['snapshot_id']), ctx)
    if body['action'] == 'reference_add':
        value = native.add_reference(ctx.workspace_id, {'request_id': ctx.operation_id, 'job_id': body['job_id'],
            'input_index': body['input_index'], 'face_index': body['face_index'], 'label': body['label']})
    else:
        value = native.delete_reference(ctx.workspace_id, body['reference_id'],
            {'request_id': ctx.operation_id, 'expected_revision': body['expected_revision']})
    return AgentToolResult(status='success', summary='已按绑定版本的用户指令执行参考库操作', data=value).model_dump()


def lookup_user_action(args, ctx):
    action_table(ctx)
    rows = ctx.store.rows('SELECT body FROM multimodal_user_actions WHERE decision_id=? AND session_id=?',
                           (args.decision_id, ctx.session_id))
    if not rows:
        return None
    body = json.loads(rows[0]['body'])
    if not service(ctx).lookup_operation(ctx.workspace_id, body['action'], ctx.operation_id)['found']:
        return None
    # apply_action validates the persisted actual user reply before restoring
    # the original version's receipt. It never invents a new review or actor.
    return apply_action(args, ctx)


def delivery_checks(snapshot, *, require_review=False):
    """Check independent evidence, not the wording of an assistant's answer."""
    analysis = snapshot['analysis']
    result = analysis.get('result') or {}
    evidence = {'snapshot_id': snapshot['snapshot_id'], 'job_id': snapshot['job_id'],
        'generation': snapshot['generation'], 'snapshot_hash': snapshot['snapshot_hash'],
        'result_hash': snapshot['result_hash'], 'model_identity': snapshot['model_identity']}
    checks = [{'name': 'multimodal_result_identity', 'status': 'passed', 'message': '固定结果身份、哈希及原件已核实', 'evidence': evidence}]
    if analysis.get('purpose') == 'preparation':
        checks.append({'name': 'business_analysis_required', 'status': 'failed',
            'message': '工程准备诊断不能充当业务交付，请继续实际业务分析', 'evidence': evidence})
    if require_review:
        review = analysis.get('review') or {}
        confirmed = snapshot['review_revision'] > 0 and review.get('assessment') == 'confirmed'
        checks.append({'name': 'human_review', 'status': 'passed' if confirmed else 'failed',
            'message': '已有绑定版本的用户复核回执' if confirmed else '正式成果要求当前结果人工核准', 'evidence': evidence})
    if snapshot['kind'] == 'audio':
        metadata = result.get('metadata') or result.get('audio') or {}
        duration = metadata.get('duration_ms', result.get('duration_ms'))
        segments = result.get('segments', [])
        valid = isinstance(duration, (int, float)) and duration > 0
        sources = {item['asset_id'] for item in snapshot['sources']}
        for row in segments:
            locator = row.get('source') or {}
            start, end = row.get('start_ms'), row.get('end_ms')
            valid = valid and type(start) is int and type(end) is int and 0 <= start < end <= duration
            valid = valid and locator.get('kind') == 'media_segment' and locator.get('asset_id') in sources
            valid = valid and locator.get('start_ms') == start and locator.get('end_ms') == end
        checks.append({'name': 'audio_time_coverage', 'status': 'passed' if valid else 'failed',
            'message': '音频片段可回查原录音时间范围' if valid else '音频时长或片段来源范围无法核实',
            'evidence': {**evidence, 'duration_ms': duration, 'segments': len(segments)}})
    return checks


def settle(ref, ctx):
    item = scoped_job(ctx, ref['object_id'])
    if item['status'] in {'queued', 'running', 'cancelling'}:
        return None
    if item['status'] == 'needs_verification':
        # Native ownership proof is required. Recovery never creates another job.
        recover = getattr(service(ctx), 'recover', None)
        if recover:
            item = recover(ctx.workspace_id, item['job_id'], {'request_id': 'r' + digest([ctx.operation_id, item['job_id'], item['generation']])[:40],
                'expected_revision': item['revision'], 'expected_generation': item['generation']})
            if item['status'] in {'queued', 'running'}:
                return None
    return read(Result(job_id=item['job_id']), ctx)
