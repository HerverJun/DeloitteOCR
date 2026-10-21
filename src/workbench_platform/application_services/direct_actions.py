"""Small receipt helpers shared by the fixed local business actions."""
from .errors import DirectActionFault
import asyncio
import json
from urllib.parse import quote

from packages.contracts.agent import AgentToolResult
from .execution import invoke
from .identity import Conflict, digest, now
from .image_text import ImageTextContext, ReceiptUnknown, _operation
from .job_observations import progress_for, publish_settled_progress


async def submit_action(runtime, session_id, request, *, driver, goal, asset_ids=None, images=False, source_text=None):
    parameters = request.model_dump(mode='json', exclude={'request_id'})
    if driver == 'classification-predict-v1' and not parameters.get('use_existing_model'):
        parameters.pop('use_existing_model', None)
        parameters.pop('model_name', None)
    if source_text is not None:
        parameters['source_text'] = source_text
    previous = runtime.store.rows('SELECT * FROM runs WHERE request_id=?', (request.request_id,))
    if previous:
        run = previous[0]
        saved = json.loads(run.get('driver_state') or '{}')
        if run['session_id'] != session_id or saved.get('direct_origin_driver', run.get('driver')) != driver or saved.get('request') != parameters:
            raise Conflict('请求身份与原业务或参数不同')
        return run
    session = runtime.store.session(session_id)
    task = runtime.tasks.get(session_id)
    if task.get('pending_scope'):
        runtime.tasks.apply_scope(session_id)
        session, task = runtime.store.session(session_id), runtime.tasks.get(session_id)
    if request.expected_scope_revision is not None and task['scope_revision'] != request.expected_scope_revision:
        raise Conflict('资料范围已改变，请核对后重新提交')
    identifiers = list(asset_ids if asset_ids is not None else session['selected_assets'])
    if not identifiers or len(identifiers) != len(set(identifiers)) or len(identifiers) > 100:
        raise DirectActionFault('请明确选择 1–100 份不同的资料')
    if session['selected_assets'] and not set(identifiers) <= set(session['selected_assets']):
        raise DirectActionFault('请求资料超出本会话选中范围')
    inputs = []
    registry = runtime.services.asset_library[0]
    for identifier in identifiers:
        asset = registry.get(session['workspace_id'], identifier)
        if images and not asset.media_type.startswith('image/'):
            raise DirectActionFault('分类预测只接受已选择的图片')
        inputs.append({'asset_id': identifier, 'revision': asset.revision, 'blob_hash': asset.blob_hash})
    saved = {'version': 1, 'request': parameters, 'inputs': inputs, 'scope_revision': task['scope_revision'],
             'stage': 'prepare', 'receipts': {}, 'timings': {}}
    from .direct_scope import resolve_resource
    saved['resource_object'], saved['resolved_link_id'] = resolve_resource(runtime, session, driver, request.request_id,
                                                                          getattr(request, 'link_id', None))
    if (source_text is not None or parameters.get('use_existing_model') or
            (driver == 'classification-predict-v1' and not parameters.get('link_id'))):
        from workbench_platform.resources.reader_features import require_features
        require_features(runtime.store.root.parent, 'direct-catalog-goals-v1')
    run, fresh = runtime.store.start_run(session_id, goal, request.request_id, 0, driver=driver, driver_state=saved)
    if fresh:
        runtime.tasks.record_goal(session_id, goal, request.request_id)
        runtime.schedule(run['id'], None)
    return run


def action_context(runtime, run, saved):
    if saved.get('version') != 1:
        raise ReceiptUnknown('直接操作恢复契约版本不受支持，未提交任何新操作')
    ctx = ImageTextContext(run['workspace_id'], run['session_id'], run['id'], run['generation'], 0, '',
        tuple(item['asset_id'] for item in saved['inputs']), runtime.services, runtime.store,
        expected_scope=saved['scope_revision'],
        input_versions=tuple((item['asset_id'], item['revision'], item['blob_hash']) for item in saved['inputs']))
    ctx.current()
    return ctx


def checkpoint(ctx, saved, stage, name, value):
    ctx.current()
    saved['stage'] = stage
    saved['receipts'][name] = value
    saved['timings'][stage] = now()
    ctx.store.save_driver_state(ctx.run_id, ctx.generation, saved)


