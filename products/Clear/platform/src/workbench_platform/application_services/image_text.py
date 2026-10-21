"""Fixed image-to-text business action over existing runs and native receipts.

No model request, Harness process or alternate native scheduler is used here.
The immutable request and operation ledger are the recovery authority.
"""
from __future__ import annotations

from .errors import DirectActionFault

import asyncio
from dataclasses import dataclass, replace
import json
import time

from packages.contracts.agent import AgentRef, AgentToolResult
from packages.contracts.direct import ImageTextRequest
from packages.contracts.models import AssetSetItem, SourceLocator
from .context import ToolContext
from .identity import Conflict, RunSuperseded, canonical, digest, now


class ReceiptUnknown(Conflict):
    """A native boundary may have accepted a write; only lookup may follow."""


@dataclass(frozen=True)
class ImageTextContext(ToolContext):
    expected_scope: int = 1
    input_versions: tuple = ()

    def current(self):
        super().current()
        tasks = self.services.agent.tasks
        task = tasks.get(self.session_id)
        if task.get('pending_scope') or task['scope_revision'] != self.expected_scope or task.get('manual'):
            raise Conflict('资料范围或处理权限已改变，原任务保留，请核对后发起新要求')
        registry = self.services.asset_library[0]
        for identifier, revision, sha256 in self.input_versions:
            asset = registry.get(self.workspace_id, identifier)
            if (asset.revision, asset.blob_hash) != (revision, sha256):
                raise Conflict('原请求的图片版本已改变，未继续写入')


async def submit(runtime, session_id, request, *, source_text=None, adopt_run_id=None,
                 analysis_goal=None, interpretation_request_id=None, expected_inputs=None):
    parameters = {**request.model_dump(exclude={'request_id'}), 'source_text': source_text}
    previous = runtime.store.rows('SELECT * FROM runs WHERE request_id=?', (request.request_id,))
    if previous and adopt_run_id is None:
        run = previous[0]
        saved = json.loads(run.get('driver_state') or '{}')
        if run['session_id'] != session_id or saved.get('direct_origin_driver', run.get('driver')) != 'image-text-v1' or saved.get('request') != parameters:
            raise Conflict('请求身份与原操作或参数不同')
        return run
    session = runtime.store.session(session_id)
    task = runtime.tasks.get(session_id)
    if task.get('pending_scope'):
        runtime.tasks.apply_scope(session_id)
        session = runtime.store.session(session_id)
        task = runtime.tasks.get(session_id)
    if request.expected_scope_revision is not None and task['scope_revision'] != request.expected_scope_revision:
        raise Conflict('资料范围已改变，请核对后重新提交')
    if not session['selected_assets']:
        raise DirectActionFault('请先选择需要提取文字的图片')
    registry = runtime.services.asset_library[0]
    inputs = []
    for identifier in session['selected_assets']:
        asset = registry.get(session['workspace_id'], identifier)
        if not asset.media_type.startswith('image/'):
            raise DirectActionFault('图片文字提取只接受图片；PDF 请使用指定页提取入口')
        inputs.append({'asset_id': identifier, 'revision': asset.revision, 'blob_hash': asset.blob_hash})
    if expected_inputs is not None and inputs != expected_inputs:
        raise Conflict('原件版本已改变，未将新资料替换为原目标输入')
    saved = {'version': 1, 'request': parameters, 'inputs': inputs,
             'scope_revision': task['scope_revision'], 'stage': 'prepare', 'receipts': {}, 'timings': {}}
    if analysis_goal is not None:
        if analysis_goal != source_text:
            raise Conflict('后续分析必须保留完整原用户目标')
        saved['analysis_goal'] = analysis_goal
    if interpretation_request_id is not None:
        saved['interpretation_request_id'] = interpretation_request_id
    from .direct_scope import resolve_resource
    saved['resource_object'], saved['resolved_link_id'] = resolve_resource(runtime, session, 'image-text-v1',
                                                                          request.request_id, request.link_id)
    goal = source_text or '提取所选图片文字'
    from .action_admission import start
    run, fresh = start(runtime, session_id, goal, request.request_id, 'image-text-v1', saved, adopt_run_id=adopt_run_id)
    if fresh:
        runtime.tasks.record_goal(session_id, goal, request.request_id)
        runtime.schedule(run['id'], None)
    return run


