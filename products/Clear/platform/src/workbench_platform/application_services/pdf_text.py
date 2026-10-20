"""Explicit PDF page text action over the existing operation and native queues."""
from __future__ import annotations

from .errors import DirectActionFault

import asyncio
from dataclasses import replace
import json
from packages.contracts.agent import AgentToolResult
from packages.contracts.direct import PdfTextRequest
from .identity import Conflict, digest, now
from .image_text import ReceiptUnknown, _context, _fixed_inputs, _operation


async def submit(runtime, session_id, request, *, source_text=None, adopt_run_id=None,
                 analysis_goal=None, interpretation_request_id=None, expected_inputs=None):
    parameters = request.model_dump(exclude={'request_id'})
    if source_text is not None:
        parameters['source_text'] = source_text
    previous = runtime.store.rows('SELECT * FROM runs WHERE request_id=?', (request.request_id,))
    if previous and adopt_run_id is None:
        run = previous[0]
        saved = json.loads(run.get('driver_state') or '{}')
        if run['session_id'] != session_id or saved.get('direct_origin_driver', run.get('driver')) != 'pdf-text-v1' or saved.get('request') != parameters:
            raise Conflict('请求身份与原 PDF 操作或页范围不同')
        return run
    session, task = runtime.store.session(session_id), runtime.tasks.get(session_id)
    if task.get('pending_scope'):
        runtime.tasks.apply_scope(session_id)
        session, task = runtime.store.session(session_id), runtime.tasks.get(session_id)
    if request.expected_scope_revision is not None and request.expected_scope_revision != task['scope_revision']:
        raise Conflict('资料范围已改变，请核对后重新提交')
    if len(session['selected_assets']) != 1:
        raise DirectActionFault('请选择一份 PDF，并明确本次要提取的页码')
    registry = runtime.services.asset_library[0]
    identifier = session['selected_assets'][0]
    asset = registry.get(session['workspace_id'], identifier)
    if asset.media_type != 'application/pdf':
        raise DirectActionFault('PDF 页文字提取只接受一份 PDF')
    inputs = [{'asset_id': identifier, 'revision': asset.revision, 'blob_hash': asset.blob_hash}]
    if expected_inputs is not None and inputs != expected_inputs:
        raise Conflict('原件版本已改变，未将新资料替换为原目标输入')
    saved = {'version': 1, 'request': parameters, 'inputs': inputs, 'scope_revision': task['scope_revision'],
             'stage': 'prepare', 'receipts': {}, 'timings': {}}
    if analysis_goal is not None:
        if analysis_goal != source_text:
            raise Conflict('后续分析必须保留完整原用户目标')
        saved['analysis_goal'] = analysis_goal
    if interpretation_request_id is not None:
        saved['interpretation_request_id'] = interpretation_request_id
    from .direct_scope import resolve_resource
    saved['resource_object'], saved['resolved_link_id'] = resolve_resource(runtime, session, 'pdf-text-v1',
                                                                          request.request_id, request.link_id)
    goal = source_text or '提取 PDF 第 ' + '、'.join(map(str, request.page_numbers)) + ' 页文字'
    from .action_admission import start
    run, fresh = start(runtime, session_id, goal, request.request_id, 'pdf-text-v1', saved, adopt_run_id=adopt_run_id)
    if fresh:
        runtime.tasks.record_goal(session_id, goal, request.request_id)
        runtime.schedule(run['id'], None)
    return run