async def operation(runtime, ctx, name, arguments, handler, *, lookup=None, safe_replay=False):
    value, current = await _operation(runtime, ctx, name, arguments, handler, lookup=lookup, safe_replay=safe_replay)
    if value['status'] != 'pending':
        if value.get('job_refs'):
            await invoke(publish_settled_progress, current, value['job_refs'])
        return value
    pending = value
    runtime.store.status(ctx.run_id, ctx.generation, 'waiting_job')
    state = {'run_id': ctx.run_id, 'generation': ctx.generation, 'session_id': ctx.session_id,
             'workspace_id': ctx.workspace_id}
    last, unavailable = None, 0
    while True:
        current.current()
        observations = [await invoke(progress_for, ref, current) for ref in pending['job_refs']]
        fingerprint = digest(observations)
        if fingerprint != last:
            for ref, observation in zip(pending['job_refs'], observations, strict=True):
                if observation['status'] not in {'succeeded', 'partial', 'failed', 'cancelled'}:
                    try:
                        current.store.public_event(current.run_id, current.generation,
                            current.operation_id + ':progress:' + digest(observation), 'job_progress',
                            {**observation, 'job_ref': ref, 'sampled_at': now()})
                    except OSError:
                        pass
            last = fingerprint
        if any(item.get('observation') == 'unavailable' for item in observations):
            from .job_lifecycle import prepare_observation
            for reference, observation in zip(pending['job_refs'], observations, strict=True):
                if observation.get('observation') == 'unavailable':
                    # Restore only the service needed to observe the original
                    # accepted receipt. Never re-enter the submission handler.
                    await invoke(prepare_observation, reference, current)
            unavailable += 1
            await asyncio.sleep(min(30, 2 ** min(unavailable, 5)))
            continue
        unavailable = 0
        value = await runtime.settle(pending, state, context=current)
        if value is not None:
            break
        await asyncio.sleep(.5)
    current.current()
    value = {**value, 'job_refs': pending['job_refs']}
    current.store.finish_operation(current.operation_id, value)
    await invoke(publish_settled_progress, current, pending['job_refs'])
    current.store.status(current.run_id, current.generation, 'running')
    return value


def require_success(value):
    if value['status'] == 'needs_verification':
        raise ReceiptUnknown(value['summary'])
    if value['status'] != 'success':
        raise ValueError(value.get('summary') or '固定业务操作未成功，原回执与已有成果保留')
    return value


async def finish_action(runtime, ctx, *, driver, summary, result_refs, artifact_refs, coverage=None, data=None, saved=None):
    ctx.current()
    status = 'partial' if any((coverage or {}).get(name, 0) for name in ('failed', 'cancelled', 'pending')) else 'succeeded'
    def publish(arguments, current):
        from .references import verify
        published = []
        for ref in arguments['artifact_refs']:
            bundle = verify(ref, current)
            base = f'/api/platform/v1/workspaces/{current.workspace_id}/artifacts/{bundle["bundle_id"]}/files/'
            published.append({**bundle, 'files': [{**file, 'download_url': base + quote(file['name'])}
                                                  for file in bundle['files']]})
        if not published:
            raise DirectActionFault('业务已结束，但没有可读取和下载的固定成果')
        return AgentToolResult(status='success', summary=summary, result_refs=arguments['result_refs'],
            artifact_refs=arguments['artifact_refs'], coverage=coverage or {}, data={**(data or {}),
                'verified': True, 'finalized': True, 'driver': driver, 'rule_version': 'input-requirements-v1',
                'completeness': 'partial' if status == 'partial' else 'complete', 'delivery': published}).model_dump(mode='json')
    delivery, _ = await _operation(runtime, ctx, driver + '_publish',
        {'result_refs': result_refs, 'artifact_refs': artifact_refs}, publish, safe_replay=True)
    body = {'status': status, 'content': delivery['summary'], 'delivery': delivery}
    ctx.store.public_event(ctx.run_id, ctx.generation, ctx.run_id + ':direct-delivery', 'delivery', body)
    if saved is not None:
        checkpoint(ctx, saved, 'complete', 'delivery', delivery)
    ctx.store.status(ctx.run_id, ctx.generation, status)
    ctx.store.finish_public(ctx.run_id, status, generation=ctx.generation)
    return body