def _context(runtime, run, saved):
    if saved.get('version') != 1:
        raise ReceiptUnknown('直接操作恢复契约版本不受支持，未提交任何新操作')
    ctx = ImageTextContext(run['workspace_id'], run['session_id'], run['id'], run['generation'], 0, '',
                      tuple(item['asset_id'] for item in saved['inputs']), runtime.services, runtime.store,
                      expected_scope=saved['scope_revision'],
                      input_versions=tuple((item['asset_id'], item['revision'], item['blob_hash']) for item in saved['inputs']))
    ctx.current()
    task = runtime.tasks.get(run['session_id'])
    if task.get('pending_scope'):
        runtime.tasks.apply_scope(run['session_id'])
        raise Conflict('用户已修订资料范围，原任务停止后续写入；请核对后发起新要求')
    if task['scope_revision'] != saved['scope_revision'] or task.get('manual'):
        raise Conflict('资料范围或处理权限已改变，原任务保留，请核对后发起新要求')
    registry = runtime.services.asset_library[0]
    for item in saved['inputs']:
        asset = registry.get(ctx.workspace_id, item['asset_id'])
        if (asset.revision, asset.blob_hash) != (item['revision'], item['blob_hash']):
            raise Conflict('原请求的图片版本已改变，未继续写入')
    return ctx


async def _operation(runtime, ctx, name, arguments, handler, *, lookup=None, safe_replay=False):
    from .execution import invoke, tool_activity
    from .errors import NativeNotSubmitted
    call = {'call_id': 'direct-' + name + '-' + digest(arguments)[:24], 'tool': name, 'arguments': arguments}
    previous = ctx.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?', (ctx.run_id, call['call_id']))
    if not previous:
        # Check before creating an intent: a blocked fresh write must remain
        # provably unsubmitted, so a later continuation can submit it once.
        ctx.store.claim_run_scope(ctx.run_id, ctx.generation)
    record, fresh = ctx.store.begin_operation(ctx.run_id, ctx.generation, call, arguments.get('parent_operation_id'))
    ctx = replace(ctx, operation_id=record['id'])
    if not fresh and record['state'] in {'finished', 'pending'}:
        return json.loads(record['result']), ctx
    async def original_receipt():
        try:
            return await invoke(lookup, arguments, ctx) if lookup else None
        except RunSuperseded:
            raise
        except Exception:
            raise ReceiptUnknown('原生回执服务暂不可核实；请恢复原服务后继续，未重新提交') from None
    negative = None
    if not fresh and not safe_replay:
        value = await original_receipt()
        ctx.current()
        if isinstance(value, NativeNotSubmitted):
            if (value.operation_id != record['id'] or value.tool != name or not value.request_id
                    or not value.project_id or not value.input_fingerprint):
                raise ReceiptUnknown('未提交负回执与原操作身份不同；未重新提交')
            negative = value
        elif value is None:
            raise ReceiptUnknown('原操作写入回执尚不可核实；未重新提交')
    if fresh or safe_replay or negative is not None:
        if not fresh:
            ctx.store.claim_run_scope(ctx.run_id, ctx.generation)
        if negative is not None:
            # The authority confirms this exact stable key is absent. Native
            # submission deduplicates that key even if an old writer races.
            # Re-enter the same operation; never create another attempt/key.
            ctx.store.public_event(ctx.run_id, ctx.generation,
                record['id'] + ':not-submitted:' + str(ctx.generation), 'native_not_submitted',
                {'operation_id': record['id'], 'native_request_id': negative.request_id,
                 'native_project_id': negative.project_id, 'input_fingerprint': negative.input_fingerprint,
                 'action': 'continue_same_operation'})
        tool_activity(ctx, call, 'started')
        try:
            value = await invoke(handler, arguments, ctx)
        except RunSuperseded:
            raise
        except Exception:
            if not safe_replay:
                value = await original_receipt()
                if value is None or isinstance(value, NativeNotSubmitted):
                    raise ReceiptUnknown('原操作写入回执尚不可核实；未重新提交') from None
            else:
                raise
    ctx.current()
    value = AgentToolResult.model_validate(value).model_dump(mode='json')
    ctx.store.finish_operation(record['id'], value)
    tool_activity(ctx, call, 'pending' if value['status'] == 'pending' else 'finished', value)
    return value, ctx


def _fixed_inputs(arguments, ctx):
    items = []
    for item in arguments['inputs']:
        locator = SourceLocator(schema_version='1.0', kind='file', asset_id=item['asset_id'],
            asset_revision=item['revision'], blob_hash=item['blob_hash'], precision='unknown')
        items.append(AssetSetItem(schema_version='1.0', item_id='i' + digest(item)[:32],
            asset_id=item['asset_id'], asset_revision=item['revision'], blob_hash=item['blob_hash'], source_locator=locator))
    value = ctx.services.asset_library[1].create(ctx.workspace_id, items, asset_set_id='s' + ctx.operation_id[1:])
    return AgentToolResult(status='success', summary='已固定所选图片版本', data=value.model_dump(mode='json')).model_dump()


