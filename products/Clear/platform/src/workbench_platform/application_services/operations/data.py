"""ClearData tools. All calculations use the fixed existing data operators."""
from __future__ import annotations

from typing import Annotated, Literal, Union
from pydantic import Field, model_validator
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity, Hash
from workbench_platform.application_services.data import stage_asset_upload
from workbench_platform.application_services.context import require_write


from packages.contracts.data_operations import *


def available(services):
    ready = services.data_workbench is not None and services.data_workbench.ready()
    return {'state': 'ready' if ready else 'unavailable', 'reason': '' if ready else '未配置数据分析运行时'}


def data_session(args, context):
    service = context.services.data_workbench
    session = service.get(args.data_session_id)
    if session['workspace_id'] != context.workspace_id:
        raise ValueError('跨项目作业')
    from workbench_platform.application_services.references import data_scope
    data_scope(context, session)
    return service, session


def compact(session):
    return {'data_session_id': session['id'], 'version': session['version'],
            'tables': {side: {key: table[key] for key in ('revision', 'name', 'format', 'columns', 'rows', 'source')}
                       for side, table in session['state']['tables'].items()},
            'result_id': session['state']['result_id']}


def scoped_asset(args, context):
    registry = context.services.asset_library[0]
    from workbench_platform.application_services.references import asset_allowed
    if not asset_allowed(context, args.asset_id):
        raise ValueError('资料不在会话选中范围')
    asset = registry.get(context.workspace_id, args.asset_id)
    if (asset.revision, asset.blob_hash) != (args.asset_revision, args.blob_hash):
        raise ValueError('资料版本已改变')
    return registry, asset


def create(args, context):
    require_write(context)
    context.current()
    session = context.services.data_workbench.create(args.name, request_key=context.operation_id,
                                                  workspace_id=context.workspace_id)
    return AgentToolResult(status='success', summary='已建立数据作业', data=compact(session)).model_dump()


def inspect(args, context):
    registry, asset = scoped_asset(args, context)
    with registry.stage_verified(context.workspace_id, asset.asset_id,
                                 revision=asset.revision, blob_hash=asset.blob_hash) as path:
        value = context.services.data_workbench.inspect_input(path, asset.original_name.rsplit('.', 1)[-1].lower(),
                                                            args.options.model_dump(exclude_none=True))
    return AgentToolResult(status='success', summary='已读取实际工作表、列和样本', data=value).model_dump()


def pending(service, job, context):
    return AgentToolResult(status='pending', summary='已提交真实数据任务，运行时将等待结果',
        job_refs=[AgentRef(kind='job', workspace_id=context.workspace_id, object_id=job['id'],
            revision=service.get(job['session_id'])['version'], application='data', parent_id=job['session_id'],
            native_id=job['id'])], data={'action': job['action'], 'native_status': job['status']}).model_dump()


def import_data(args, context):
    require_write(context)
    service, session = data_session(args, context)
    registry, asset = scoped_asset(args, context)
    options = args.options.model_dump(exclude_none=True)
    if asset.original_name.lower().endswith('.xlsx') and not options.get('sheet_name'):
        discovery = inspect(args, context)['data']
        if discovery['requires_sheet_selection']:
            return AgentToolResult(status='error', summary='资料含多个工作表，请先探查并向用户确认工作表',
                error={'code': 'ambiguous_sheet', 'message': '多个工作表且没有明确选择，尚未导入'}).model_dump()
    context.current()
    upload = stage_asset_upload(service, registry, context.workspace_id, session['id'], asset.asset_id,
                               asset.revision, asset.blob_hash, context.operation_id)
    context.current()
    job = service.submit(session['id'], 'import', {'side': args.side, 'upload_id': upload['id'], 'options': options},
                         args.expected_version, request_key=context.operation_id)
    return pending(service, job, context)


def apply_step(args, context):
    require_write(context)
    service, session = data_session(args, context)
    step = args.step.model_dump(exclude_none=True)
    operator = step.pop('operator')
    if operator == 'filter' and step['predicate'] in {'is_null', 'not_null'}:
        step.pop('value', None)
    context.current()
    job = service.submit(session['id'], 'step', {'side': args.side, 'operator': operator, 'parameters': step},
                         args.expected_version, request_key=context.operation_id)
    return pending(service, job, context)


def compare(args, context):
    require_write(context)
    service, session = data_session(args, context)
    context.current()
    job = service.submit(session['id'], 'compare', {'spec': args.spec.model_dump()},
                         args.expected_version, request_key=context.operation_id)
    return pending(service, job, context)


