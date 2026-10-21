"""Existing-model prediction with fixed inputs, receipts and exported snapshots."""
from .errors import DirectActionFault
import json

from packages.contracts.agent import AgentRef, AgentToolResult
from packages.contracts.direct_classification import PredictRequest
from .direct_actions import action_context, checkpoint, finish_action, operation, require_success, submit_action
from .identity import Conflict, digest
from .image_text import _fixed_inputs
from .operations import classification


DRIVER = 'classification-predict-v1'


async def submit(runtime, session_id, request, *, source_text=None):
    return await submit_action(runtime, session_id, request, driver=DRIVER,
        goal=source_text or '使用指定已有模型预测所选图片', images=True, source_text=source_text)


def select_existing_model(request, catalog):
    models = catalog.get('models', [])
    if request.model_id:
        selected = [item for item in models if item['id'] == request.model_id]
    elif request.model_name:
        selected = [item for item in models if request.model_name in
                    {item.get('id'), item.get('name'), item.get('display_name')}]
    else:
        active = catalog.get('active_model_id')
        selected = [item for item in models if item['id'] == active] if active else models
    if len(selected) != 1:
        raise DirectActionFault('没有唯一可用的指定/活动分类模型；请选择已有模型，未自动启动训练')
    return selected[0]['id']


async def drive(runtime, run_id):
    run = runtime.store.run(run_id)
    saved = json.loads(run['driver_state'])
    request = PredictRequest(request_id=run['request_id'], **{key: value for key, value in saved['request'].items() if key != 'source_text'})
    ctx = action_context(runtime, run, saved)
    receipts = saved['receipts']
    if 'prepared' not in receipts:
        def prepare_existing(args, current):
            value = classification.prepare_tool(classification.ClassificationPrepare(**args), current)
            catalog = value.get('data', {})
            if request.use_existing_model and not catalog.get('requires_project_selection'):
                from .native_mapping import adapter_for
                catalog['active_model_id'] = adapter_for(current, 'classification', catalog['link_id']).project().get('active_model_id')
            return value
        value = require_success(await operation(runtime, ctx, 'classification_prepare', {'link_id': request.link_id},
            prepare_existing, safe_replay=True))
        catalog = value['data']
        if catalog.get('requires_project_selection'):
            raise DirectActionFault('指定分类模型未在该项目登记；请选择已有模型，未自动启动训练')
        catalog['selected_model_id'] = select_existing_model(request, catalog)
        checkpoint(ctx, saved, 'inputs', 'prepared', catalog)
    selected_model = receipts['prepared'].get('selected_model_id') or request.model_id
    selected_link = receipts['prepared'].get('link_id') or request.link_id
    if not selected_model or not selected_link:
        raise DirectActionFault('原预测模型或项目回执无法核实，未提交推理或训练')
    if 'inputs' not in receipts:
        value = require_success(await operation(runtime, ctx, 'classification_inputs', {'inputs': saved['inputs']},
                                                 _fixed_inputs, safe_replay=True))
        checkpoint(ctx, saved, 'predict', 'inputs', value['data'])
    if 'prediction' not in receipts:
        fixed = receipts['inputs']
        arguments = {'link_id': selected_link, 'model_id': selected_model, 'device': request.device,
                     'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision']}
        value = require_success(await operation(runtime, ctx, 'classification_infer', arguments,
            lambda args, current: classification.infer(classification.ClassificationInfer(**args), current),
            lookup=lambda args, current: classification.lookup(classification.ClassificationInfer(**args), current,
                include_not_submitted=True)))
        checkpoint(ctx, saved, 'snapshot', 'prediction', value)
    prediction = receipts['prediction']
    if 'snapshot' not in receipts:
        handoff_id = prediction['data']['handoff_id']
        def snapshot_action(args, current):
            value = classification.snapshot(current, args['handoff_id'])
            ref = prediction['result_refs'][0]
            if digest(value['items']) != ref['sha256']:
                raise Conflict('分类预测版本已改变，未将新人工处置内容替换为原预测回执')
            return AgentToolResult(status='success', summary='已固定预测和人工标签的独立版本', data=value).model_dump()
        value = require_success(await operation(runtime, ctx, 'classification_pin_snapshot', {'handoff_id': handoff_id},
                                                 snapshot_action, safe_replay=True))
        checkpoint(ctx, saved, 'export', 'snapshot', value['data'])
    if 'export' not in receipts:
        def export_action(args, current):
            current.current()
            bundle = current.services.artifact_center.classification.publish(current.workspace_id,
                args['handoff_id'], snapshot=receipts['snapshot'])
            return AgentToolResult(status='success', summary='已有模型预测及来源已导出', data={'bundle': bundle},
                artifact_refs=[AgentRef(kind='artifact', workspace_id=current.workspace_id, object_id=bundle['bundle_id'],
                                       revision=1, application='classification', parent_id=args['handoff_id'])]).model_dump()
        value = require_success(await operation(runtime, ctx, 'classification_export_fixed',
            {'handoff_id': receipts['snapshot']['handoff_id'], 'snapshot_hash': digest(receipts['snapshot'])},
            export_action, safe_replay=True))
        checkpoint(ctx, saved, 'delivery', 'export', value)
    await finish_action(runtime, ctx, driver=DRIVER, summary='已有模型预测已完成，可查看预测、人工标签及下载结果。',
        result_refs=prediction['result_refs'], artifact_refs=receipts['export']['artifact_refs'],
        coverage=prediction.get('coverage', {}), data={'model_id': selected_model, 'device': request.device,
        'inference_and_annotation_are_distinct': True, 'preview': prediction.get('data', {})}, saved=saved)