def _engines(catalog, requested):
    available = {item['id']: item for item in catalog['engines'] if item.get('available', True) is not False}
    if requested:
        if len(set(requested)) != len(requested) or not set(requested) <= available.keys():
            raise DirectActionFault('指定引擎不在当前项目可用目录中')
        return requested
    if 'auto' in available:
        return ['auto']
    # Older native installations retain their available general text engine.
    preferred = [name for name in available if 'ppocr' in name.lower() or 'paddleocr' in name.lower()]
    if not preferred:
        preferred = [name for name in available if available[name].get('available', True) is not False]
    if not preferred:
        raise DirectActionFault('没有可用的图片文字识别引擎')
    return [preferred[0]]


async def drive(runtime, run_id):
    from .native import prepare
    from .operations import ocr
    from .execution import invoke
    from .job_observations import progress_for
    from .job_lifecycle import prepare_observation
    from workbench_platform.artifacts.text_outputs import publish_text_result
    run = runtime.store.run(run_id)
    saved = json.loads(run['driver_state'])
    ctx = _context(runtime, run, saved)
    state = {'run_id': run_id, 'generation': run['generation'], 'session_id': run['session_id'],
             'workspace_id': run['workspace_id']}

    def commit(stage, **receipts):
        ctx.current()
        saved['stage'] = stage
        saved['receipts'].update(receipts)
        saved['timings'][stage] = now()
        runtime.store.save_driver_state(run_id, ctx.generation, saved)

    if 'prepared' not in saved['receipts']:
        runtime.activity(state, 'preparing_request', reason='准备所选图片文字提取')
        def prepare_action(arguments, current):
            value = prepare(current, 'ocr', arguments['link_id'])
            return AgentToolResult(status='success', summary='文字识别服务已准备', data=value).model_dump()
        prepared, _ = await _operation(runtime, ctx, 'image_text_prepare',
                                       {'link_id': saved.get('resolved_link_id') or saved['request']['link_id']},
                                       prepare_action, safe_replay=True)
        if prepared['data'].get('requires_project_selection'):
            raise DirectActionFault('存在多个 OCR 项目，请先明确选择要使用的项目')
        commit('inputs', prepared=prepared['data'])
    if 'input_set' not in saved['receipts']:
        frozen, _ = await _operation(runtime, ctx, 'image_text_inputs', {'inputs': saved['inputs']}, _fixed_inputs, safe_replay=True)
        commit('submit', input_set=frozen['data'])
    if 'submitted' not in saved['receipts']:
        prepared, frozen = saved['receipts']['prepared'], saved['receipts']['input_set']
        arguments = {'link_id': prepared['link_id'], 'asset_set_id': frozen['asset_set_id'],
            'asset_set_revision': frozen['revision'], 'engines': _engines(prepared, saved['request']['engines'])}
        pending, submission_context = await _operation(runtime, ctx, 'ocr_run', arguments,
            lambda args, current: ocr.run(ocr.OcrRun.model_validate(args), current),
            lookup=lambda args, current: ocr.lookup(ocr.OcrRun.model_validate(args), current, include_not_submitted=True))
        if not pending.get('job_refs'):
            raise DirectActionFault('文字识别未返回可观察的原作业回执')
        if pending['status'] == 'pending':
            commit('waiting', submitted=pending, submitted_operation_id=submission_context.operation_id,
                   submit_arguments=arguments)
        else:
            # The durable operation may have finished immediately before the
            # driver checkpoint. Preserve its fixed result; never re-read a
            # newer manual revision under the old operation's identity.
            commit('delivery', submitted={**pending, 'status': 'pending'}, result=pending,
                   submitted_operation_id=submission_context.operation_id, submit_arguments=arguments)
    submitted = saved['receipts']['submitted']
    arguments = saved['receipts'].get('submit_arguments') or {'link_id': saved['receipts']['prepared']['link_id'],
            'asset_set_id': saved['receipts']['input_set']['asset_set_id'],
            'asset_set_revision': saved['receipts']['input_set']['revision'],
            'engines': _engines(saved['receipts']['prepared'], saved['request']['engines'])}
    records = ctx.store.rows('SELECT * FROM operations WHERE run_id=? AND call_id=?',
        (run_id, 'direct-ocr_run-' + digest(arguments)[:24]))
    if len(records) != 1 or (saved['receipts'].get('submitted_operation_id') and
                            saved['receipts']['submitted_operation_id'] != records[0]['id']):
        raise ReceiptUnknown('原文字识别操作身份尚不可核实，未提交新的原生任务')
    operation = records[0]
    settled = saved['receipts'].get('result')
    if settled is None and operation['state'] == 'finished' and operation['result']:
        settled = json.loads(operation['result'])
    if settled is None:
        runtime.store.status(run_id, ctx.generation, 'waiting_job')
        last_progress, unavailable = None, 0
        while True:
            ctx = replace(_context(runtime, run, saved), operation_id=operation['id'])
            progress = await invoke(progress_for, submitted['job_refs'][0], ctx)
            fingerprint = digest(progress)
            if fingerprint != last_progress and progress.get('status') not in {'succeeded', 'partial', 'failed', 'cancelled'}:
                runtime.store.public_event(run_id, ctx.generation, run_id + ':direct-progress:' + fingerprint,
                    'job_progress', {**progress, 'job_ref': submitted['job_refs'][0], 'sampled_at': now()})
                last_progress = fingerprint
            if progress.get('observation') == 'unavailable':
                await invoke(prepare_observation, submitted['job_refs'][0], ctx)
                unavailable += 1
                await asyncio.sleep(min(30, 2 ** min(unavailable, 5)))
                continue
            unavailable = 0
            settled = await runtime.settle(submitted, state, context=ctx)
            if settled is not None:
                break
            await asyncio.sleep(.5)
        ctx.current()
        settled = {**settled, 'job_refs': submitted['job_refs']}
        ctx.store.finish_operation(operation['id'], settled)
    else:
        ctx = replace(ctx, operation_id=operation['id'])
    if settled['status'] == 'needs_verification':
        raise ReceiptUnknown(settled['summary'])
    from .job_observations import publish_settled_progress
    await invoke(publish_settled_progress, ctx, submitted['job_refs'])
    # A bounded fallback is allowed only for the unspecified text operation.
    # Exact engine requests, permission failures and unknown writes retain
    # their original requirement and are never silently substituted.
    if not saved['request']['engines'] and (settled['status'] == 'error' or settled.get('coverage', {}).get('failed')):
        from .requirement_coverage import OcrCoverageReader, UNKNOWN_CODES
        units = await invoke(OcrCoverageReader(ctx).attempt, submitted['job_refs'][0])
        failures = [unit for unit in units if unit['state'] == 'failed']
        if any(unit.get('failure_code') in UNKNOWN_CODES for unit in failures):
            raise ReceiptUnknown('原生识别回执未知，先核实原尝试；未提交后备任务')
        allowed_codes = {'engine_load_timeout', 'engine_inference_timeout', 'engine_failed', 'engine_process_failed'}
        candidates = [item['id'] for item in saved['receipts']['prepared']['engines']
                      if item['id'] in {'ppocr', 'paddlevl'} and item.get('available', True) is not False
                      and item['id'] not in arguments['engines']]
        can_retry = failures and candidates and all(unit.get('failure_code') in allowed_codes for unit in failures)
        if can_retry:
            fallback = saved['receipts'].get('fallback')
            if fallback is None:
                failed_keys = {(unit['asset_id'], unit['asset_revision'], unit['blob_hash']) for unit in failures}
                retry_inputs = [item for item in saved['inputs']
                                if (item['asset_id'], item['revision'], item['blob_hash']) in failed_keys]
                if not retry_inputs:
                    raise ReceiptUnknown('失败输入的原件映射不可核实，未提交后备任务')
                retry_set, _ = await _operation(runtime, ctx, 'image_text_retry_inputs', {'inputs': retry_inputs},
                                                _fixed_inputs, safe_replay=True)
                retry_arguments = {'link_id': saved['receipts']['prepared']['link_id'],
                    'asset_set_id': retry_set['data']['asset_set_id'], 'asset_set_revision': retry_set['data']['revision'],
                    'engines': [candidates[0]], 'parent_operation_id': operation['id']}
                retry_result, retry_context = await _operation(runtime, ctx, 'ocr_run', retry_arguments,
                    lambda args, current: ocr.run(ocr.OcrRun.model_validate(args), current),
                    lookup=lambda args, current: ocr.lookup(ocr.OcrRun.model_validate(args), current, include_not_submitted=True))
                fallback = {'arguments': retry_arguments, 'operation_id': retry_context.operation_id,
                            'submitted': retry_result}
                if retry_result['status'] != 'pending':
                    fallback['result'] = retry_result
                commit('fallback_wait', fallback=fallback, result=settled)
            retry_context = replace(_context(runtime, run, saved), operation_id=fallback['operation_id'])
            retried = fallback.get('result')
            if retried is None:
                recorded = ctx.store.rows('SELECT * FROM operations WHERE id=? AND run_id=?',
                                          (fallback['operation_id'], run_id))
                if len(recorded) != 1:
                    raise ReceiptUnknown('后备尝试身份不可核实，未再次提交')
                if recorded[0]['state'] == 'finished' and recorded[0]['result']:
                    retried = json.loads(recorded[0]['result'])
            delayed = 0
            while retried is None:
                retry_context.current()
                progress = await invoke(progress_for, fallback['submitted']['job_refs'][0], retry_context)
                if progress.get('observation') == 'unavailable':
                    await invoke(prepare_observation, fallback['submitted']['job_refs'][0], retry_context)
                    delayed += 1
                    await asyncio.sleep(min(30, 2 ** min(delayed, 5)))
                    continue
                delayed = 0
                if progress.get('status') not in {'succeeded', 'partial', 'failed', 'cancelled'}:
                    runtime.store.public_event(run_id, ctx.generation,
                        fallback['operation_id'] + ':progress:' + digest(progress), 'job_progress',
                        {**progress, 'job_ref': fallback['submitted']['job_refs'][0], 'sampled_at': now()})
                retried = await runtime.settle(fallback['submitted'], state, context=retry_context)
                if retried is None:
                    await asyncio.sleep(.5)
            if retried['status'] == 'needs_verification':
                raise ReceiptUnknown(retried['summary'])
            retried = {**retried, 'job_refs': fallback['submitted']['job_refs']}
            ctx.store.finish_operation(fallback['operation_id'], retried)
            fallback['result'] = retried
            commit('delivery', fallback=fallback)
            await invoke(publish_settled_progress, retry_context, fallback['submitted']['job_refs'])
            refs = {canonical(ref): ref for ref in [*settled.get('result_refs', []), *retried.get('result_refs', [])]}
            if refs:
                from .requirement_coverage import project_requirements
                records = [{**row, 'arguments': json.loads(row['arguments']), 'result': json.loads(row['result'])}
                           for row in ctx.store.rows('SELECT * FROM operations WHERE run_id=? AND result IS NOT NULL', (run_id,))]
                coverage = await invoke(project_requirements, records, list(refs.values()), ctx)
                settled = {**settled, 'status': 'success', 'result_refs': list(refs.values()),
                           'coverage': {'total': coverage['total'], 'succeeded': coverage['satisfied'],
                                        'failed': coverage['missing'], 'cancelled': 0, 'pending': len(coverage['unverified'])}}
                saved['requirement_coverage'] = coverage
    if settled['status'] == 'error':
        commit('failed', result=settled)
        runtime.finish_error(run_id, ctx.generation, 'failed', (settled.get('error') or {}).get('code', 'ocr_failed'),
                             settled['summary'])
        return
    commit('delivery', result=settled)
    status = 'partial' if any(settled.get('coverage', {}).get(key, 0) for key in ('failed', 'cancelled', 'pending')) else 'succeeded'
    async def publish_action(arguments, current):
        outputs = [await invoke(publish_text_result, current, reference) for reference in arguments['result_refs']]
        current.current()
        if not outputs:
            raise DirectActionFault('作业已结束，但没有可读取的固定文字结果')
        return AgentToolResult(status='success', summary='图片文字已提取，可复制原文或下载 TXT。',
            result_refs=arguments['result_refs'], artifact_refs=[out['artifact_ref'] for out in outputs],
            data={'verified': True, 'finalized': True, 'completeness': 'partial' if status == 'partial' else 'complete',
                  'readable_text': [out['readable_text'] for out in outputs],
                  'delivery': [out['delivery'] for out in outputs], 'driver': 'image-text-v1',
                  'rule_version': 'input-requirements-v1', 'requirement_coverage': saved.get('requirement_coverage')},
                  coverage=settled.get('coverage', {})).model_dump(mode='json')
    delivery, _ = await _operation(runtime, ctx, 'image_text_publish', {'result_refs': settled['result_refs']},
                                   publish_action, safe_replay=True)
    ctx.store.public_event(run_id, ctx.generation, run_id + ':direct-delivery', 'delivery',
                           {'status': status, 'content': delivery['summary'], 'delivery': delivery})
    commit('complete', delivery=delivery)
    ctx.store.status(run_id, ctx.generation, status)
    ctx.store.finish_public(run_id, status, generation=ctx.generation)
