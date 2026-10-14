"""ClearData tools. All calculations use the fixed existing data operators."""
from __future__ import annotations

from typing import Annotated, Literal, Union
from pydantic import Field, model_validator
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity, Hash
from workbench_platform.application_services.data import stage_asset_upload
from workbench_platform.agent.registry import ToolSpec


class DataCreate(AgentModel):
    name: str = Field(min_length=1, max_length=100)


class InputOptions(AgentModel):
    sheet_name: str | None = Field(default=None, min_length=1, max_length=256)
    header_row: int = Field(default=1, ge=1, le=100)
    encoding: Literal['utf-8', 'utf-8-sig', 'gb18030', 'utf-16'] = 'utf-8-sig'
    delimiter: Literal[',', '\t', ';', '|'] = ','


class DataInspect(AgentModel):
    asset_id: Identity
    asset_revision: int = Field(ge=1)
    blob_hash: Hash
    options: InputOptions = Field(default_factory=InputOptions)


class DataWrite(AgentModel):
    data_session_id: Identity
    expected_version: int = Field(ge=1)


class DataImport(DataWrite, DataInspect):
    side: Literal['left', 'right']


class Trim(AgentModel):
    operator: Literal['trim']
    column: str = Field(pattern=r'^c[1-9][0-9]{0,2}$')


class Rename(Trim):
    operator: Literal['rename']
    header: str = Field(min_length=1, max_length=256)


class MapValues(Trim):
    operator: Literal['map']
    values: dict[str, str] = Field(min_length=1, max_length=1000)


class Cast(Trim):
    operator: Literal['cast']
    target: Literal['decimal', 'integer', 'boolean', 'date']


class Filter(Trim):
    operator: Literal['filter']
    predicate: Literal['equals', 'not_equals', 'is_null', 'not_null']
    value: str | bool | None = None

    @model_validator(mode='after')
    def valid_filter(self):
        if self.predicate in {'equals', 'not_equals'} and self.value is None:
            raise ValueError('Filter value is required')
        if self.predicate in {'is_null', 'not_null'} and self.value is not None:
            raise ValueError('Null filter does not accept a value')
        return self


class Duplicates(AgentModel):
    operator: Literal['duplicates']
    columns: list[str] = Field(min_length=1, max_length=256)
    include_null: bool = False


class TypedCompare(Trim):
    operator: Literal['filter_compare']
    predicate: Literal['gt', 'ge', 'lt', 'le', 'between']
    value_kind: Literal['decimal', 'date', 'text']
    value: str = Field(max_length=4096)
    upper: str | None = Field(default=None, max_length=4096)

    @model_validator(mode='after')
    def interval(self):
        if (self.predicate == 'between') != (self.upper is not None):
            raise ValueError('Only between requires an upper bound')
        return self


class GroupSummary(AgentModel):
    operator: Literal['group_summary']
    columns: list[str] = Field(min_length=1, max_length=256)
    amount_columns: list[str] = Field(default_factory=list, max_length=256)


class DataStep(DataWrite):
    side: Literal['left', 'right']
    step: Annotated[Union[Trim, Rename, MapValues, Cast, Filter, Duplicates, TypedCompare, GroupSummary], Field(discriminator='operator')]


class CompareSpec(AgentModel):
    key_pairs: list[list[str]] = Field(min_length=1, max_length=256)
    compare_pairs: list[list[str]] = Field(default_factory=list, max_length=256)
    amount_pairs: list[list[str]] = Field(default_factory=list, max_length=256, description='金额列对必须同时出现在 compare_pairs 中')
    type_policy: Literal['strict', 'text'] = 'strict'
    null_policy: Literal['never_match', 'equal'] = 'never_match'
    amount_tolerance: str = Field(default='0', pattern=r'^\d+(\.\d+)?$')
    trim_text: bool = False
    casefold_text: bool = False

    @model_validator(mode='after')
    def pairs(self):
        import re
        for pair in [*self.key_pairs, *self.compare_pairs, *self.amount_pairs]:
            if len(pair) != 2 or any(not re.fullmatch(r'c[1-9][0-9]{0,2}', column) for column in pair):
                raise ValueError('Column pairs must contain two queried column IDs')
        if any(pair not in self.compare_pairs for pair in self.amount_pairs):
            raise ValueError('Amount pairs must also be comparison pairs')
        return self


class DataCompare(DataWrite):
    spec: CompareSpec


class DataPage(AgentModel):
    data_session_id: Identity
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class DataTable(DataPage):
    side: Literal['left', 'right']
    revision: Identity | None = None