async def drive(runtime, run_id):
    from .execution import invoke
    from .native import prepare
    from .operations import pdf
    from .job_observations import progress_for, publish_settled_progress
    from workbench_platform.artifacts.text_outputs import publish_text_result

    run = runtime.store.run(run_id)
    saved = json.loads(run['driver_state'])
    ctx = _context(runtime, run, saved)
    state = {'run_id': run_id, 'generation': run['generation'], 'session_id': run['session_id'], 'workspace_id': run['workspace_id']}

    def commit(stage, **receipts):
        ctx.current()
        saved['stage'] = stage
        saved['receipts'].update(receipts)
        saved['timings'][stage] = now()
        runtime.store.save_driver_state(run_id, ctx.generation, saved)

    if 'prepared' not in saved['receipts']:
        runtime.activity(state, 'preparing_request', reason='准备指定 PDF 页文字提取')
        def prepare_action(arguments, current):
            return AgentToolResult(status='success', summary='PDF 服务已准备', data=prepare(current, 'ocr', arguments['link_id'])).model_dump()
        prepared, _ = await _operation(runtime, ctx, 'pdf_text_prepare',
            {'link_id': saved.get('resolved_link_id') or saved['request']['link_id']}, prepare_action, safe_replay=True)
        if prepared['data'].get('requires_project_selection'):
            raise DirectActionFault('存在多个 OCR 项目，请先明确选择要使用的项目')
        commit('inputs', prepared=prepared['data'])
    if 'input_set' not in saved['receipts']:
        def freeze(arguments, current):
            value = _fixed_inputs(arguments, current)
            value['summary'] = '已固定所选 PDF 版本'
            return value
        frozen, _ = await _operation(runtime, ctx, 'pdf_text_inputs', {'inputs': saved['inputs']}, freeze, safe_replay=True)
        commit('inspect', input_set=frozen['data'])
    if 'inspected' not in saved['receipts']:
        frozen = saved['receipts']['input_set']
        arguments = {'link_id': saved['receipts']['prepared']['link_id'], 'asset_set_id': frozen['asset_set_id'], 'asset_set_revision': frozen['revision']}
        inspected, _ = await _operation(runtime, ctx, 'ocr_inspect_document', arguments,
            lambda args, current: pdf.inspect(pdf.InspectDocument.model_validate(args), current), safe_replay=True)
        documents = inspected['data'].get('documents', [])
        if len(documents) != 1 or documents[0]['asset_id'] != saved['inputs'][0]['asset_id']:
            raise DirectActionFault('PDF 导入回执与固定原件不一致')
        if max(saved['request']['page_numbers']) > documents[0]['page_count']:
            raise DirectActionFault('所选页超出真实 PDF 页数，未提交处理')
        commit('submit', inspected=inspected['data'])
    if 'submitted' not in saved['receipts']:
        inspected = saved['receipts']['inspected']
        arguments = {'handoff_id': inspected['handoff_id'], 'document_id': inspected['documents'][0]['document_id'],
                     'page_numbers': saved['request']['page_numbers'], 'mode': saved['request']['mode'], 'engine': saved['request']['engine']}
        pending, operation_ctx = await _operation(runtime, ctx, 'ocr_process_pages', arguments,
            lambda args, current: pdf.process(pdf.ProcessPages.model_validate(args), current),
            lookup=lambda args, current: pdf.lookup(pdf.ProcessPages.model_validate(args), current, include_not_submitted=True))
        if not pending.get('job_refs'):
            raise DirectActionFault('PDF 处理未返回可观察的原作业回执')
        commit('waiting', submitted=pending, submit_arguments=arguments, submitted_operation_id=operation_ctx.operation_id)
    submitted = saved['receipts']['submitted']
    arguments = saved['receipts']['submit_arguments']
    records = ctx.store.rows('SELECT * FROM operations WHERE run_id=? AND call_id=?',
        (run_id, 'direct-ocr_process_pages-' + digest(arguments)[:24]))
    if len(records) != 1 or records[0]['id'] != saved['receipts']['submitted_operation_id']:
        raise ReceiptUnknown('原 PDF 页处理操作尚不可核实，未提交新请求')
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
                runtime.store.public_event(run_id, ctx.generation, run_id + ':pdf-progress:' + fingerprint, 'job_progress',
                    {**progress, 'job_ref': submitted['job_refs'][0], 'sampled_at': now()})
                last_progress = fingerprint
            if progress.get('observation') == 'unavailable':
                from .job_lifecycle import prepare_observation
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
    await invoke(publish_settled_progress, ctx, submitted['job_refs'])
    if settled['status'] == 'error':
        commit('failed', result=settled)
        runtime.finish_error(run_id, ctx.generation, 'failed', (settled.get('error') or {}).get('code', 'pdf_failed'), settled['summary'])
        return
    commit('delivery', result=settled)
    status = 'partial' if any(settled.get('coverage', {}).get(key, 0) for key in ('failed', 'cancelled', 'pending')) else 'succeeded'
    async def publish_action(arguments, current):
        outputs = [await invoke(publish_text_result, current, reference) for reference in arguments['result_refs']]
        current.current()
        if not outputs:
            raise DirectActionFault('PDF 作业已结束，但没有可读取的固定页文字结果')
        return AgentToolResult(status='success', summary='指定 PDF 页文字已提取，可复制原文或下载 TXT。',
            result_refs=arguments['result_refs'], artifact_refs=[out['artifact_ref'] for out in outputs], coverage=settled.get('coverage', {}),
            data={'verified': True, 'finalized': True, 'completeness': 'partial' if status == 'partial' else 'complete',
                  'readable_text': [out['readable_text'] for out in outputs], 'delivery': [out['delivery'] for out in outputs],
                  'driver': 'pdf-text-v1', 'requested_pages': saved['request']['page_numbers'], 'mode': saved['request']['mode']}).model_dump(mode='json')
    delivery, _ = await _operation(runtime, ctx, 'pdf_text_publish', {'result_refs': settled['result_refs']}, publish_action, safe_replay=True)
    ctx.store.public_event(run_id, ctx.generation, run_id + ':pdf-delivery', 'delivery', {'status': status, 'content': delivery['summary'], 'delivery': delivery})
    commit('complete', delivery=delivery)
    ctx.store.status(run_id, ctx.generation, status)
    ctx.store.finish_public(run_id, status, generation=ctx.generation)