def read_table(args, context):
    service, session = data_session(args, context)
    table = session['state']['tables'][args.side]
    revision = args.revision or table['revision']
    value = service.page(session['id'], 'table', revision, args.offset, args.limit)
    from workbench_platform.application_services.references import data_reference
    identity = data_reference(context, session['id'], 'table', revision)
    return AgentToolResult(status='success', summary='已读取实际表格分页',
        data={**value, 'data_session_id': session['id'], 'version': session['version']},
        result_refs=[AgentRef(kind='table', workspace_id=context.workspace_id, object_id=revision,
                             revision=revision, parent_id=session['id'], application='data', sha256=identity['content_sha256'])],
        next_offset=args.offset + args.limit if args.offset + args.limit < value['total'] else None).model_dump()


def read_result(args, context):
    service, session = data_session(args, context)
    value = service.page(session['id'], 'result', args.result_id, args.offset, args.limit, args.category)
    from workbench_platform.application_services.references import data_reference
    identity = data_reference(context, session['id'], 'result', args.result_id)
    return AgentToolResult(status='success', summary='已读取真实差异结果', data=value,
        result_refs=[AgentRef(kind='result', workspace_id=context.workspace_id, object_id=args.result_id,
                             revision=args.result_id, parent_id=session['id'], application='data', sha256=identity['content_sha256'])],
        coverage=value['metadata']['coverage'],
        next_offset=args.offset + args.limit if args.offset + args.limit < value['total'] else None).model_dump()


def export(args, context):
    require_write(context)
    service, session = data_session(args, context)
    action, payload = ('export', {'result_id': args.result_id}) if args.result_id else ('export_table', {'side': args.side})
    if args.categories:
        payload['categories'] = args.categories
    context.current()
    return pending(service, service.submit(session['id'], action, payload, args.expected_version,
                                           request_key=context.operation_id), context)


def lookup(args, context, *, include_not_submitted=False):
    service, _ = data_session(args, context)
    job = service.operation(context.operation_id)
    if job:
        if job['session_id'] != args.data_session_id:
            raise ValueError('原操作回执属于其他数据作业')
        return pending(service, job, context)
    authority = getattr(service, 'operation_not_submitted', None)
    if include_not_submitted and callable(authority):
        context.current()
        if isinstance(args, DataImport):
            scoped_asset(args, context)
        absent = authority(context.operation_id, args.data_session_id, args.expected_version, context.workspace_id)
        context.current()
        if absent is True:
            from workbench_platform.application_services.errors import NativeNotSubmitted
            from workbench_platform.application_services.identity import digest
            tool = {DataImport: 'data_import', DataStep: 'data_apply_step',
                    DataCompare: 'data_compare', DataExport: 'data_export'}.get(type(args))
            if tool:
                return NativeNotSubmitted(context.operation_id, tool, context.operation_id,
                    args.data_session_id, digest(args.model_dump(mode='json')))
    return None


def settle(job_ref, context):
    context.current()
    service = context.services.data_workbench
    session = service.get(job_ref['parent_id'])
    if session['workspace_id'] != context.workspace_id:
        raise ValueError('跨项目任务')
    job = service.job(session['id'], job_ref['object_id'])
    if job['status'] in {'queued', 'running', 'cancelling'}:
        return None
    if job['status'] == 'interrupted':
        operations = context.store.rows('SELECT id FROM operations WHERE run_id=? AND '
            "json_extract(result,'$.job_refs[0].object_id')=?", (context.run_id, job['id']))
        if operations:
            context.current()
            job = service.resume_operation(operations[0]['id'])
            if job['status'] in {'queued', 'running', 'cancelling'}:
                return None
    if job['status'] != 'succeeded':
        return AgentToolResult(status='error', summary='数据任务未成功，保留原有成果', data={'native_status': job['status']},
            error={'code': job['status'], 'message': (job['error'] or '数据任务未完成')[:500]}).model_dump()
    result = AgentToolResult(status='success', summary='数据任务已完成', data=compact(service.get(session['id'])))
    if job['action'] in {'export', 'export_table'}:
        context.current()
        bundle = context.services.artifact_center.publish_data_export(context.workspace_id, session['id'], job['id'])
        result.data['bundle'] = bundle
        if 'table_schema' in bundle:
            result.data['table_schema'] = bundle['table_schema']
        result.artifact_refs = [AgentRef(kind='artifact', workspace_id=context.workspace_id,
            object_id=bundle['bundle_id'], revision=1, application='data', parent_id=session['id'])]
    return result.model_dump()