class DataResult(DataPage):
    result_id: Identity
    category: Literal['same', 'different', 'only_left', 'only_right', 'ambiguous'] | None = None


class DataExport(DataWrite):
    result_id: Identity | None = None
    side: Literal['left', 'right'] | None = None
    categories: list[Literal['same', 'different', 'only_left', 'only_right', 'ambiguous']] | None = Field(default=None, min_length=1, max_length=5)

    @model_validator(mode='after')
    def target(self):
        if bool(self.result_id) == bool(self.side):
            raise ValueError('Choose comparison result or one table')
        if self.side and self.categories:
            raise ValueError('Categories apply only to comparison results')
        return self


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
    service, session = data_session(args, context)
    registry, asset = scoped_asset(args, context)
    options = args.options.model_dump(exclude_none=True)
    if asset.original_name.lower().endswith('.xlsx') and not options.get('sheet_name'):
        discovery = inspect(args, context)['data']
        if discovery['requires_sheet_selection']:
            return AgentToolResult(status='error', summary='资料含多个工作表，请先探查并向用户确认工作表',
                error={'code': 'ambiguous_sheet', 'message': '多个工作表且没有明确选择，尚未导入'}).model_dump()
    upload = stage_asset_upload(service, registry, context.workspace_id, session['id'], asset.asset_id,
                               asset.revision, asset.blob_hash, context.operation_id)
    job = service.submit(session['id'], 'import', {'side': args.side, 'upload_id': upload['id'], 'options': options},
                         args.expected_version, request_key=context.operation_id)
    return pending(service, job, context)


def apply_step(args, context):
    service, session = data_session(args, context)
    step = args.step.model_dump(exclude_none=True)
    operator = step.pop('operator')
    if operator == 'filter' and step['predicate'] in {'is_null', 'not_null'}:
        step.pop('value', None)
    job = service.submit(session['id'], 'step', {'side': args.side, 'operator': operator, 'parameters': step},
                         args.expected_version, request_key=context.operation_id)
    return pending(service, job, context)


def compare(args, context):
    service, session = data_session(args, context)
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
    service, session = data_session(args, context)
    action, payload = ('export', {'result_id': args.result_id}) if args.result_id else ('export_table', {'side': args.side})
    if args.categories:
        payload['categories'] = args.categories
    return pending(service, service.submit(session['id'], action, payload, args.expected_version,
                                           request_key=context.operation_id), context)


def lookup(args, context):
    service, _ = data_session(args, context)
    job = service.operation(context.operation_id)
    return pending(service, job, context) if job else None


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
        result.artifact_refs = [AgentRef(kind='artifact', workspace_id=context.workspace_id,
            object_id=bundle['bundle_id'], revision=1, application='data', parent_id=session['id'])]
    return result.model_dump()


def register(registry):
    for name, schema, handler, description, effect in [
        ('data_create_session', DataCreate, create, '在当前项目创建幂等数据作业，返回真实作业 ID 和版本。', 'derive'),
        ('data_inspect_input', DataInspect, inspect, '导入前探查资料的工作表、列与最多5行样本。多个工作表应确认选择。', 'read'),
        ('data_import', DataImport, import_data, '将当前项目的已选资料按明确工作表和表头导入左右表；需当前作业版本。', 'versioned'),
        ('data_read_table', DataTable, read_table, '读取实际表格列ID、单元格类型与来源行，返回最新作业版本，每页最多50行。', 'read'),
        ('data_apply_step', DataStep, apply_step, '按真实列ID清洗、duplicates检测、filter_compare类型化大小/区间筛选、group_summary分组计数与精确Decimal金额合计。金额须先明确转decimal；未知金额会标错误，不生成部分合计。汇总保留全部source_rows。', 'versioned'),
        ('data_compare', DataCompare, compare, '按已查到的列ID进行精确比对。金额阈值用十进制字符串，保留编号和来源。', 'versioned'),
        ('data_read_result', DataResult, read_result, '读取比对结果的真实差异、覆盖和来源，每页最多50行。', 'read'),
        ('data_export', DataExport, export, '导出已存在的比对结果或单张清洗表，生成并归集实际CSV/XLSX/Parquet。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect,
            availability=available, input_kinds=('asset', 'table', 'result'), output_kinds=('table', 'result', 'artifact'),
            resource_keys=lambda args, ctx: ('data:' + getattr(args, 'data_session_id', ctx.workspace_id),),
            lookup_operation=lookup if schema in (DataImport, DataStep, DataCompare, DataExport) else None,
            idempotent=True))
