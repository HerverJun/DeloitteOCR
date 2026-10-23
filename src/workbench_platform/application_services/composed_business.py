"""Agent entry points over the existing fixed classification/data sequences."""
from __future__ import annotations

from dataclasses import replace
import json
import re
from types import SimpleNamespace
from typing import Literal
from urllib.parse import quote

from pydantic import Field, model_validator
from packages.contracts.agent import AgentModel, AgentToolResult, Identity
from packages.contracts.data_operations import CompareSpec, InputOptions
from packages.contracts.direct_data import ActionStep
from . import classification_action, data_action
from .context import require_write
from .errors import DirectActionFault
from .identity import Conflict, RunSuperseded
from .operations import data


class ClassificationPredict(AgentModel):
    asset_ids: list[Identity] = Field(default_factory=list, max_length=100)
    link_id: Identity | None = None
    model_id: Identity | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=100)
    device: Literal['cpu', 'cuda'] = 'cpu'
    resume_operation_id: Identity | None = None

    @model_validator(mode='after')
    def distinct(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError('资料不能重复')
        if self.model_id and self.model_name:
            raise ValueError('模型 ID 与名称只需填写一个')
        return self


class NamedCompareSpec(AgentModel):
    key_pairs: list[list[str]] = Field(min_length=1, max_length=256)
    compare_pairs: list[list[str]] = Field(default_factory=list, max_length=256)
    amount_pairs: list[list[str]] = Field(default_factory=list, max_length=256)
    type_policy: Literal['strict', 'text'] = 'strict'
    null_policy: Literal['never_match', 'equal'] = 'never_match'
    amount_tolerance: str = Field(default='0', pattern=r'^\d+(\.\d+)?$')
    trim_text: bool = False
    casefold_text: bool = False

    @model_validator(mode='after')
    def pairs(self):
        for pair in [*self.key_pairs, *self.compare_pairs, *self.amount_pairs]:
            if len(pair) != 2 or any(not value or len(value) > 256 for value in pair):
                raise ValueError('列配对需两个实际列 ID 或唯一表头')
        if any(pair not in self.compare_pairs for pair in self.amount_pairs):
            raise ValueError('金额列必须同时列为比较列')
        return self


class CompareFiles(AgentModel):
    left_asset_id: Identity
    right_asset_id: Identity
    left_options: InputOptions = Field(default_factory=InputOptions)
    right_options: InputOptions = Field(default_factory=InputOptions)
    steps: list[ActionStep] = Field(default_factory=list, max_length=32)
    compare_spec: NamedCompareSpec
    name: str = Field(default='双表核对', min_length=1, max_length=100)
    resume_operation_id: Identity | None = None

    @model_validator(mode='after')
    def different(self):
        if self.left_asset_id == self.right_asset_id:
            raise ValueError('双表核对需要两份不同资料')
        return self


def resolve_columns(spec, tables):
    """Resolve unique real headers; IDs always retain their explicit meaning."""
    issues = {}
    def resolve(value, side):
        columns = tables[side]['columns']
        if re.fullmatch(r'c[1-9][0-9]{0,2}', value):
            matches = [column for column in columns if column['column_id'] == value]
        else:
            matches = [column for column in columns if column['header'] == value]
        if len(matches) != 1:
            available = '、'.join(column['column_id'] + '=' + str(column['header']) for column in columns)
            issues[(side, value)] = f'{side} 表列“{value}”不能唯一确定，可选实际列：{available}'
            return value
        return matches[0]['column_id']
    value = spec.model_dump()
    for field in ('key_pairs', 'compare_pairs', 'amount_pairs'):
        value[field] = [[resolve(pair[0], 'left'), resolve(pair[1], 'right')] for pair in value[field]]
    if issues:
        raise DirectActionFault('请一次确认以下列映射：' + '；'.join(issues.values()), code='ambiguous_column')
    return CompareSpec(**value)


def original_context(args, ctx, tool, seen=None):
    if not args.resume_operation_id:
        return ctx
    seen = set() if seen is None else seen
    if args.resume_operation_id in seen:
        raise ValueError('继续操作引用不能形成循环')
    seen.add(args.resume_operation_id)
    rows = ctx.store.rows('SELECT * FROM operations WHERE id=? AND run_id=? AND tool=?',
                         (args.resume_operation_id, ctx.run_id, tool))
    if not rows:
        raise ValueError('只能继续本任务原组合操作')
    original = type(args).model_validate(json.loads(rows[0]['arguments']))
    if original.model_dump(exclude={'resume_operation_id'}) != args.model_dump(exclude={'resume_operation_id'}):
        raise ValueError('继续操作须保留原输入和参数；修订要求应作为新操作')
    if original.resume_operation_id:
        return original_context(original, ctx, tool, seen)
    return replace(ctx, operation_id=rows[0]['id'])


async def fixed_inputs(op, identifiers, *, images=False):
    from .composed import selected
    def freeze(arguments, ctx):
        if not 1 <= len(arguments['asset_ids']) <= 100 or len(set(arguments['asset_ids'])) != len(arguments['asset_ids']):
            raise ValueError('请选择 1–100 份不同资料')
        inputs = selected(ctx, arguments['asset_ids'])
        if images and any(not item['media_type'].startswith('image/') for item in inputs):
            raise ValueError('分类预测只接受图片')
        return AgentToolResult(status='success', summary='已固定输入版本', data={'inputs': inputs}).model_dump()
    value = op.require_success(await op.call('business_fixed_inputs', {'asset_ids': identifiers}, freeze, replay=True))
    inputs = value['data']['inputs']
    from .references import asset_allowed
    for item in inputs:
        if not asset_allowed(op.context, item['asset_id']):
            raise Conflict('原资料已经不在当前任务范围，未继续原操作')
        asset = op.context.services.asset_library[0].get(op.context.workspace_id, item['asset_id'])
        if (asset.revision, asset.blob_hash) != (item['revision'], item['blob_hash']):
            raise Conflict('原资料版本已经改变，原结果保留；修订后请发起新操作')
    return inputs


def result_with_downloads(value, ctx, inputs, *, failure_key):
    bundle = value['data'].get('bundle') or {}
    base = f'/api/platform/v1/workspaces/{ctx.workspace_id}/artifacts/{bundle.get("bundle_id", "")}/files/'
    files = [{**file, 'download_url': base + quote(file['name'])} for file in bundle.get('files', [])]
    return AgentToolResult(status='success', **{**value, 'data': {**value['data'], 'downloads': files,
        'bundle': {**bundle, 'files': files}, 'completed_failure_keys': [failure_key],
        'files': [{'asset_id': item['asset_id'], 'name': item['name'], 'downloads': files} for item in inputs]}}).model_dump(mode='json')


async def run_sequence(args, ctx, tool, request, *, images=False):
    from .composed import Operations
    require_write(ctx)
    current_call = ctx.operation_id
    ctx = original_context(args, ctx, tool)
    failure_key = f'{tool}:{ctx.operation_id}'
    op = Operations(ctx)
    identifiers = (args.asset_ids or list(ctx.selected_assets)) if images else [args.left_asset_id, args.right_asset_id]
    previous_inputs = ctx.store.rows('SELECT arguments FROM operations WHERE run_id=? AND parent_id=? AND tool=?',
        (ctx.run_id, ctx.operation_id, 'business_fixed_inputs'))
    if previous_inputs:
        identifiers = json.loads(previous_inputs[0]['arguments'])['asset_ids']
    inputs = await fixed_inputs(op, identifiers, images=images)
    saved = {'inputs': inputs, 'receipts': {}}
    def remember(stage, name, value):
        saved['receipts'][name] = value
    async def call(name, arguments, handler, *, lookup=None, safe_replay=False):
        op.namespace = ''
        # A confirmed failed export can be retried alone. Unknown and accepted
        # writes keep their original key and follow the ordinary lookup path.
        if args.resume_operation_id and name == 'data_export':
            rows = ctx.store.rows('SELECT arguments,result FROM operations WHERE run_id=? AND parent_id=? AND tool=? ORDER BY created DESC',
                (ctx.run_id, ctx.operation_id, name))
            for row in rows:
                prior = json.loads(row['arguments'])
                if any(prior.get(key) != arguments.get(key) for key in ('data_session_id', 'result_id', 'side')):
                    continue
                result = json.loads(row['result']) if row['result'] else {}
                if result.get('status') == 'error' and result.get('data', {}).get('native_status') in {'failed', 'cancelled'}:
                    op.namespace = 'export-retry:' + current_call
                    arguments = {**arguments, 'expected_version': ctx.services.data_workbench.get(arguments['data_session_id'])['version']}
                else:
                    op.namespace = prior.get('_composition_key', '')
                    arguments = {key: value for key, value in prior.items() if key not in {'parent_operation_id', '_composition_key'}}
                break
        return await op.call(name, arguments, handler, lookup=lookup, replay=safe_replay)
    try:
        if images:
            value = await classification_action.execute(op.runtime, ctx, saved, request, call=call, save=remember)
        else:
            # Discover sheet ambiguity before creating any native data session.
            by_id = {item['asset_id']: item for item in inputs}
            sheet_questions = []
            for side in ('left', 'right'):
                asset = by_id[getattr(args, side + '_asset_id')]
                inspect_args = {'asset_id': asset['asset_id'], 'asset_revision': asset['revision'],
                                'blob_hash': asset['blob_hash'], 'options': getattr(args, side + '_options').model_dump()}
                inspected = op.require_success(await call('data_inspect_input', inspect_args,
                    lambda value, current: data.inspect(data.DataInspect(**value), current), safe_replay=True))
                if inspected['data'].get('requires_sheet_selection'):
                    sheet_questions.append(f'{side} 表可选：' +
                        '、'.join(str(sheet.get('name') or sheet.get('sheet_name')) for sheet in inspected['data']['sheets']))
            if sheet_questions:
                raise DirectActionFault('请一次明确以下文件使用的工作表：' + '；'.join(sheet_questions), code='ambiguous_sheet')
            value = await data_action.execute(op.runtime, ctx, saved, request, call=call, save=remember,
                                              resolve_compare=resolve_columns)
        return result_with_downloads(value, ctx, inputs, failure_key=failure_key)
    except (RunSuperseded, Conflict):
        raise
    except (OSError, ValueError) as error:
        receipts = saved['receipts']
        fixed = receipts.get('prediction') if images else receipts.get('preview')
        if fixed:
            repair = args.model_dump(mode='json') | {'resume_operation_id': ctx.operation_id}
            return AgentToolResult(status='error', summary='分析结果已保存，输出尚未完成；可只续原操作的缺失步骤',
                result_refs=fixed.get('result_refs', []), coverage=fixed.get('coverage', {}),
                data={'preview': fixed.get('data', {}), 'repair': {'tool': tool, 'arguments': repair},
                      'failures': [{'failure_key': failure_key, 'asset_ids': [item['asset_id'] for item in inputs], 'stage': 'export',
                                    'reason': '分析结果已保存，输出尚未完成'}]},
                error={'code': 'export_incomplete', 'message': '原计算结果保留，继续时不会重新导入或推理', 'retryable': True}).model_dump()
        if isinstance(error, DirectActionFault) and error.code in {'ambiguous_column', 'ambiguous_sheet', 'model_selection_required', 'project_selection_required'}:
            from .decisions import AskUser, ask_user
            return ask_user(AskUser(question=str(error)[:2000]), ctx)
        if isinstance(error, DirectActionFault):
            return AgentToolResult(status='error', summary=str(error), data={},
                error={'code': error.code, 'message': str(error), 'retryable': False}).model_dump()
        raise


async def classification_predict(args, ctx):
    request = SimpleNamespace(**args.model_dump(exclude={'asset_ids', 'resume_operation_id'}), use_existing_model=True)
    return await run_sequence(args, ctx, 'classification_predict', request, images=True)


async def data_compare_files(args, ctx):
    request = SimpleNamespace(**{name: getattr(args, name) for name in
        ('left_asset_id', 'right_asset_id', 'left_options', 'right_options', 'steps', 'compare_spec', 'name')}, action='compare')
    return await run_sequence(args, ctx, 'data_compare_files', request)
