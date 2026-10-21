"""Typed sequential data cleaning and comparison over the existing operators."""
import json

from packages.contracts.direct_data import ActionStep, DataActionRequest
from .direct_actions import action_context, checkpoint, finish_action, operation, require_success, submit_action
from .operations import data


DRIVER = 'data-action-v1'


async def submit(runtime, session_id, request, *, source_text=None):
    identifiers = [request.left_asset_id] + ([request.right_asset_id] if request.right_asset_id else [])
    return await submit_action(runtime, session_id, request, driver=DRIVER,
        goal=source_text or ('按明确算子清洗表格' if request.action == 'clean' else '按明确列条件核对双表'),
        asset_ids=identifiers, source_text=source_text)


async def drive(runtime, run_id):
    run = runtime.store.run(run_id)
    saved = json.loads(run['driver_state'])
    request = DataActionRequest(request_id=run['request_id'], **{key: value for key, value in saved['request'].items() if key != 'source_text'})
    ctx = action_context(runtime, run, saved)
    receipts = saved['receipts']
    if 'created' not in receipts:
        value = require_success(await operation(runtime, ctx, 'data_create_session', {'name': request.name},
            lambda args, current: data.create(data.DataCreate(**args), current), safe_replay=True))
        checkpoint(ctx, saved, 'import', 'created', value)
    latest = receipts['created']
    inputs = {item['asset_id']: item for item in saved['inputs']}
    for side, identifier, options in [('left', request.left_asset_id, request.left_options),
                                      ('right', request.right_asset_id, request.right_options)]:
        if identifier is None:
            continue
        key = 'import_' + side
        if key not in receipts:
            fixed = inputs[identifier]
            arguments = {'data_session_id': latest['data']['data_session_id'], 'expected_version': latest['data']['version'],
                'side': side, 'asset_id': identifier, 'asset_revision': fixed['revision'], 'blob_hash': fixed['blob_hash'],
                'options': options.model_dump(mode='json')}
            value = require_success(await operation(runtime, ctx, 'data_import', arguments,
                lambda args, current: data.import_data(data.DataImport(**args), current),
                lookup=lambda args, current: data.lookup(data.DataImport(**args), current, include_not_submitted=True)))
            checkpoint(ctx, saved, key, key, value)
        latest = receipts[key]
    for index, step in enumerate(request.steps):
        key = 'step_' + str(index)
        if key not in receipts:
            arguments = {'data_session_id': latest['data']['data_session_id'], 'expected_version': latest['data']['version'],
                         **step.model_dump(mode='json')}
            value = require_success(await operation(runtime, ctx, 'data_apply_step', arguments,
                lambda args, current: data.apply_step(data.DataStep(**args), current),
                lookup=lambda args, current: data.lookup(data.DataStep(**args), current, include_not_submitted=True)))
            checkpoint(ctx, saved, key, key, value)
        latest = receipts[key]
    if request.action == 'compare':
        if 'compared' not in receipts:
            arguments = {'data_session_id': latest['data']['data_session_id'], 'expected_version': latest['data']['version'],
                         'spec': request.compare_spec.model_dump(mode='json')}
            value = require_success(await operation(runtime, ctx, 'data_compare', arguments,
                lambda args, current: data.compare(data.DataCompare(**args), current),
                lookup=lambda args, current: data.lookup(data.DataCompare(**args), current, include_not_submitted=True)))
            checkpoint(ctx, saved, 'preview', 'compared', value)
        latest = receipts['compared']
    if 'preview' not in receipts:
        if request.action == 'compare':
            arguments = {'data_session_id': latest['data']['data_session_id'], 'result_id': latest['data']['result_id']}
            handler = lambda args, current: data.read_result(data.DataResult(**args), current)
        else:
            arguments = {'data_session_id': latest['data']['data_session_id'], 'side': 'left',
                         'revision': latest['data']['tables']['left']['revision']}
            handler = lambda args, current: data.read_table(data.DataTable(**args), current)
        value = require_success(await operation(runtime, ctx, 'data_read_fixed', arguments, handler, safe_replay=True))
        checkpoint(ctx, saved, 'export', 'preview', value)
    if 'exported' not in receipts:
        arguments = {'data_session_id': latest['data']['data_session_id'], 'expected_version': latest['data']['version']}
        arguments.update({'result_id': latest['data']['result_id']} if request.action == 'compare' else {'side': 'left'})
        value = require_success(await operation(runtime, ctx, 'data_export', arguments,
            lambda args, current: data.export(data.DataExport(**args), current),
            lookup=lambda args, current: data.lookup(data.DataExport(**args), current, include_not_submitted=True)))
        checkpoint(ctx, saved, 'delivery', 'exported', value)
    preview = receipts['preview']
    await finish_action(runtime, ctx, driver=DRIVER, summary='指定数据处理已完成，可预览固定结果或下载完整表格。',
        result_refs=preview.get('result_refs', []), artifact_refs=receipts['exported'].get('artifact_refs', []),
        coverage=preview.get('coverage', {}), data={'action': request.action, 'preview': preview.get('data', {}),
        'fixed_operator_count': len(request.steps), 'decimal_and_source_values_preserved': True}, saved=saved)
